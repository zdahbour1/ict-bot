"""
Option Selector
Decides WHICH option to buy when an ICT signal fires.
Uses IB real-time data, validates contracts, and places bracket orders.
"""
import logging
import config

log = logging.getLogger(__name__)

# IB order statuses that mean the order FAILED — never create a trade for these
FAILED_STATUSES = {"Cancelled", "Inactive", "ApiCancelled", "ApiPending", "PendingCancel"}

# IB order statuses that mean the order is working — safe to track
WORKING_STATUSES = {"Filled", "Submitted", "PreSubmitted"}


def _trigger_orphan_scan_fast_path(client, ticker: str, option_symbol: str,
                                    order_result: dict) -> None:
    """Immediate orphan scan when IB error 201 indicates stale brackets.

    IB error 201 ("Cannot have open orders on both sides of the same US
    Option contract") has TWO possible causes:

      (a) ORPHANS — SELL orders left over from a closed trade. The
          intended target of this fast-path.
      (b) LEGITIMATE DUPLICATE — an existing open trade on the same
          contract is running its bracket, and we just tried to stack
          another BUY on top. Cancelling those SELLs would strip the
          live trade of its TP/SL. DANGEROUS.

    We discriminate by checking the DB: if there's an open trade with
    the same conId, we SKIP the fast-path entirely. The 201 is
    legitimate — whoever fired the duplicate entry is the one with
    the bug, not IB.

    When we DO cancel, we target by permId (globally unique across IB
    clients) rather than orderId (which is per-client and can collide).
    Audit messages reflect the ACTUAL cancelled order's symbol/price,
    not the rejected entry's — important when orderIds collide across
    stale sessions.
    """
    con_id = order_result.get("con_id")
    if not con_id:
        log.warning(f"[{ticker}] Orphan fast-path: no conId on order_result — skip")
        return

    # GUARD: don't run the fast-path if an open DB trade lives on this
    # contract. Its bracket is legitimate, not orphaned.
    try:
        from db.connection import get_session
        from sqlalchemy import text
        session = get_session()
        has_open_trade = False
        if session is not None:
            row = session.execute(
                text("SELECT id FROM trades "
                     "WHERE ib_con_id = :cid AND status = 'open' LIMIT 1"),
                {"cid": con_id},
            ).fetchone()
            session.close()
            has_open_trade = row is not None
        if has_open_trade:
            log.info(
                f"[{ticker}] Orphan fast-path: SKIPPED — open DB trade exists "
                f"on conId={con_id}. Error 201 is legitimate (duplicate entry "
                f"guard). Not touching any SELL orders on this contract."
            )
            return
    except Exception as e:
        # If the DB check fails we prefer the SAFE default — skip, not act.
        log.warning(f"[{ticker}] Orphan fast-path: DB guard query failed: {e} "
                    f"— skipping to stay safe")
        return

    try:
        client.refresh_all_open_orders()
        orders = client.find_open_orders_for_contract(con_id, option_symbol)
    except Exception as e:
        log.warning(f"[{ticker}] Orphan fast-path: query failed: {e}")
        return

    # Candidate orphans: SELL bracket children still working
    candidates = [
        o for o in orders
        if o.get("action") == "SELL"
        and o.get("status") in ("Submitted", "PreSubmitted", "PendingSubmit")
        and (o.get("parentId") or 0) != 0  # bracket children only — never touch standalone SELLs
    ]
    if not candidates:
        log.info(f"[{ticker}] Orphan fast-path: no SELL bracket children found on "
                 f"{option_symbol} — 201 may be stale; no action")
        return

    log.warning(f"[{ticker}] Orphan fast-path: IB error 201 on {option_symbol} "
                f"— cancelling {len(candidates)} stale SELL order(s): "
                f"{[(o.get('orderId'), o.get('permId')) for o in candidates]}")

    from strategy.audit import log_trade_action
    for o in candidates:
        order_id = o.get("orderId")
        perm_id = o.get("permId")
        # Each candidate order has its OWN symbol/price — use that in
        # the audit, not the rejected-entry's option_symbol. OrderIds
        # aren't unique across clients; permId is.
        actual_symbol = o.get("symbol") or option_symbol
        actual_price = o.get("lmtPrice") or o.get("auxPrice")
        try:
            client.cancel_order_by_id(order_id)
        except Exception as e:
            log.warning(f"[{ticker}] Orphan fast-path: cancel of "
                        f"orderId={order_id} permId={perm_id} failed: {e}")
        log_trade_action(
            None, "cancel_orphan_bracket", "option_selector",
            f"IB error 201 fast-path: cancelled orderId={order_id} "
            f"permId={perm_id} on {actual_symbol} "
            f"({o.get('orderType')} @ ${actual_price})",
            level="warn",
            extra={
                "ticker":    ticker,
                "orderId":   order_id,
                "permId":    perm_id,
                "conId":     con_id,
                "symbol":    actual_symbol,         # actual cancelled order's symbol
                "rejected_entry_symbol": option_symbol,  # for context
                "orderType": o.get("orderType"),
                "trigger":   "ib_error_201_fast_path",
                "price_level": actual_price,
                "clientId":  o.get("clientId"),
            },
        )


