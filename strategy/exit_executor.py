"""
Exit Executor — handles the mechanics of closing a trade on IB.

SAFETY RULE: Before ANY sell order, verify the position exists on IB
with positive quantity. NEVER sell more than we hold. This prevents
naked short positions from accidental double-closes.

Single close function: execute_exit(). Everything uses it —
manual close, TP, SL, trailing, rolling, EOD, reconciliation.
"""
import logging
from datetime import datetime
import pytz

log = logging.getLogger(__name__)
PT = pytz.timezone("America/Los_Angeles")


def cancel_bracket_orders(client, trade: dict) -> bool:
    """
    Cancel bracket TP and SL legs on IB and VERIFY they are cancelled.
    Waits up to 3 seconds for IB to confirm cancellation.
    Returns True if brackets were cancelled (or didn't exist).
    """
    tp_id = trade.get("ib_tp_order_id")
    sl_id = trade.get("ib_sl_order_id")
    ids = [i for i in [tp_id, sl_id] if i]
    if not ids:
        return True  # No brackets to cancel

    ticker = trade.get("ticker", "UNK")
    try:
        client.cancel_bracket_children(*ids)
        log.info(f"[{ticker}] Cancel request sent for brackets (TP={tp_id}, SL={sl_id})")
    except Exception as e:
        log.warning(f"[{ticker}] Failed to send cancel for brackets: {e}")
        # Continue anyway — brackets may not exist anymore

    # VERIFY cancellation: wait up to 3 seconds for IB to process
    import time
    for attempt in range(6):  # 6 × 0.5s = 3 seconds
        time.sleep(0.5)
        try:
            still_active = client.check_bracket_orders_active(trade)
            if not still_active:
                trade["ib_tp_order_id"] = None
                trade["ib_sl_order_id"] = None
                log.info(f"[{ticker}] Bracket orders confirmed cancelled")
                return True
        except Exception:
            pass

    # After 3 seconds, brackets STILL active — ABORT. Do NOT proceed.
    # Assume nothing: if we can't confirm cancellation, it's not safe to sell.
    log.error(f"[{ticker}] Bracket cancel NOT confirmed after 3s — "
              f"ABORTING close. Will retry on next cycle.")
    from strategy.error_handler import handle_error
    handle_error(f"exit_executor-{ticker}", "bracket_cancel_timeout",
                 RuntimeError(f"Bracket orders still active after 3s cancel attempt"),
                 context={"ticker": ticker, "tp_id": tp_id, "sl_id": sl_id},
                 critical=True)
    return False  # Caller must check return value and abort if False


def get_ib_position_qty(client, trade: dict) -> int:
    """
    Check the ACTUAL position quantity on IB for this trade.
    Returns: positive for long, 0 if no position, negative if short.

    Uses conId for exact matching (no symbol string issues).
    Returns 0 if conId not available or IB query fails (safe default).
    """
    con_id = trade.get("ib_con_id")
    if not con_id:
        # No conId — try to look up by symbol as fallback
        log.warning(f"[{trade.get('ticker')}] No conId — cannot verify position precisely")
        return _fallback_position_check(client, trade)

    try:
        qty = client.get_position_quantity(con_id)
        return qty
    except Exception as e:
        log.warning(f"[{trade.get('ticker')}] Position check failed for conId={con_id}: {e}")
        return 0  # Safe default — don't sell if can't verify


def _fallback_position_check(client, trade: dict) -> int:
    """Fallback position check using symbol matching when conId not available."""
    try:
        positions = client.get_ib_positions_raw()
        sym_clean = trade["symbol"].replace(" ", "")
        for p in positions:
            p_sym = p.get("symbol", "").replace(" ", "")
            if p_sym == sym_clean:
                return int(p.get("qty", 0))
    except Exception as e:
        log.warning(f"[{trade.get('ticker')}] Fallback position check failed: {e}")
    return 0


def close_position_on_ib(client, trade: dict, max_qty: int) -> bool:
    """
    Send sell order to IB. Only sells up to max_qty contracts.

    SAFETY: Caller must provide max_qty from get_ib_position_qty().
    This function will NOT sell more than the verified quantity.
    Returns True if sell order was sent, False if skipped.
    """
    direction = trade.get("direction", "LONG")
    requested = trade.get("contracts", 0)
    symbol = trade["symbol"]
    ticker = trade.get("ticker", "UNK")

    # Never sell more than what IB shows we hold
    sell_qty = min(abs(requested), abs(max_qty))

    if sell_qty <= 0:
        log.warning(f"[{ticker}] SAFETY: Refusing to sell — no position on IB "
                    f"(requested={requested}, IB qty={max_qty})")
        return False

    if sell_qty != abs(requested):
        log.warning(f"[{ticker}] SAFETY: Reducing sell from {abs(requested)} to {sell_qty} "
                    f"(IB only shows {abs(max_qty)} contracts)")

    try:
        if direction == "SHORT":
            client.sell_put(symbol, sell_qty)
        else:
            client.sell_call(symbol, sell_qty)
        log.info(f"[{ticker}] Sell order sent: {sell_qty}x {symbol}")
        return True
    except Exception as e:
        log.error(f"[{ticker}] Failed to send sell order: {e}")
        return False


