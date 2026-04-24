"""
Exit Executor — handles the mechanics of closing a trade on IB.

ARCH-005: This is the SINGLE code path that closes trades.
All close requests funnel through execute_exit().

SAFETY RULES:
1. Cancel ALL bracket orders for the contract (not just stored IDs)
2. VERIFY brackets are cancelled before proceeding
3. Check IB position quantity before selling
4. NEVER sell more than we hold
5. If any check fails, ABORT and retry next cycle

Every step is logged with full detail for tracing.
"""
import logging
import time
from datetime import datetime
import pytz

log = logging.getLogger(__name__)
PT = pytz.timezone("America/Los_Angeles")


def _trace(ticker: str, message: str, level: str = "info"):
    """Log to BOTH Python logger AND system_log DB for dashboard visibility."""
    component = f"exit_executor-{ticker}"
    if level == "error":
        log.error(f"[{ticker}] {message}")
    elif level == "warn":
        log.warning(f"[{ticker}] {message}")
    else:
        log.info(f"[{ticker}] {message}")
    try:
        from db.writer import add_system_log
        add_system_log(component, level, message[:500])
    except Exception:
        pass


def cancel_all_orders_and_verify(client, trade: dict) -> bool:
    """
    STEP 1: Find ALL open orders for this contract on IB.
    STEP 2: Cancel every one of them.
    STEP 3: Verify all cancelled.

    This runs FIRST, before checking position. By cancelling all orders first,
    we eliminate any other process (IB bracket) that could close the trade
    while we're working on it.

    If no orders found but brackets were expected → bracket may have just fired.
    Wait 2s, re-check position. If qty=0 → bracket closed it, set _bracket_fired flag.

    Returns True if safe to proceed, False if should abort.
    """
    ticker = trade.get("ticker", "UNK")
    con_id = trade.get("ib_con_id")
    symbol = (trade.get("symbol") or "").replace(" ", "")

    # FIX A (docs/roll_close_bug_fixes.md): refresh openTrades() with orders
    # from every client in the account before querying. Entry manager places
    # brackets on clientId=3; exit manager queries on clientId=1. Without
    # this refresh, cross-client brackets are invisible and we leave them
    # alive on IB after the close.
    try:
        total_visible = client.refresh_all_open_orders()
        _trace(ticker, f"STEP 0: reqAllOpenOrders → {total_visible} trades "
                       "visible (cross-client merge)")
    except Exception as e:
        _trace(ticker, f"STEP 0: refresh_all_open_orders failed: {e}", "warn")

    _trace(ticker, f"STEP 1: Finding ALL open orders for conId={con_id} symbol={symbol}")

    # STEP 1: Find all open orders
    try:
        open_orders = client.find_open_orders_for_contract(con_id, symbol)
    except Exception as e:
        _trace(ticker, f"STEP 1: Could not query IB for open orders: {e}", "error")
        open_orders = []

    # STEP 2: Cancel all found orders
    if open_orders:
        target_ids = {o["orderId"] for o in open_orders}
        _trace(ticker, f"STEP 2: Found {len(open_orders)} order(s), cancelling all: "
               f"{[f'id={o['orderId']} type={o['orderType']} status={o['status']}' for o in open_orders]}")

        # STRICT TERMINAL-STATE CANCEL (docs/bracket_cancel_strict_verification.md)
        # --------------------------------------------------------------------
        # Only these statuses mean "definitely won't fill":
        TERMINAL_OK = {"Cancelled", "ApiCancelled", "Inactive", "Filled"}
        # 'PendingCancel' is NOT terminal — cancel-requested but can still
        # fill. IB sometimes reverts cancels (observed on MSFT 2026-04-20
        # where the preset-modified TIF caused reverts). Up to MAX_CANCEL_ROUNDS
        # rounds of: cancel + poll 3s. If orders revert to Submitted mid-poll,
        # retry. If after all rounds orders still aren't terminal, ABORT the
        # close — caller must NOT proceed to send the close SELL.
        MAX_CANCEL_ROUNDS = 3
        POLLS_PER_ROUND = 6
        POLL_SLEEP = 0.5

        # Phase 5: route cancels straight to the owning pool slot when we
        # know which one placed the order. Falls back to fan-out inside
        # cancel_order_by_id if the slot is missing or the attempt raises.
        preferred_cid = trade.get("ib_client_id")

        def _send_cancels(ids):
            for oid in ids:
                try:
                    client.cancel_order_by_id(oid, preferred_client_id=preferred_cid)
                    _trace(ticker, f"STEP 2: Cancel sent for orderId={oid}"
                                   f"{f' (preferred clientId={preferred_cid})' if preferred_cid else ''}")
                except Exception as e:
                    _trace(ticker, f"STEP 2: Failed to cancel orderId={oid}: {e}", "warn")

        _send_cancels(target_ids)

        for round_idx in range(MAX_CANCEL_ROUNDS):
            reverted: set[int] = set()
            for attempt in range(POLLS_PER_ROUND):
                time.sleep(POLL_SLEEP)
                try:
                    # Refresh view so cross-client state is current
                    client.refresh_all_open_orders()
                    remaining = client.find_open_orders_for_contract(con_id, symbol)
                except Exception:
                    continue

                # For each original target, determine its current status.
                # Absence from remaining = we'll treat as "gone" (terminal).
                remaining_by_id = {o["orderId"]: o for o in remaining
                                    if o["orderId"] in target_ids}
                non_terminal = [
                    o for o in remaining_by_id.values()
                    if o["status"] not in TERMINAL_OK
                ]

                if not non_terminal:
                    _trace(ticker,
                           f"STEP 3: All {len(target_ids)} order(s) "
                           f"TERMINAL (verified after round {round_idx + 1} / "
                           f"{(attempt + 1) * POLL_SLEEP:.1f}s)")
                    trade["ib_tp_order_id"] = None
                    trade["ib_sl_order_id"] = None
                    return True

                # Detect cancel reverts: order that left PendingCancel and
                # came back to Submitted / PreSubmitted. These need a fresh
                # cancel — IB didn't honor our first one.
                for o in non_terminal:
                    if o["status"] in ("Submitted", "PreSubmitted", "PendingSubmit"):
                        reverted.add(o["orderId"])

            if reverted:
                _trace(ticker,
                       f"STEP 3: IB REVERTED {len(reverted)} cancel(s): "
                       f"{sorted(reverted)} — round {round_idx + 1} / "
                       f"{MAX_CANCEL_ROUNDS}. Re-issuing cancels.",
                       "warn")
                _send_cancels(reverted)
                # Next round will re-poll
                continue

            # Still non-terminal (probably PendingCancel) without reverts.
            # Give IB more time in the next round.
            _trace(ticker,
                   f"STEP 3: {len(non_terminal)} order(s) still pending-cancel "
                   f"after round {round_idx + 1} / {MAX_CANCEL_ROUNDS}. Continuing.",
                   "warn")

        # Exhausted retries — orders still not terminal.
        _trace(ticker,
               f"STEP 3: ABORT — cancels did not reach terminal state after "
               f"{MAX_CANCEL_ROUNDS} rounds. Not sending close SELL. "
               f"Will retry on next exit cycle.",
               "error")
        return False

    # No orders found — check if brackets were expected (may have just fired)
    expected_brackets = bool(trade.get("ib_tp_order_id") or trade.get("ib_sl_order_id"))
    if expected_brackets:
        _trace(ticker, f"STEP 1: No orders found but brackets EXPECTED "
               f"(TP={trade.get('ib_tp_order_id')}, SL={trade.get('ib_sl_order_id')}). "
               f"Bracket may have JUST FIRED. Waiting 2s...", "warn")
        time.sleep(2)

        # Re-check position — if 0, bracket already closed it
        try:
            qty = client.get_position_quantity(con_id) if con_id else 0
            if qty == 0:
                _trace(ticker, "STEP 1 RESULT: Position now 0 — bracket fired. "
                       "No sell needed, will update DB only.")
                trade["_bracket_fired"] = True
                trade["ib_tp_order_id"] = None
                trade["ib_sl_order_id"] = None
                return True
            _trace(ticker, f"STEP 1 RESULT: Position still {qty} after wait — proceeding")
        except Exception:
            pass
    else:
        _trace(ticker, "STEP 1: No open orders found, no brackets expected — safe to proceed")

    trade["ib_tp_order_id"] = None
    trade["ib_sl_order_id"] = None
    return True


