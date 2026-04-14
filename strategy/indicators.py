"""
Technical Indicators — pure pandas functions for OHLCV bars.
No IB or broker dependency. All functions return scalars or dicts.
"""
import logging
import pandas as pd
import numpy as np

log = logging.getLogger(__name__)


def rsi(bars: pd.DataFrame, period: int = 14) -> float | None:
    """Wilder RSI on close prices. Returns last value."""
    if bars.empty or len(bars) < period + 1:
        return None
    close = bars["close"]
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs = gain / loss
    result = 100 - (100 / (1 + rs))
    val = result.iloc[-1]
    return round(float(val), 2) if not np.isnan(val) else None


def sma(bars: pd.DataFrame, period: int) -> float | None:
    """Simple moving average of close. Returns last value."""
    if bars.empty or len(bars) < period:
        return None
    val = bars["close"].rolling(period).mean().iloc[-1]
    return round(float(val), 4) if not np.isnan(val) else None


def ema(bars: pd.DataFrame, period: int) -> float | None:
    """Exponential moving average of close. Returns last value."""
    if bars.empty or len(bars) < period:
        return None
    val = bars["close"].ewm(span=period, adjust=False).mean().iloc[-1]
    return round(float(val), 4) if not np.isnan(val) else None


def macd(bars: pd.DataFrame, fast: int = 12, slow: int = 26, signal_period: int = 9) -> dict:
    """MACD(12,26,9). Returns {macd_line, macd_signal, macd_histogram}."""
    if bars.empty or len(bars) < slow + signal_period:
        return {"macd_line": None, "macd_signal": None, "macd_histogram": None}
    close = bars["close"]
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    macd_signal = macd_line.ewm(span=signal_period, adjust=False).mean()
    macd_hist = macd_line - macd_signal
    def _safe(val):
        v = float(val.iloc[-1])
        return round(v, 4) if not np.isnan(v) else None
    return {
        "macd_line": _safe(macd_line),
        "macd_signal": _safe(macd_signal),
        "macd_histogram": _safe(macd_hist),
    }


def vwap(bars: pd.DataFrame) -> float | None:
    """
    Intraday VWAP. Filters to current trading day, computes
    cumsum(typical_price * volume) / cumsum(volume).
    """
    if bars.empty or "volume" not in bars.columns:
        return None
    try:
        # Use last trading day's bars
        last_date = bars.index[-1].date()
        day_bars = bars[bars.index.date == last_date]
        if day_bars.empty or day_bars["volume"].sum() == 0:
            return None
        typical_price = (day_bars["high"] + day_bars["low"] + day_bars["close"]) / 3
        cum_tp_vol = (typical_price * day_bars["volume"]).cumsum()
        cum_vol = day_bars["volume"].cumsum()
        vwap_series = cum_tp_vol / cum_vol
        val = vwap_series.iloc[-1]
        return round(float(val), 4) if not np.isnan(val) else None
    except Exception as e:
        logging.getLogger(__name__).debug(f"VWAP calculation failed: {e}")
        return None


def compute_snapshot(bars_1m: pd.DataFrame) -> dict:
    """
    Compute all technical indicators from 1-minute bars.
    Returns a flat dict with all indicator values (None if insufficient data).
    """
    if bars_1m is None or bars_1m.empty:
        return {
            "rsi_14": None,
            "sma_7": None, "sma_10": None, "sma_20": None, "sma_50": None,
            "ema_7": None, "ema_10": None, "ema_20": None, "ema_50": None,
            "macd_line": None, "macd_signal": None, "macd_histogram": None,
            "vwap": None,
        }

    macd_vals = macd(bars_1m)

    return {
        "rsi_14": rsi(bars_1m, 14),
        "sma_7": sma(bars_1m, 7),
        "sma_10": sma(bars_1m, 10),
        "sma_20": sma(bars_1m, 20),
        "sma_50": sma(bars_1m, 50),
        "ema_7": ema(bars_1m, 7),
        "ema_10": ema(bars_1m, 10),
        "ema_20": ema(bars_1m, 20),
        "ema_50": ema(bars_1m, 50),
        "macd_line": macd_vals["macd_line"],
        "macd_signal": macd_vals["macd_signal"],
        "macd_histogram": macd_vals["macd_histogram"],
        "vwap": vwap(bars_1m),
    }
