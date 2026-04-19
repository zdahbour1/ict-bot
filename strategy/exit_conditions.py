"""
Exit Conditions — pure functions that evaluate whether a trade should exit.
No IB calls, no DB writes. Just logic.
"""
import logging
from datetime import datetime
import pytz
import config

log = logging.getLogger(__name__)
PT = pytz.timezone("America/Los_Angeles")


def update_trailing_stop(trade: dict, pnl_pct: float) -> float:
    """Update trailing stop based on peak P&L milestones. Returns new SL level."""
    peak = trade["peak_pnl_pct"]
    steps = int(peak / 0.10)
    if steps > 0:
        trail_base = steps * 0.10
        return trail_base - config.STOP_LOSS
    return trade["dynamic_sl_pct"]


def check_tp_to_trail(trade: dict, pnl_pct: float, entry_price: float) -> bool:
    """Check if TP should convert to trailing stop. Returns True if converted."""
    if not config.TP_TO_TRAIL:
        return False
    if pnl_pct >= config.PROFIT_TARGET and not trade.get("_tp_trailed"):
        trade["dynamic_sl_pct"] = config.PROFIT_TARGET - config.STOP_LOSS
        trade["_tp_trailed"] = True
        log.info(f"[{trade.get('ticker')}] TP-TO-TRAIL: TP hit at {pnl_pct:+.1%} — "
                 f"converting SL to trail at {trade['dynamic_sl_pct']:+.0%}")
        # Log to system_log for dashboard visibility
        try:
            from db.writer import add_system_log
            ticker = trade.get('ticker', 'UNK')
            add_system_log(f"exit_conditions-{ticker}", "info",
                          f"TP-TO-TRAIL: P&L={pnl_pct:+.1%}, new SL={trade['dynamic_sl_pct']:+.0%}")
        except Exception:
            pass
        return True
    return False


def check_roll_condition(trade: dict, pnl_pct: float) -> bool:
    """Check if trade should be rolled to next strike."""
    if (config.ROLL_ENABLED and not trade.get("_rolled")
            and pnl_pct >= config.ROLL_THRESHOLD * config.PROFIT_TARGET):
        log.info(f"[{trade.get('ticker')}] Roll trigger at {pnl_pct:+.1%}")
        trade["_rolled"] = True
        trade["_should_roll"] = True
        return True
    return False


def evaluate_exit(trade: dict, current_price: float, now_pt: datetime) -> dict | None:
    """
    Evaluate all exit conditions for a trade.
    Returns dict with 'result' and 'reason' if exit triggered, None otherwise.
    """
    entry_price = trade["entry_price"]
    if entry_price <= 0:
        return None

    pnl_pct = (current_price - entry_price) / entry_price

    # Update peak
    if pnl_pct > trade["peak_pnl_pct"]:
        trade["peak_pnl_pct"] = pnl_pct

    # Update trailing stop
    old_sl = trade["dynamic_sl_pct"]
    trade["dynamic_sl_pct"] = update_trailing_stop(trade, pnl_pct)

    # Check TP → trail conversion
    tp_converted = check_tp_to_trail(trade, pnl_pct, entry_price)

    # Check roll
    should_roll = check_roll_condition(trade, pnl_pct)

    # Time exit — use the injected `now_pt` so backtests can drive the
    # clock from bar timestamps instead of wall-clock.
    entry_time = trade.get("entry_time")
    bars_held = 0
    if entry_time:
        bars_held = (now_pt - entry_time).total_seconds() / 60
    time_exit = bars_held >= 90

    # EOD exit
    eod_exit = now_pt.hour >= 13

    # TP/SL
    hit_tp = False
    if not config.TP_TO_TRAIL:
        hit_tp = pnl_pct >= config.PROFIT_TARGET
    hit_sl = pnl_pct <= trade["dynamic_sl_pct"]

    # Determine exit — reason is a STANDARD CATEGORY for analytics GROUP BY
    # reason_detail has the variable data for drill-down analysis
    sl_changed = old_sl != trade["dynamic_sl_pct"]
    if should_roll:
        return {"result": "WIN", "reason": "ROLL",
                "reason_detail": f"P&L={pnl_pct:+.0%}",
                "pnl_pct": pnl_pct, "sl_changed": sl_changed, "should_roll": True}
    elif hit_tp:
        return {"result": "WIN", "reason": "TP",
                "reason_detail": f"P&L={pnl_pct:+.0%}",
                "pnl_pct": pnl_pct, "sl_changed": sl_changed}
    elif hit_sl and trade["dynamic_sl_pct"] > -config.STOP_LOSS:
        result = "WIN" if pnl_pct > 0 else "LOSS" if pnl_pct < 0 else "SCRATCH"
        return {"result": result, "reason": "TRAIL_STOP",
                "reason_detail": f"SL={trade['dynamic_sl_pct']:+.0%} P&L={pnl_pct:+.0%}",
                "pnl_pct": pnl_pct, "sl_changed": sl_changed}
    elif hit_sl:
        return {"result": "LOSS", "reason": "SL",
                "reason_detail": f"P&L={pnl_pct:+.0%}",
                "pnl_pct": pnl_pct, "sl_changed": sl_changed}
    elif time_exit:
        result = "WIN" if pnl_pct > 0 else "LOSS" if pnl_pct < 0 else "SCRATCH"
        return {"result": result, "reason": "TIME_EXIT",
                "reason_detail": f"90min P&L={pnl_pct:+.0%}",
                "pnl_pct": pnl_pct, "sl_changed": sl_changed}
    elif eod_exit:
        result = "WIN" if pnl_pct > 0 else "LOSS"
        return {"result": result, "reason": "EOD_EXIT",
                "reason_detail": f"P&L={pnl_pct:+.0%}",
                "pnl_pct": pnl_pct, "sl_changed": sl_changed}

    # No exit — return info for monitoring
    return None
