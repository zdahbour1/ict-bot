"""
Option Selector
Decides WHICH option to buy when an ICT signal fires.
Uses IB real-time data, validates contracts, and places bracket orders.
"""
import logging
import config

log = logging.getLogger(__name__)


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

    # ── Extract fill price ────────────────────────────────
    if isinstance(order_result, dict) and order_result.get("fill_price", 0) > 0:
        entry_price = order_result["fill_price"]
        log.info(f"[{ticker}] Actual IB fill price: ${entry_price:.2f} (quote was ${pre_quote:.2f})")
        # Recalculate TP/SL based on actual fill
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

    if isinstance(order_result, dict) and order_result.get("fill_price", 0) > 0:
        entry_price = order_result["fill_price"]
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