def get_ib_position_qty(client, trade: dict) -> int:
    """
    Check the ACTUAL position quantity on IB for this trade.
    Uses conId for exact matching. Returns 0 if can't verify (safe default).
    """
    ticker = trade.get("ticker", "UNK")
    con_id = trade.get("ib_con_id")

    if not con_id:
        log.warning(f"[{ticker}] CLOSE STEP 4: No conId — cannot verify position")
        return 0

    try:
        qty = client.get_position_quantity(con_id)
        log.info(f"[{ticker}] CLOSE STEP 4 RESULT: IB position qty={qty} for conId={con_id}")
        return qty
    except Exception as e:
        log.warning(f"[{ticker}] CLOSE STEP 4: Position check failed: {e}")
        return 0


def best_effort_cancel_brackets(client, trade: dict) -> None:
    """Fire cancel requests for any open bracket orders on this contract,
    WITHOUT blocking on terminal-state verification.

    Used in the "sell-first" close mode — we don't block the SELL on cancel
    completion because:
      1. IB auto-cancels resting TP/SL when position flattens anyway.
      2. Cross-client ownership (IB Error 10147) can make cancel-by-orderId
         fail when another pool client owns the bracket. Fire-and-forget
         lets the SELL proceed; ``verify_brackets_cleared_post_sell`` then
         handles any stragglers.
    """
    ticker = trade.get("ticker", "UNK")
    con_id = trade.get("ib_con_id")
    symbol = (trade.get("symbol") or "").replace(" ", "")
    try:
        client.refresh_all_open_orders()
        open_orders = client.find_open_orders_for_contract(con_id, symbol)
    except Exception as e:
        _trace(ticker, f"PRE-SELL cancel: query failed: {e}", "warn")
        return
    if not open_orders:
        return
    _trace(ticker, f"PRE-SELL cancel: firing best-effort cancel for "
                    f"{len(open_orders)} order(s) (non-blocking)")
    # Phase 5: prefer the pool slot that placed the entry.
    preferred_cid = trade.get("ib_client_id")
    for o in open_orders:
        try:
            client.cancel_order_by_id(o["orderId"], preferred_client_id=preferred_cid)
        except Exception as e:
            _trace(ticker, f"PRE-SELL cancel: orderId={o['orderId']} raised {e} "
                            "(ignoring, will verify post-SELL)", "warn")


