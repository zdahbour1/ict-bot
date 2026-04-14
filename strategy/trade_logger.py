"""
Trade Logger — CSV and DB logging for closed trades.
Extracted from exit_manager to keep it focused on trade lifecycle.
"""
import logging
import csv
import os
from datetime import datetime
import pytz
import threading

from strategy.indicators import compute_snapshot
import config

log = logging.getLogger(__name__)
PT = pytz.timezone("America/Los_Angeles")
LOGS_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")
_csv_lock = threading.Lock()


def collect_exit_enrichment(client, trade: dict) -> dict:
    """Collect exit-time enrichment data (indicators, Greeks, VIX)."""
    enrichment = {"exit_time": datetime.now(PT).isoformat()}
    ticker = trade.get("ticker", "QQQ")

    try:
        from data.ib_provider import get_bars_1m_ib
        exit_bars = get_bars_1m_ib(client, ticker, days_back=2)
        enrichment["exit_indicators"] = compute_snapshot(exit_bars)
    except Exception as e:
        enrichment["exit_indicators"] = {}

    try:
        enrichment["exit_stock_price"] = client.get_realtime_equity_price(ticker)
    except Exception as e:
        enrichment["exit_stock_price"] = None

    try:
        enrichment["exit_greeks"] = client.get_option_greeks(trade["symbol"])
    except Exception as e:
        enrichment["exit_greeks"] = {}

    try:
        enrichment["exit_vix"] = client.get_vix()
    except Exception as e:
        enrichment["exit_vix"] = None

    return enrichment


def log_trade_result(trade: dict, exit_price: float, result: str, reason: str,
                     exit_enrichment: dict = None):
    """Log a closed trade to console and CSV file."""
    pnl_pct = (exit_price - trade["entry_price"]) / trade["entry_price"] * 100
    pnl_usd = (exit_price - trade["entry_price"]) * 100 * trade["contracts"]
    ticker = trade.get("ticker", "UNK")

    log.info(
        f"{'=' * 50}\n"
        f"[{ticker}] TRADE CLOSED — {result} ({reason})\n"
        f"Symbol:  {trade['symbol']}\n"
        f"Entry:   ${trade['entry_price']:.2f}\n"
        f"Exit:    ${exit_price:.2f}\n"
        f"P&L:     {pnl_pct:+.1f}%  (${pnl_usd:+.2f})\n"
        f"{'=' * 50}"
    )

    if exit_enrichment is None:
        exit_enrichment = {}

    # Extract enrichment data
    ei = trade.get("entry_indicators", {})
    eg = trade.get("entry_greeks", {})
    xi = exit_enrichment.get("exit_indicators", {})
    xg = exit_enrichment.get("exit_greeks", {})

    header = [
        "entry_time", "exit_time", "ticker", "symbol", "direction", "contracts",
        "entry_price", "exit_price", "pnl_pct", "pnl_usd", "result", "reason",
        "entry_stock_price", "exit_stock_price",
        "entry_delta", "entry_gamma", "entry_theta", "entry_vega",
        "exit_delta", "exit_gamma", "exit_theta", "exit_vega",
        "entry_vwap", "exit_vwap", "entry_vix", "exit_vix",
        "entry_rsi_14", "exit_rsi_14",
        "entry_sma_7", "entry_sma_10", "entry_sma_20", "entry_sma_50",
        "exit_sma_7", "exit_sma_10", "exit_sma_20", "exit_sma_50",
        "entry_ema_7", "entry_ema_10", "entry_ema_20", "entry_ema_50",
        "exit_ema_7", "exit_ema_10", "exit_ema_20", "exit_ema_50",
        "entry_macd_line", "entry_macd_signal", "entry_macd_histogram",
        "exit_macd_line", "exit_macd_signal", "exit_macd_histogram",
    ]

    row = [
        trade.get("entry_time"), exit_enrichment.get("exit_time"),
        ticker, trade["symbol"], trade.get("direction", "LONG"), trade["contracts"],
        trade["entry_price"], exit_price,
        round(pnl_pct, 2), round(pnl_usd, 2), result, reason,
        trade.get("entry_stock_price"), exit_enrichment.get("exit_stock_price"),
        eg.get("delta"), eg.get("gamma"), eg.get("theta"), eg.get("vega"),
        xg.get("delta"), xg.get("gamma"), xg.get("theta"), xg.get("vega"),
        ei.get("vwap"), xi.get("vwap"),
        trade.get("entry_vix"), exit_enrichment.get("exit_vix"),
        ei.get("rsi_14"), xi.get("rsi_14"),
        ei.get("sma_7"), ei.get("sma_10"), ei.get("sma_20"), ei.get("sma_50"),
        xi.get("sma_7"), xi.get("sma_10"), xi.get("sma_20"), xi.get("sma_50"),
        ei.get("ema_7"), ei.get("ema_10"), ei.get("ema_20"), ei.get("ema_50"),
        xi.get("ema_7"), xi.get("ema_10"), xi.get("ema_20"), xi.get("ema_50"),
        ei.get("macd_line"), ei.get("macd_signal"), ei.get("macd_histogram"),
        xi.get("macd_line"), xi.get("macd_signal"), xi.get("macd_histogram"),
    ]

    _write_csv(header, row)


def close_trade_in_db(trade: dict, exit_price: float, result: str, reason: str,
                      exit_enrichment: dict = None):
    """Update trade status to closed in the database."""
    if not trade.get("db_id"):
        log.warning(f"Trade {trade.get('ticker')} has no db_id — cannot update DB")
        return

    try:
        from db.writer import close_trade as db_close_trade
        db_close_trade(trade["db_id"], exit_price, result, reason, exit_enrichment or {})
        log.info(f"Trade closed in DB: id={trade['db_id']} {result} ({reason})")
    except Exception as e:
        log.warning(f"DB close_trade failed for id={trade.get('db_id')}: {e}")


def _write_csv(header: list, row: list):
    """Write trade result to daily CSV file (thread-safe)."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    account = config.IB_ACCOUNT or "unknown"
    today_str = datetime.now(PT).strftime("%Y%m%d")

    daily_file = None
    for fname in sorted(os.listdir(LOGS_DIR)):
        if fname.startswith(f"{account}_{today_str}") and fname.endswith(".csv"):
            daily_file = os.path.join(LOGS_DIR, fname)
            break

    if daily_file is None:
        timestamp = datetime.now(PT).strftime("%Y%m%d_%H%M%S")
        daily_file = os.path.join(LOGS_DIR, f"{account}_{timestamp}.csv")

    with _csv_lock:
        write_header = not os.path.exists(daily_file)
        with open(daily_file, "a", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(header)
            writer.writerow(row)
        log.info(f"Trade logged to {daily_file}")
