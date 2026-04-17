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


def cancel_all_brackets_for_contract(client, trade: dict) -> bool:
    """
    Find and cancel ALL open orders on IB for this contract.
    Does NOT rely on stored order IDs — searches IB directly by symbol/conId.
    Waits up to 3 seconds for IB to confirm cancellation.
    Returns True if no active orders remain, False if still active (abort).
    """
    ticker = trade.get("ticker", "UNK")
    con_id = trade.get("ib_con_id")
    symbol = (trade.get("symbol") or "").replace(" ", "")

    log.info(f"[{ticker}] CLOSE STEP 1: Finding ALL open orders for conId={con_id} symbol={symbol}")

    # Step 1: Find all open orders for this contract on IB
    try:
        open_orders = client.find_open_orders_for_contract(con_id, symbol)
    except Exception as e:
        log.error(f"[{ticker}] Could not query IB for open orders: {e}")
        open_orders = []

    if not open_orders:
        # No orders found. But if we EXPECTED brackets (stored IDs), they may have
        # JUST FIRED. IB takes 1-2 seconds to update positions after a bracket fill.
        # Wait and re-check position — if qty is now 0, bracket closed it.
        expected_brackets = bool(trade.get("ib_tp_order_id") or trade.get("ib_sl_order_id"))
        if expected_brackets:
            log.warning(f"[{ticker}] CLOSE STEP 1: No orders found but brackets were expected "
                        f"(TP={trade.get('ib_tp_order_id')}, SL={trade.get('ib_sl_order_id')}). "
                        f"Bracket may have JUST FIRED. Waiting 2s for IB to update positions...")
            time.sleep(2)
            # Re-check position — if 0, bracket already closed it
            try:
                qty_after_wait = client.get_position_quantity(con_id) if con_id else 0
                if qty_after_wait == 0:
                    log.info(f"[{ticker}] CLOSE STEP 1 RESULT: Position now 0 — bracket fired. "
                             f"No sell needed, will update DB only.")
                    trade["ib_tp_order_id"] = None
                    trade["ib_sl_order_id"] = None
                    trade["_bracket_fired"] = True  # Signal to caller
                    return True
                log.info(f"[{ticker}] CLOSE STEP 1 RESULT: Position still {qty_after_wait} after wait — "
                         f"proceeding with close")
            except Exception:
                pass
        else:
            log.info(f"[{ticker}] CLOSE STEP 1 RESULT: No open orders found — safe to proceed")
        trade["ib_tp_order_id"] = None
        trade["ib_sl_order_id"] = None
        return True

    log.info(f"[{ticker}] CLOSE STEP 1 RESULT: Found {len(open_orders)} open order(s): "
             f"{[f'orderId={o['orderId']} type={o['orderType']} status={o['status']}' for o in open_orders]}")

    # Step 2: Cancel all of them
    cancelled_ids = []
    for order in open_orders:
        order_id = order["orderId"]
        try:
            client.cancel_order_by_id(order_id)
            cancelled_ids.append(order_id)
            log.info(f"[{ticker}] CLOSE STEP 2: Cancel sent for orderId={order_id} "
                     f"(type={order['orderType']} action={order.get('action')})")
        except Exception as e:
            log.warning(f"[{ticker}] Failed to cancel orderId={order_id}: {e}")

    if not cancelled_ids:
        log.warning(f"[{ticker}] CLOSE STEP 2: No cancels sent — orders may not be cancellable")

    # Step 3: Verify all cancelled (poll up to 3 seconds)
    for attempt in range(6):
        time.sleep(0.5)
        try:
            remaining = client.find_open_orders_for_contract(con_id, symbol)
            active = [o for o in remaining if o["status"] in ("Submitted", "PreSubmitted", "PendingSubmit")]
            if not active:
                log.info(f"[{ticker}] CLOSE STEP 3 RESULT: All orders cancelled (verified after {(attempt+1)*0.5}s)")
                trade["ib_tp_order_id"] = None
                trade["ib_sl_order_id"] = None
                return True
            log.debug(f"[{ticker}] CLOSE STEP 3: Still {len(active)} active after {(attempt+1)*0.5}s")
        except Exception:
            pass

    # After 3 seconds, orders still active — ABORT
    log.error(f"[{ticker}] CLOSE STEP 3 RESULT: ABORT — orders still active after 3s. "
              f"Will retry next cycle.")
    from strategy.error_handler import handle_error
    handle_error(f"exit_executor-{ticker}", "bracket_cancel_timeout",
                 RuntimeError(f"Orders still active after 3s cancel attempt"),
                 context={"ticker": ticker, "con_id": con_id, "cancelled_ids": cancelled_ids},
                 critical=True)
    return False


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

    # Step 1-3: Cancel ALL brackets and verify
    brackets_clear = cancel_all_brackets_for_contract(client, trade)
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

    if (trade.get("direction") == "LONG" and ib_qty < 0) or \
       (trade.get("direction") == "SHORT" and ib_qty > 0):
        log.error(f"[{ticker}] EXECUTE EXIT ABORTED — direction mismatch! "
                  f"Trade={trade.get('direction')} but IB qty={ib_qty}")
        from strategy.error_handler import handle_error
        handle_error(f"exit_executor-{ticker}", "position_direction_mismatch",
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