def verify_brackets_cleared_post_sell(client, trade: dict, timeout: float) -> bool:
    """After MKT SELL, verify all bracket orders for this contract reach a
    terminal state. IB auto-cancels resting TP/SL when the position flattens,
    so usually this is a quick confirmation. If any remain alive past
    ``timeout``, issue an explicit cancel. Any still-alive after that raises
    a non-critical handle_error alert for the dashboard.

    Returns True if all clear, False if something stayed alive.
    """
    ticker = trade.get("ticker", "UNK")
    con_id = trade.get("ib_con_id")
    symbol = (trade.get("symbol") or "").replace(" ", "")
    TERMINAL_OK = {"Cancelled", "ApiCancelled", "Inactive", "Filled"}

    deadline = time.time() + timeout
    alive: list = []
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            client.refresh_all_open_orders()
            remaining = client.find_open_orders_for_contract(con_id, symbol)
        except Exception:
            continue
        alive = [o for o in remaining if o.get("status") not in TERMINAL_OK]
        if not alive:
            _trace(ticker, "POST-SELL verify: all brackets terminal "
                            "(IB auto-cancelled on position flat)")
            return True

    # Still alive — try explicit cancel, then one more poll.
    _trace(ticker, f"POST-SELL verify: {len(alive)} bracket(s) still alive after "
                    f"{timeout}s — issuing explicit cancel: "
                    f"{[o['orderId'] for o in alive]}", "warn")
    # Phase 5: prefer the pool slot that placed the entry.
    preferred_cid = trade.get("ib_client_id")
    for o in alive:
        try:
            client.cancel_order_by_id(o["orderId"], preferred_client_id=preferred_cid)
        except Exception as e:
            _trace(ticker, f"POST-SELL verify: explicit cancel orderId={o['orderId']} "
                            f"raised {e}", "warn")

    time.sleep(2.0)
    try:
        client.refresh_all_open_orders()
        remaining = client.find_open_orders_for_contract(con_id, symbol)
        alive = [o for o in remaining if o.get("status") not in TERMINAL_OK]
    except Exception:
        alive = []

    if alive:
        _trace(ticker, f"POST-SELL verify: {len(alive)} bracket(s) STILL ALIVE "
                        f"after explicit cancel — reconcile PASS 3/4 will clean up",
                        "error")
        try:
            from strategy.error_handler import handle_error
            handle_error(f"exit_executor-{ticker}", "brackets_alive_after_close",
                         RuntimeError(f"{len(alive)} brackets alive after SELL"),
                         context={"ticker": ticker, "symbol": symbol,
                                  "remaining": [o["orderId"] for o in alive]},
                         critical=False)
        except Exception:
            pass
        return False
    _trace(ticker, "POST-SELL verify: brackets terminal after explicit cancel")
    return True


