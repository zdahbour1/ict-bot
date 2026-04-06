"""
Option Selector
Decides WHICH option to buy when an ICT signal fires.
Supports any ticker with ATM 0DTE options.
"""
import logging
import config

log = logging.getLogger(__name__)


def select_and_enter(client, ticker: str = "QQQ") -> dict | None:
    """
    Called when a bullish ICT signal is detected.
    1. Finds the ATM 0DTE call for the given ticker
    2. Places the buy order
    3. Returns trade info dict for the exit manager to monitor

    Returns None if entry was skipped (e.g. outside trading hours).
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

    # ── Find ATM 0DTE call ────────────────────────────────
    option_symbol = client.get_atm_call_symbol(ticker)

    # ── Get entry price before placing order ──────────────
    entry_price = client.get_option_price(option_symbol)
    log.info(f"[{ticker}] Entry price: ${entry_price:.2f} per contract")

    # ── Place order ───────────────────────────────────────
    client.buy_call(option_symbol, contracts)

    # ── Return trade info for exit manager ────────────────
    trade = {
        "ticker":       ticker,
        "symbol":       option_symbol,
        "contracts":    contracts,
        "entry_price":  entry_price,
        "profit_target": entry_price * (1 + config.PROFIT_TARGET),
        "stop_loss":     entry_price * (1 - config.STOP_LOSS),
        "entry_time":   now_pt,
    }
    log.info(
        f"[{ticker}] Trade opened: {option_symbol} | "
        f"Entry: ${entry_price:.2f} | "
        f"TP: ${trade['profit_target']:.2f} | "
        f"SL: ${trade['stop_loss']:.2f}"
    )
    return trade


def select_and_enter_put(client, ticker: str = "QQQ") -> dict | None:
    """
    Called when a bearish ICT signal is detected.
    1. Finds the ATM 0DTE put for the given ticker
    2. Places the buy order
    3. Returns trade info dict for the exit manager to monitor
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
    entry_price   = client.get_option_price(option_symbol)
    log.info(f"[{ticker}] PUT entry price: ${entry_price:.2f} per contract")

    client.buy_put(option_symbol, contracts)

    trade = {
        "ticker":        ticker,
        "symbol":        option_symbol,
        "contracts":     contracts,
        "entry_price":   entry_price,
        "profit_target": entry_price * (1 + config.PROFIT_TARGET),
        "stop_loss":     entry_price * (1 - config.STOP_LOSS),
        "entry_time":    now_pt,
        "direction":     "SHORT",
    }
    log.info(
        f"[{ticker}] PUT trade opened: {option_symbol} | "
        f"Entry: ${entry_price:.2f} | "
        f"TP: ${trade['profit_target']:.2f} | "
        f"SL: ${trade['stop_loss']:.2f}"
    )
    return trade
