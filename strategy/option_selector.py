"""
Option Selector
Decides WHICH option to buy when an ICT signal fires.
Currently: ATM call, 0DTE, QQQ.
"""
import logging
from broker.tastytrade_client import TastytradeClient
import config

log = logging.getLogger(__name__)


def select_and_enter(client: TastytradeClient) -> dict | None:
    """
    Called when a bullish ICT signal is detected.
    1. Finds the ATM 0DTE QQQ call
    2. Places the buy order
    3. Returns trade info dict for the exit manager to monitor

    Returns None if entry was skipped (e.g. outside trading hours).
    """
    import pytz
    from datetime import datetime

    # ── Time filter: only trade 07:00–09:00 Pacific Time ─────────
    pt = pytz.timezone("America/Los_Angeles")
    now_pt = datetime.now(pt)
    if not (config.TRADE_WINDOW_START_PT <= now_pt.hour < config.TRADE_WINDOW_END_PT):
        log.info(f"Signal received at {now_pt.strftime('%H:%M')} PT — outside trading window. Skipped.")
        return None

    log.info("Signal received inside trading window — entering trade...")

    # ── Find ATM 0DTE call ────────────────────────────────
    option_symbol = client.get_atm_call_symbol(config.TICKER)

    # ── Get entry price before placing order ──────────────
    entry_price = client.get_option_price(option_symbol)
    log.info(f"Entry price: ${entry_price:.2f} per contract")

    # ── Place order ───────────────────────────────────────
    client.buy_call(option_symbol, config.CONTRACTS)

    # ── Return trade info for exit manager ────────────────
    trade = {
        "symbol":       option_symbol,
        "contracts":    config.CONTRACTS,
        "entry_price":  entry_price,
        "profit_target": entry_price * (1 + config.PROFIT_TARGET),   # +25%
        "stop_loss":     entry_price * (1 - config.STOP_LOSS),        # -15%
        "entry_time":   now_pt.isoformat(),
    }
    log.info(
        f"Trade opened: {option_symbol} | "
        f"Entry: ${entry_price:.2f} | "
        f"TP: ${trade['profit_target']:.2f} | "
        f"SL: ${trade['stop_loss']:.2f}"
    )
    return trade


def select_and_enter_put(client: TastytradeClient) -> dict | None:
    """
    Called when a bearish ICT signal is detected.
    1. Finds the ATM 0DTE QQQ put
    2. Places the buy order
    3. Returns trade info dict for the exit manager to monitor
    """
    import pytz
    from datetime import datetime

    pt = pytz.timezone("America/Los_Angeles")
    now_pt = datetime.now(pt)
    if not (config.TRADE_WINDOW_START_PT <= now_pt.hour < config.TRADE_WINDOW_END_PT):
        log.info(f"SHORT signal at {now_pt.strftime('%H:%M')} PT — outside trading window. Skipped.")
        return None

    log.info("SHORT signal inside trading window — entering PUT trade...")

    option_symbol = client.get_atm_put_symbol(config.TICKER)
    entry_price   = client.get_option_price(option_symbol)
    log.info(f"PUT entry price: ${entry_price:.2f} per contract")

    client.buy_put(option_symbol, config.CONTRACTS)

    trade = {
        "symbol":        option_symbol,
        "contracts":     config.CONTRACTS,
        "entry_price":   entry_price,
        "profit_target": entry_price * (1 + config.PROFIT_TARGET),
        "stop_loss":     entry_price * (1 - config.STOP_LOSS),
        "entry_time":    now_pt.isoformat(),
        "direction":     "SHORT",
    }
    log.info(
        f"PUT trade opened: {option_symbol} | "
        f"Entry: ${entry_price:.2f} | "
        f"TP: ${trade['profit_target']:.2f} | "
        f"SL: ${trade['stop_loss']:.2f}"
    )
    return trade