def close_position_on_ib(client, trade: dict, max_qty: int) -> bool:
    """
    Send sell order to IB. Only sells up to max_qty contracts.
    Returns True if sell order was sent, False if skipped.
    """
    direction = trade.get("direction", "LONG")
    requested = trade.get("contracts", 0)
    symbol = trade.get("symbol", "")
    ticker = trade.get("ticker", "UNK")

    sell_qty = min(abs(requested), abs(max_qty))

    if sell_qty <= 0:
        log.warning(f"[{ticker}] CLOSE STEP 5: REFUSED — no position to sell "
                    f"(requested={requested}, IB qty={max_qty})")
        return False

    if sell_qty != abs(requested):
        log.warning(f"[{ticker}] CLOSE STEP 5: Reducing sell {abs(requested)} → {sell_qty} "
                    f"(IB only shows {abs(max_qty)} contracts)")

    # Use the trade's entry ref + '-close' suffix so the IB Order Ref
    # column on the close order traces back to the same strategy entry.
    close_ref = ((trade.get("client_trade_id") or "") + "-close") or None
    try:
        if direction == "SHORT":
            client.sell_put(symbol, sell_qty, order_ref=close_ref)
        else:
            client.sell_call(symbol, sell_qty, order_ref=close_ref)
        log.info(f"[{ticker}] CLOSE STEP 5 RESULT: Sell order sent — "
                 f"{sell_qty}x {symbol} ({direction}) ref={close_ref}")
        return True
    except Exception as e:
        log.error(f"[{ticker}] CLOSE STEP 5: FAILED to send sell: {e}")
        return False


def _execute_exit_sell_first(client, trade: dict, reason: str,
                              post_sell_timeout: float) -> float | None:
    """Sell-first close flow — works around IB's cross-client cancel asymmetry.

    Flow:
      1. Check IB position qty. If 0 → already closed (bracket fired / prior
         close), update DB only. If negative → critical alert, abort.
      2. Best-effort cancel brackets (fire-and-forget, non-blocking).
      3. Send MKT SELL.
      4. Post-SELL: verify brackets reach terminal (IB auto-cancels on flat).
      5. Post-SELL: verify IB position qty dropped to ≤ 0 (no naked short).
    """
    ticker = trade.get("ticker", "UNK")
    symbol = trade.get("symbol", "?")
    con_id = trade.get("ib_con_id")

    # Step 1: Position qty
    ib_qty = get_ib_position_qty(client, trade)
    if ib_qty == 0:
        # Position already closed. Still clean up any stale brackets.
        _trace(ticker, "SELL-FIRST: position already closed on IB (qty=0) — "
                       "will update DB only")
        expected_brackets = bool(trade.get("ib_tp_order_id") or
                                  trade.get("ib_sl_order_id"))
        if expected_brackets:
            trade["_bracket_fired"] = True
        trade["ib_tp_order_id"] = None
        trade["ib_sl_order_id"] = None
        return None

    if ib_qty < 0:
        # Same guard as legacy path — bot only supports long-options.
        log.error(f"[{ticker}] SELL-FIRST ABORTED — NEGATIVE position on IB "
                  f"(qty={ib_qty}). Manual intervention required.")
        from strategy.error_handler import handle_error
        handle_error(f"exit_executor-{ticker}", "negative_position_on_close",
                     RuntimeError(f"Direction={trade.get('direction')}, IB qty={ib_qty}"),
                     context={"ticker": ticker, "symbol": symbol,
                              "ib_qty": ib_qty, "con_id": con_id},
                     critical=True)
        return None

    # Step 2: Best-effort cancel (non-blocking).
    best_effort_cancel_brackets(client, trade)

    # Step 3: MKT SELL.
    sent = close_position_on_ib(client, trade, ib_qty)
    if not sent:
        _trace(ticker, "SELL-FIRST: SELL not sent — aborting", "error")
        return None

    # Step 4: Verify brackets terminal (IB should auto-cancel on position flat).
    verify_brackets_cleared_post_sell(client, trade, timeout=post_sell_timeout)

    # Step 5: Verify position didn't end up negative (simultaneous-fill guard).
    try:
        time.sleep(0.5)
        final_qty = client.get_position_quantity(con_id) if con_id else 0
    except Exception:
        final_qty = 0
    if final_qty < 0:
        log.error(f"[{ticker}] SELL-FIRST: position went NEGATIVE after SELL "
                  f"(qty={final_qty}) — likely simultaneous bracket+SELL fill. "
                  f"CRITICAL — manual review.")
        from strategy.error_handler import handle_error
        handle_error(f"exit_executor-{ticker}", "naked_short_after_close",
                     RuntimeError(f"Final IB qty={final_qty} after close SELL"),
                     context={"ticker": ticker, "symbol": symbol,
                              "final_qty": final_qty},
                     critical=True)

    trade["ib_tp_order_id"] = None
    trade["ib_sl_order_id"] = None
    _trace(ticker, f"SELL-FIRST EXIT COMPLETE — {reason}")
    return None


