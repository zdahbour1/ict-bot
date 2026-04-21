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

        def _send_cancels(ids):
            for oid in ids:
                try:
                    client.cancel_order_by_id(oid)
                    _trace(ticker, f"STEP 2: Cancel sent for orderId={oid}")
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
    for o in open_orders:
        try:
            client.cancel_order_by_id(o["orderId"])
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
    for o in alive:
        try:
            client.cancel_order_by_id(o["orderId"])
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

    try:
        if direction == "SHORT":
            client.sell_put(symbol, sell_qty)
        else:
            client.sell_call(symbol, sell_qty)
        log.info(f"[{ticker}] CLOSE STEP 5 RESULT: Sell order sent — "
                 f"{sell_qty}x {symbol} ({direction})")
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
           f"bracket TP={trade.get('ib_tp_order_id')} SL={trade.get('ib_sl_order_id')}")

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