def select_and_enter(client, ticker: str = "QQQ") -> dict | None:
    """
    Called when a bullish ICT signal is detected.
    1. Finds the ATM 0DTE call (IB validated)
    2. Gets real-time quote from IB
    3. Places bracket order (market + TP limit + SL stop) or simple market order
    4. Uses actual IB fill price as entry
    """
    import pytz
    from datetime import datetime

    pt = pytz.timezone("America/Los_Angeles")
    now_pt = datetime.now(pt)
    if not (config.TRADE_WINDOW_START_PT <= now_pt.hour < config.TRADE_WINDOW_END_PT):
        log.info(f"[{ticker}] Signal received at {now_pt.strftime('%H:%M')} PT — outside trading window. Skipped.")
        return None

    contracts = config.CONTRACTS_PER_TICKER.get(ticker, config.CONTRACTS)
    log.info(f"[{ticker}] Signal received inside trading window — entering trade...")

    # ── Find ATM 0DTE call (contract validated on IB) ─────
    option_symbol = client.get_atm_call_symbol(ticker)

    # ── Validate contract exists before ordering ──────────
    if not client.validate_contract(option_symbol):
        log.error(f"[{ticker}] Contract validation FAILED for {option_symbol} — order NOT placed")
        return None

    # ── Get IB real-time quote ────────────────────────────
    pre_quote = client.get_option_price(option_symbol)
    log.info(f"[{ticker}] IB pre-order quote: ${pre_quote:.2f} per contract")

    # ── Place order (bracket or simple) ───────────────────
    tp_price = round(pre_quote * (1 + config.PROFIT_TARGET), 2)
    sl_price = round(pre_quote * (1 - config.STOP_LOSS), 2)

    if config.USE_BRACKET_ORDERS:
        order_result = client.place_bracket_order(
            option_symbol, contracts, "BUY", tp_price, sl_price
        )
    else:
        order_result = client.buy_call(option_symbol, contracts)

    # ── Verify order result ─────────────────────────────────
    if order_result is None:
        log.error(f"[{ticker}] Order placement returned None — trade NOT opened")
        return None

    if not isinstance(order_result, dict):
        log.error(f"[{ticker}] Order placement returned unexpected type: {type(order_result)}")
        return None

    order_status = order_result.get("status", "unknown")

    # ── Gate on order status — NEVER create a trade for failed orders ──
    if order_result.get("dry_run"):
        pass  # Dry run always proceeds
    elif order_status in FAILED_STATUSES:
        ib_error = order_result.get("ib_error", {})
        error_detail = f"code={ib_error.get('code')} {ib_error.get('message')}" if ib_error else "no IB error details"
        log.error(f"[{ticker}] Order REJECTED — status='{order_status}' ({error_detail}). "
                  f"Trade NOT opened.")

        # FAST-PATH: IB error 201 ("Cannot have open orders on both sides
        # of the same US Option contract") is strong evidence of orphaned
        # SELL brackets from a prior trade. Trigger the orphan detector
        # immediately without the usual 60s grace period — we have direct
        # evidence something's wrong. See docs/orphan_bracket_detector.md.
        if ib_error.get("code") == 201:
            try:
                _trigger_orphan_scan_fast_path(
                    client, ticker, option_symbol, order_result
                )
            except Exception as e:
                log.warning(f"[{ticker}] Fast-path orphan scan failed: {e}")

        try:
            from strategy.error_handler import handle_error
            handle_error(f"option_selector-{ticker}", "order_rejected",
                         RuntimeError(f"IB order status '{order_status}': {error_detail}"),
                         context={"ticker": ticker, "symbol": option_symbol,
                                  "status": order_status, "ib_error": ib_error,
                                  "order_id": order_result.get("order_id")},
                         critical=True)
        except Exception:
            pass
        return None
    elif order_status not in WORKING_STATUSES:
        log.error(f"[{ticker}] Order returned unexpected status '{order_status}' — "
                  f"refusing to create trade. Trade NOT opened.")
        try:
            from strategy.error_handler import handle_error
            handle_error(f"option_selector-{ticker}", "order_unknown_status",
                         RuntimeError(f"Unexpected IB order status '{order_status}'"),
                         context={"ticker": ticker, "symbol": option_symbol,
                                  "status": order_status,
                                  "order_id": order_result.get("order_id")},
                         critical=True)
        except Exception:
            pass
        return None
    elif order_status in ("Submitted", "PreSubmitted"):
        log.warning(f"[{ticker}] Order status '{order_status}' — not yet filled. "
                    f"Will track with pre-order quote as entry.")

    # ── Extract fill price ────────────────────────────────
    fill_price = order_result.get("fill_price", 0)
    if fill_price and fill_price > 0:
        entry_price = fill_price
        log.info(f"[{ticker}] Actual IB fill price: ${entry_price:.2f} (quote was ${pre_quote:.2f})")
        tp_price = round(entry_price * (1 + config.PROFIT_TARGET), 2)
        sl_price = round(entry_price * (1 - config.STOP_LOSS), 2)
    else:
        entry_price = pre_quote
        log.info(f"[{ticker}] Using pre-order quote as entry: ${entry_price:.2f}")

    trade = {
        "ticker":       ticker,
        "symbol":       option_symbol,
        "contracts":    contracts,
        "entry_price":  entry_price,
        "profit_target": tp_price,
        "stop_loss":     sl_price,
        "entry_time":   now_pt,
    }

    # Store IB IDs for reconciliation and bracket management
    if isinstance(order_result, dict):
        trade["ib_order_id"] = order_result.get("order_id")
        trade["ib_perm_id"] = order_result.get("perm_id")
        trade["ib_con_id"] = order_result.get("con_id")
        trade["ib_tp_order_id"] = order_result.get("tp_order_id")
        trade["ib_tp_perm_id"] = order_result.get("tp_perm_id")
        trade["ib_sl_order_id"] = order_result.get("sl_order_id")
        trade["ib_sl_perm_id"] = order_result.get("sl_perm_id")

    log.info(
        f"[{ticker}] Trade opened: {option_symbol} | "
        f"Entry: ${entry_price:.2f} | TP: ${tp_price:.2f} | SL: ${sl_price:.2f}"
        f"{' [BRACKET]' if config.USE_BRACKET_ORDERS else ''}"
    )
    return trade