def _fetch_open_legs(trade_id: int) -> list[dict]:
    """Read every open leg for a trade from the DB. Used by the multi-leg
    exit path to know what needs to close. Returns a list of dicts with
    the fields the close orders need (symbol, sec_type, right, direction,
    contracts_open, entry_price, ib_con_id, ib_tp_perm_id, ib_sl_perm_id)."""
    try:
        from db.connection import get_session
        from sqlalchemy import text
        session = get_session()
        if session is None:
            return []
        rows = session.execute(text(
            "SELECT leg_id, leg_index, leg_role, sec_type, symbol, "
            '       "right", strike, expiry, direction, '
            "       contracts_open, entry_price, ib_con_id, "
            "       ib_tp_perm_id, ib_sl_perm_id "
            "FROM trade_legs "
            "WHERE trade_id = :tid AND leg_status = 'open' "
            "ORDER BY leg_index"
        ), {"tid": trade_id}).fetchall()
        session.close()
        return [
            {
                "leg_id": r[0], "leg_index": r[1], "leg_role": r[2],
                "sec_type": r[3], "symbol": r[4], "right": r[5],
                "strike": float(r[6]) if r[6] is not None else None,
                "expiry": r[7], "direction": r[8],
                "contracts_open": int(r[9] or 0),
                "entry_price": float(r[10] or 0),
                "ib_con_id": r[11],
                "ib_tp_perm_id": r[12], "ib_sl_perm_id": r[13],
            }
            for r in rows
        ]
    except Exception as e:
        log.warning(f"_fetch_open_legs(trade_id={trade_id}) failed: {e}")
        return []


def _close_action_for_leg(leg: dict) -> tuple[str, str] | None:
    """Return (method_name, description) for the IB call that closes
    this leg, or None if the leg is unsupported. Method names map to
    IBOrdersMixin: buy_call / buy_put / sell_call / sell_put.
    Stock legs are handled separately (not yet supported — flagged).

    direction='LONG' + right='C' → sell_call (SELL to close long call)
    direction='LONG' + right='P' → sell_put
    direction='SHORT' + right='C' → buy_call (BUY to close short call)
    direction='SHORT' + right='P' → buy_put
    """
    sec_type = (leg.get("sec_type") or "OPT").upper()
    direction = (leg.get("direction") or "").upper()
    # ENH-036: stock hedge legs (e.g. delta-neutral's stock overlay).
    if sec_type == "STK":
        if direction == "LONG":
            return ("sell_stock", "sell-to-close long stock hedge")
        if direction == "SHORT":
            return ("buy_stock", "buy-to-close short stock hedge")
        return None
    right = (leg.get("right") or "").upper()
    if direction == "LONG":
        if right == "C": return ("sell_call", "sell-to-close long call")
        if right == "P": return ("sell_put",  "sell-to-close long put")
    elif direction == "SHORT":
        if right == "C": return ("buy_call", "buy-to-close short call")
        if right == "P": return ("buy_put",  "buy-to-close short put")
    return None


