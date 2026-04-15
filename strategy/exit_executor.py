"""
Exit Executor — handles the mechanics of closing a trade on IB.
Implements the cancel-brackets → verify-position → sell flow.
"""
import logging
from datetime import datetime
import pytz

log = logging.getLogger(__name__)
PT = pytz.timezone("America/Los_Angeles")


def cancel_bracket_orders(client, trade: dict) -> bool:
    """
    Cancel bracket TP and SL legs on IB.
    Returns True if cancellation was sent, False if no brackets.
    """
    tp_id = trade.get("ib_tp_order_id")
    sl_id = trade.get("ib_sl_order_id")
    ids = [i for i in [tp_id, sl_id] if i]

    if not ids:
        return False

    try:
        client.cancel_bracket_children(*ids)
        trade["ib_tp_order_id"] = None
        trade["ib_sl_order_id"] = None
        log.info(f"[{trade.get('ticker')}] Cancelled bracket orders (TP={tp_id}, SL={sl_id})")
        return True
    except Exception as e:
        log.warning(f"[{trade.get('ticker')}] Failed to cancel brackets: {e}")
        return False


def verify_position_exists(client, trade: dict) -> bool:
    """
    Check if the position still exists on IB.
    Returns True if position is still open, False if already closed.
    """
    try:
        ib_positions = client.get_ib_positions_raw()
        sym_clean = trade["symbol"].replace(" ", "")
        for p in ib_positions:
            p_sym = p.get("symbol", "").replace(" ", "")
            if p_sym == sym_clean and abs(p.get("qty", 0)) > 0:
                return True
        log.info(f"[{trade.get('ticker')}] Position not found on IB — already closed")
        return False
    except Exception as e:
        log.warning(f"[{trade.get('ticker')}] Could not verify IB position: {e}")
        return True  # Assume open if can't check — safer to try to close


def close_position_on_ib(client, trade: dict):
    """Send sell order to IB to close the position."""
    direction = trade.get("direction", "LONG")
    contracts = trade["contracts"]
    symbol = trade["symbol"]
    ticker = trade.get("ticker", "UNK")

    try:
        if direction == "SHORT":
            client.sell_put(symbol, contracts)
        else:
            client.sell_call(symbol, contracts)
        log.info(f"[{ticker}] Sell order sent: {contracts}x {symbol}")
    except Exception as e:
        log.error(f"[{ticker}] Failed to send sell order: {e}")


def execute_exit(client, trade: dict, reason: str) -> float | None:
    """
    Full exit flow: cancel brackets → verify position → close.
    Returns the current price used for exit, or None if position was already closed.

    This is the SINGLE function that closes trades. No other code should
    send sell orders directly.
    """
    ticker = trade.get("ticker", "UNK")
    log.info(f"[{ticker}] Executing exit: {reason}")

    # Step 1: Cancel bracket orders (if any)
    has_brackets = bool(trade.get("ib_tp_order_id") or trade.get("ib_sl_order_id"))
    if has_brackets:
        cancel_bracket_orders(client, trade)

    # Step 2: Verify position still exists on IB
    position_open = verify_position_exists(client, trade)

    # Step 3: Close if still open
    if position_open:
        close_position_on_ib(client, trade)
        return None  # Caller should use the current_price they already have
    else:
        # Position already closed — bracket fired before our cancel arrived
        log.info(f"[{ticker}] Position was closed by bracket order — updating DB only")
        return None


def execute_roll(client, trade: dict, pnl_pct: float):
    """
    Roll a trade: close current position, then open new one at next strike.
    Uses execute_exit() for the close — same cancel-brackets → verify → sell flow.
    Returns the new trade dict or None if roll failed.

    Sequence:
    1. Close current trade via execute_exit() (cancels brackets, verifies, sells)
    2. Open new trade via select_and_enter() (finds new strike, places bracket order)

    If step 1 fails, abort — don't open a new position.
    If step 2 fails, the old position is closed but no new one opened (logged as error).
    """
    ticker = trade.get("ticker", "QQQ")
    direction = trade.get("direction", "LONG")

    # Step 1: Close current trade using the SAME close function everyone uses
    log.info(f"[{ticker}] Rolling: closing current position first...")
    execute_exit(client, trade, reason=f"ROLL at {pnl_pct:+.0%}")

    # Verify the position is actually closed before opening new one
    if verify_position_exists(client, trade):
        log.error(f"[{ticker}] Roll aborted — position still open after exit attempt")
        return None

    # Step 2: Open new position at next strike
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
            log.warning(f"[{ticker}] Roll: old position closed but new entry failed — no new position")
            return None
    except Exception as e:
        log.error(f"[{ticker}] Roll: old position closed but new entry failed: {e}")
        return None