def select_and_enter_put(client, ticker: str = "QQQ") -> dict | None:
    """
    Called when a bearish ICT signal is detected.
    Same as select_and_enter but for puts.
    """
    import pytz
    from datetime import datetime

    pt = pytz.timezone("America/Los_Angeles")
    now_pt = datetime.now(pt)
    if not (config.TRADE_WINDOW_START_PT <= now_pt.hour < config.TRADE_WINDOW_END_PT):
        log.info(f"[{ticker}] SHORT signal at {now_pt.strftime('%H:%M')} PT — outside trading window. Skipped.")
        return None

    contracts = config.CONTRACTS_PER_TICKER.get(ticker, config.CONTRACTS)
    log.info(f"[{ticker}] SHORT signal inside trading window — entering PUT trade...")

    option_symbol = client.get_atm_put_symbol(ticker)

    if not client.validate_contract(option_symbol):
        log.error(f"[{ticker}] Contract validation FAILED for {option_symbol} — order NOT placed")
        return None

    pre_quote = client.get_option_price(option_symbol)
    log.info(f"[{ticker}] IB pre-order PUT quote: ${pre_quote:.2f} per contract")

    tp_price = round(pre_quote * (1 + config.PROFIT_TARGET), 2)
    sl_price = round(pre_quote * (1 - config.STOP_LOSS), 2)

    if config.USE_BRACKET_ORDERS:
        order_result = client.place_bracket_order(
            option_symbol, contracts, "BUY", tp_price, sl_price
        )
    else:
        order_result = client.buy_put(option_symbol, contracts)

    # ── Verify order result ─────────────────────────────────
    if order_result is None:
        log.error(f"[{ticker}] PUT order placement returned None — trade NOT opened")
        return None

    if not isinstance(order_result, dict):
        log.error(f"[{ticker}] PUT order placement returned unexpected type: {type(order_result)}")
        return None

    order_status = order_result.get("status", "unknown")

    # ── Gate on order status — NEVER create a trade for failed orders ──
    if order_result.get("dry_run"):
        pass  # Dry run always proceeds
    elif order_status in FAILED_STATUSES:
        ib_error = order_result.get("ib_error", {})
        error_detail = f"code={ib_error.get('code')} {ib_error.get('message')}" if ib_error else "no IB error details"
        log.error(f"[{ticker}] PUT order REJECTED — status='{order_status}' ({error_detail}). "
                  f"Trade NOT opened.")

        # FAST-PATH: same as the CALL branch — IB error 201 means
        # orphaned brackets on this contract. Trigger immediate
        # orphan scan rather than waiting for periodic reconcile.
        if ib_error.get("code") == 201:
            try:
                _trigger_orphan_scan_fast_path(
                    client, ticker, option_symbol, order_result
                )
            except Exception as e:
                log.warning(f"[{ticker}] Fast-path orphan scan failed: {e}")

        try:
            from strategy.error_handler import handle_error
            handle_error(f"option_selector-{ticker}", "put_order_rejected",
                         RuntimeError(f"IB PUT order status '{order_status}': {error_detail}"),
                         context={"ticker": ticker, "symbol": option_symbol,
                                  "status": order_status, "ib_error": ib_error,
                                  "order_id": order_result.get("order_id")},
                         critical=True)
        except Exception:
            pass
        return None
    elif order_status not in WORKING_STATUSES:
        log.error(f"[{ticker}] PUT order returned unexpected status '{order_status}' — "
                  f"refusing to create trade. Trade NOT opened.")
        try:
            from strategy.error_handler import handle_error
            handle_error(f"option_selector-{ticker}", "put_order_unknown_status",
                         RuntimeError(f"Unexpected IB PUT order status '{order_status}'"),
                         context={"ticker": ticker, "symbol": option_symbol,
                                  "status": order_status,
                                  "order_id": order_result.get("order_id")},
                         critical=True)
        except Exception:
            pass
        return None
    elif order_status in ("Submitted", "PreSubmitted"):
        log.warning(f"[{ticker}] PUT order status '{order_status}' — not yet filled. "
                    f"Will track with pre-order quote as entry.")

    fill_price = order_result.get("fill_price", 0)
    if fill_price and fill_price > 0:
        entry_price = fill_price
        log.info(f"[{ticker}] Actual IB fill price: ${entry_price:.2f} (quote was ${pre_quote:.2f})")
        tp_price = round(entry_price * (1 + config.PROFIT_TARGET), 2)
        sl_price = round(entry_price * (1 - config.STOP_LOSS), 2)
    else:
        entry_price = pre_quote
        log.info(f"[{ticker}] Using pre-order quote as entry: ${entry_price:.2f}")

    trade = {
        "ticker":        ticker,
        "symbol":        option_symbol,
        "contracts":     contracts,
        "entry_price":   entry_price,
        "profit_target": tp_price,
        "stop_loss":     sl_price,
        "entry_time":    now_pt,
        "direction":     "SHORT",
    }

    if isinstance(order_result, dict):
        trade["ib_order_id"] = order_result.get("order_id")
        trade["ib_perm_id"] = order_result.get("perm_id")
        trade["ib_con_id"] = order_result.get("con_id")
        trade["ib_tp_order_id"] = order_result.get("tp_order_id")
        trade["ib_tp_perm_id"] = order_result.get("tp_perm_id")
        trade["ib_sl_order_id"] = order_result.get("sl_order_id")
        trade["ib_sl_perm_id"] = order_result.get("sl_perm_id")

    log.info(
        f"[{ticker}] PUT trade opened: {option_symbol} | "
        f"Entry: ${entry_price:.2f} | TP: ${tp_price:.2f} | SL: ${sl_price:.2f}"
        f"{' [BRACKET]' if config.USE_BRACKET_ORDERS else ''}"
    )
    return trade