def _execute_multi_leg_exit(client, trade: dict, reason: str) -> float | None:
    """Close every leg of a multi-leg trade (Phase 6b).

    Assumes multi-leg trades placed via ``place_multi_leg_order`` — each
    leg is its own IB order, typically in a shared OCA group. Close
    semantics:
      1. Fetch all open legs from ``trade_legs``.
      2. For each leg, determine the closing action based on direction
         + right (see _close_action_for_leg).
      3. Send each close order. Don't wait between — fire them all so
         the legs close as close-to-simultaneously as possible.
      4. Cancel any still-live TP/SL bracket on each leg by permId.
      5. Per-leg update in DB happens via finalize_close on the caller
         (exit_manager) using the trade's aggregated status.

    Simplification: currently uses sell_first-style best-effort. No
    retries, no post-verify per leg — OCA siblings should cancel
    themselves on first fill. If closes partially fill, the stuck legs
    will stay open and reconcile PASS 3 / the next exit cycle will
    surface them.

    Returns None. Caller uses trade['current_price'] for aggregate P&L
    the same way it does for single-leg today.
    """
    ticker = trade.get("ticker", "UNK")
    trade_id = trade.get("db_id")
    if trade_id is None:
        _trace(ticker, "MULTI-LEG EXIT: trade has no db_id — aborting", "error")
        return None

    legs = _fetch_open_legs(trade_id)
    if not legs:
        _trace(ticker, f"MULTI-LEG EXIT: no open legs for trade {trade_id} "
                        "— nothing to close (may already be flat)")
        return None

    _trace(ticker, f"MULTI-LEG EXIT START — reason={reason} db_id={trade_id} "
                   f"n_legs={len(legs)} "
                   f"legs=[{', '.join(l.get('leg_role') or l.get('symbol','?') for l in legs)}]")

    # ENH-046 Phase 2: prefer closing the combo with ONE reversed-Bag
    # order when the flag is on (read from DB settings so the user can
    # flip it at runtime from the dashboard). Falls back to per-leg
    # closes on any exception so this is a safe upgrade.
    import config as _cfg
    try:
        from db.settings_cache import get_bool
        use_combo = get_bool(
            "USE_COMBO_ORDERS_FOR_MULTI_LEG",
            default=bool(getattr(_cfg,
                                  "USE_COMBO_ORDERS_FOR_MULTI_LEG",
                                  False)),
        )
    except Exception:
        use_combo = bool(getattr(_cfg,
                                  "USE_COMBO_ORDERS_FOR_MULTI_LEG",
                                  False))
    if use_combo and hasattr(client, "place_combo_close_order"):
        # Shape the legs the way the broker method expects (flat dicts
        # with direction + contracts + symbol + leg metadata).
        combo_legs = [{
            "sec_type": l.get("sec_type") or "OPT",
            "symbol": l["symbol"],
            "direction": l.get("direction") or "LONG",
            "contracts": int(l.get("contracts_open") or 0),
            "strike": l.get("strike"),
            "right": l.get("right"),
            "expiry": l.get("expiry"),
            "multiplier": l.get("multiplier", 100),
            "exchange": l.get("exchange", "SMART"),
            "currency": l.get("currency", "USD"),
            "leg_role": l.get("leg_role"),
            "underlying": l.get("underlying") or ticker,
        } for l in legs if int(l.get("contracts_open") or 0) > 0]
        if combo_legs:
            try:
                close_ref = (trade.get("client_trade_id") or "") + "-close"
                result = client.place_combo_close_order(
                    combo_legs, order_ref=close_ref, limit_price=None,
                )
                if result and result.get("all_filled"):
                    _trace(ticker,
                           f"MULTI-LEG EXIT: combo close SUCCESS — "
                           f"ONE IB order (orderId={result.get('combo_order_id')}) "
                           f"closed all {len(combo_legs)} legs at "
                           f"net={result.get('net_fill_price'):+.2f}")
                    # Still best-effort cancel any stale brackets on legs.
                    for leg in legs:
                        for pid_key in ("ib_tp_perm_id", "ib_sl_perm_id"):
                            pid = leg.get(pid_key)
                            if not pid:
                                continue
                            try:
                                if hasattr(client, "cancel_order_by_perm_id"):
                                    client.cancel_order_by_perm_id(int(pid))
                            except Exception:
                                pass
                    return None
                else:
                    _trace(ticker,
                           f"MULTI-LEG EXIT: combo close partial/failed "
                           f"(status={result.get('legs') and result['legs'][0].get('status')}) "
                           f"— falling back to per-leg closes",
                           "warn")
            except Exception as e:
                _trace(ticker,
                       f"MULTI-LEG EXIT: combo close raised {type(e).__name__}: "
                       f"{e} — falling back to per-leg closes",
                       "warn")

    preferred_cid = trade.get("ib_client_id")
    sent = 0
    skipped = 0

    for leg in legs:
        action = _close_action_for_leg(leg)
        if action is None:
            _trace(ticker, f"MULTI-LEG EXIT: skipping leg {leg.get('leg_index')} "
                           f"({leg.get('sec_type')} {leg.get('direction')} "
                           f"right={leg.get('right')}) — no close path (STK or bad shape)",
                           "warn")
            skipped += 1
            continue
        method_name, desc = action
        method = getattr(client, method_name, None)
        if method is None:
            _trace(ticker, f"MULTI-LEG EXIT: client missing {method_name} — skipping leg "
                           f"{leg.get('leg_index')}", "warn")
            skipped += 1
            continue
        try:
            method(leg["symbol"], int(leg["contracts_open"]))
            _trace(ticker, f"MULTI-LEG EXIT: leg {leg.get('leg_index')} "
                            f"({leg.get('leg_role') or leg['symbol']}) → "
                            f"{desc} x{leg['contracts_open']}")
            sent += 1
        except Exception as e:
            _trace(ticker, f"MULTI-LEG EXIT: leg {leg.get('leg_index')} "
                            f"{method_name}({leg['symbol']}) raised: {e}",
                            "error")

        # Best-effort bracket cancel by permId (fire-and-forget).
        for pid_key in ("ib_tp_perm_id", "ib_sl_perm_id"):
            pid = leg.get(pid_key)
            if not pid:
                continue
            try:
                if hasattr(client, "cancel_order_by_perm_id"):
                    client.cancel_order_by_perm_id(int(pid))
            except Exception as e:
                _trace(ticker, f"MULTI-LEG EXIT: cancel permId={pid} failed: {e}", "warn")

    _trace(ticker, f"MULTI-LEG EXIT: {sent} close order(s) sent, "
                    f"{skipped} leg(s) skipped. "
                    f"Exit finalization handled by caller.")
    return None