def execute_exit(client, trade: dict, reason: str) -> float | None:
    """
    Full exit flow: cancel brackets → check position → close.
    Returns None. Caller uses current_price for P&L calculation.

    This is the SINGLE function that closes trades. No other code
    should send sell orders directly.

    SAFETY: Checks IB position quantity before selling. Will NOT sell
    if position is already closed (qty=0) or negative.
    """
    ticker = trade.get("ticker", "UNK")
    log.info(f"[{ticker}] Executing exit: {reason}")

    # Step 1: Cancel bracket orders and VERIFY cancelled
    has_brackets = bool(trade.get("ib_tp_order_id") or trade.get("ib_sl_order_id"))
    if has_brackets:
        brackets_cancelled = cancel_bracket_orders(client, trade)
        if not brackets_cancelled:
            # Brackets still active — ABORT. Do not proceed to sell.
            # Will retry on the next exit_manager cycle.
            log.error(f"[{ticker}] ABORTING exit — brackets not confirmed cancelled")
            return None

    # Step 2: Check ACTUAL position on IB (after brackets confirmed cancelled)
    ib_qty = get_ib_position_qty(client, trade)

    if ib_qty == 0:
        # Position already closed — bracket fired before us, or already exited
        log.info(f"[{ticker}] Position already closed on IB (qty=0) — updating DB only")
        return None

    if (trade.get("direction") == "LONG" and ib_qty < 0) or \
       (trade.get("direction") == "SHORT" and ib_qty > 0):
        # Position is in the wrong direction — something is very wrong
        log.error(f"[{ticker}] CRITICAL: Position direction mismatch! "
                  f"Trade says {trade.get('direction')} but IB qty={ib_qty}. "
                  f"NOT selling to avoid making it worse.")
        from strategy.error_handler import handle_error
        handle_error(f"exit_executor-{ticker}", "position_direction_mismatch",
                     RuntimeError(f"Direction mismatch: trade={trade.get('direction')}, IB qty={ib_qty}"),
                     context={"ticker": ticker, "symbol": trade.get("symbol"),
                              "ib_qty": ib_qty, "con_id": trade.get("ib_con_id")},
                     critical=True)
        return None

    # Step 3: Close — only sell what we actually hold
    close_position_on_ib(client, trade, ib_qty)
    return None


def execute_roll(client, trade: dict, pnl_pct: float):
    """
    Roll a trade: close current position, then open new one at next strike.
    Uses execute_exit() for the close — same safety checks apply.

    Sequence:
    1. execute_exit() — cancels brackets, checks position, sells
    2. Verify position is closed on IB
    3. Open new trade at next strike

    If any step fails, abort. Never open new position if old one is still open.
    """
    ticker = trade.get("ticker", "QQQ")
    direction = trade.get("direction", "LONG")

    # Step 1: Close current trade
    log.info(f"[{ticker}] Rolling: closing current position first...")
    execute_exit(client, trade, reason=f"ROLL at {pnl_pct:+.0%}")

    # Step 2: Verify the position is actually closed
    remaining = get_ib_position_qty(client, trade)
    if remaining != 0:
        log.error(f"[{ticker}] Roll aborted — position still has {remaining} contracts after exit")
        return None

    # Step 3: Open new position
    try:
        from strategy.option_selector import select_and_enter, select_and_enter_put
        if direction == "SHORT":
            rolled_trade = select_and_enter_put(client, ticker)
        else:
            rolled_trade = select_and_enter(client, ticker)

        if rolled_trade:
            rolled_trade["signal"] = f"ROLL from {trade['symbol']}"
            rolled_trade["_rolled_from"] = trade["symbol"]
            log.info(f"[{ticker}] Rolled to {rolled_trade['symbol']} @ ${rolled_trade['entry_price']:.2f}")
            return rolled_trade
        else:
            log.warning(f"[{ticker}] Roll: old position closed but new entry failed")
            return None
    except Exception as e:
        log.error(f"[{ticker}] Roll: old position closed but new entry failed: {e}")
        return None