def execute_exit(client, trade: dict, reason: str) -> float | None:
    """
    Full exit flow with detailed tracing:
    Step 1: Find and cancel ALL bracket orders for this contract
    Step 2: (done inside step 1)
    Step 3: Verify all brackets cancelled
    Step 4: Check IB position quantity
    Step 5: Send sell order (only if qty > 0)

    Returns None. Caller uses current_price for P&L.
    If any step fails, returns None and the trade stays open for retry.
    """
    ticker = trade.get("ticker", "UNK")
    db_id = trade.get("db_id", "?")
    symbol = trade.get("symbol", "?")
    con_id = trade.get("ib_con_id", "?")

    _trace(ticker, f"EXECUTE EXIT START — reason={reason} db_id={db_id} "
           f"symbol={symbol} conId={con_id} direction={trade.get('direction')} "
           f"contracts={trade.get('contracts')} "
           f"bracket TP={trade.get('ib_tp_order_id')} SL={trade.get('ib_sl_order_id')} "
           f"n_legs={trade.get('n_legs', 1)}")

    # Phase 6b: multi-leg trades (iron condors, hedged positions) branch
    # to a separate close path that iterates every leg and sends the
    # correct closing order per (direction, right, sec_type). Single-leg
    # trades (everything today) go through the same sell-first path as
    # before — zero behavior change.
    n_legs = int(trade.get("n_legs") or 1)
    if n_legs > 1:
        return _execute_multi_leg_exit(client, trade, reason)

    # Mode switch — sell-first (new default) vs cancel-first (legacy).
    # See config.CLOSE_MODE_SELL_FIRST and docs/ib_db_correlation.md.
    try:
        import config
        sell_first = getattr(config, "CLOSE_MODE_SELL_FIRST", True)
        post_sell_timeout = float(getattr(config, "POST_SELL_BRACKET_TIMEOUT", 5.0))
    except Exception:
        sell_first = True
        post_sell_timeout = 5.0

    if sell_first:
        return _execute_exit_sell_first(client, trade, reason, post_sell_timeout)

    # Legacy: Step 1-3: Cancel ALL orders for this contract and verify
    brackets_clear = cancel_all_orders_and_verify(client, trade)
    if not brackets_clear:
        log.error(f"[{ticker}] EXECUTE EXIT ABORTED — brackets not cleared")
        log.info(f"{'='*60}")
        return None

    # If bracket just fired (detected in step 1), skip sell entirely
    if trade.get("_bracket_fired"):
        _trace(ticker, "EXECUTE EXIT: Bracket already fired — position closed by IB. "
               "Skipping sell, will update DB only.")
        return None

    # Step 4: Check position
    ib_qty = get_ib_position_qty(client, trade)

    if ib_qty == 0:
        log.info(f"[{ticker}] EXECUTE EXIT: Position already closed on IB (qty=0) — "
                 f"will update DB only")
        log.info(f"{'='*60}")
        return None

    # FIX B (docs/roll_close_bug_fixes.md): the previous check was inverted
    # for ICT's convention where direction=SHORT means "bearish / long puts"
    # (we BUY puts, so ib_qty is POSITIVE, not negative). The bot currently
    # supports only long-options strategies — any active trade should have
    # ib_qty > 0 on IB regardless of direction. Any negative qty indicates
    # either a reconciliation bug or a naked-short position we can't close
    # with a simple SELL.
    if ib_qty < 0:
        log.error(f"[{ticker}] EXECUTE EXIT ABORTED — NEGATIVE position on IB "
                  f"(qty={ib_qty}). Bot only supports long-options strategies; "
                  f"a SELL here would widen the short. Manual intervention required.")
        from strategy.error_handler import handle_error
        handle_error(f"exit_executor-{ticker}", "negative_position_on_close",
                     RuntimeError(f"Direction={trade.get('direction')}, IB qty={ib_qty}"),
                     context={"ticker": ticker, "symbol": symbol,
                              "ib_qty": ib_qty, "con_id": con_id},
                     critical=True)
        log.info(f"{'='*60}")
        return None

    # Step 5: Sell
    close_position_on_ib(client, trade, ib_qty)

    _trace(ticker, f"EXECUTE EXIT COMPLETE — {reason}")
    return None


def execute_roll(client, trade: dict, pnl_pct: float):
    """
    Roll a trade: close current → open new at next strike.
    Uses execute_exit() for the close.
    """
    ticker = trade.get("ticker", "QQQ")
    direction = trade.get("direction", "LONG")

    log.info(f"[{ticker}] ROLL START: closing current position...")
    execute_exit(client, trade, reason=f"ROLL at {pnl_pct:+.0%}")

    # Verify closed
    remaining = get_ib_position_qty(client, trade)
    if remaining != 0:
        log.error(f"[{ticker}] ROLL ABORTED — position still has {remaining} contracts")
        return None

    # Open new position
    try:
        from strategy.option_selector import select_and_enter, select_and_enter_put
        if direction == "SHORT":
            rolled_trade = select_and_enter_put(client, ticker)
        else:
            rolled_trade = select_and_enter(client, ticker)

        if rolled_trade:
            # Guard: same-strike roll is degenerate churn. Observed on SPY
            # 2026-04-21: the selector re-picked the very same 710P we were
            # rolling out of, producing a close→open→close loop because
            # exit_manager's _verify_close_on_ib polled the old conId and
            # saw the new position sitting on it. Close the duplicate new
            # entry immediately and return None so the caller treats this
            # as a plain exit. No churn, no loop.
            if rolled_trade.get("symbol") == trade.get("symbol"):
                log.warning(
                    f"[{ticker}] ROLL ABORTED — new entry at SAME symbol "
                    f"{rolled_trade['symbol']} as the trade being rolled. "
                    f"Treating as plain exit; closing duplicate position."
                )
                try:
                    execute_exit(client, rolled_trade,
                                  reason="SAME_STRIKE_ROLL_REVERT")
                except Exception as e:
                    log.error(f"[{ticker}] failed to revert same-strike roll: {e}")
                return None
            rolled_trade["signal"] = f"ROLL from {trade['symbol']}"
            rolled_trade["_rolled_from"] = trade["symbol"]
            log.info(f"[{ticker}] ROLL COMPLETE: → {rolled_trade['symbol']} @ ${rolled_trade['entry_price']:.2f}")
            return rolled_trade
        else:
            log.warning(f"[{ticker}] ROLL: old position closed but new entry failed")
            return None
    except Exception as e:
        log.error(f"[{ticker}] ROLL: old position closed but new entry failed: {e}")
        return None
