"""
Per-trade indicator snapshot — captures features at entry and exit
so downstream analysis can correlate outcomes with market context.

Every value is a native Python scalar (not numpy) so JSONB can
serialize it without coercion. NaN and inf are filtered out.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd


def _clean(v) -> Optional[float | int | str | bool]:
    """Make a value JSON-safe (for JSONB storage)."""
    if v is None:
        return None
    if isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        if math.isnan(v) or math.isinf(v):
            return None
        return float(v)
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.isoformat()
    return str(v)


# ── Per-bar indicators ──────────────────────────────────────

def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


def vwap(bars: pd.DataFrame) -> pd.Series:
    """Intraday VWAP reset per trading day."""
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3.0
    vol_price = typical * bars["volume"]
    day = bars.index.normalize()
    cum_vp = vol_price.groupby(day).cumsum()
    cum_v = bars["volume"].groupby(day).cumsum()
    return cum_vp / cum_v.replace(0, 1e-10)


def atr(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = bars["close"].shift(1)
    tr = pd.concat([
        bars["high"] - bars["low"],
        (bars["high"] - prev_close).abs(),
        (bars["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def sma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(period).mean()


def bollinger_width(close: pd.Series, period: int = 20, std: float = 2.0) -> pd.Series:
    mean = close.rolling(period).mean()
    sd = close.rolling(period).std()
    return (mean + std * sd - (mean - std * sd)) / mean  # width / price


# ── Snapshot builders ───────────────────────────────────────

def snapshot_at(bars: pd.DataFrame, bar_idx: int, *,
                vix_series: Optional[pd.Series] = None) -> dict:
    """Return the indicator dict at bar index `bar_idx`.

    Skips any indicator that hasn't warmed up at that index.
    """
    if bars is None or bars.empty or bar_idx < 0 or bar_idx >= len(bars):
        return {}

    out: dict = {}

    # Cheap scalars
    bar = bars.iloc[bar_idx]
    out["price"] = _clean(bar.get("close"))
    out["volume"] = _clean(bar.get("volume"))

    ts = bars.index[bar_idx]
    out["bar_time"] = _clean(ts)
    out["bar_of_day"] = _clean(int(ts.hour * 60 + ts.minute))
    out["day_of_week"] = _clean(ts.day_name())
    out["minute_of_hour"] = _clean(int(ts.minute))

    # Indicator series — compute once on the full bars, then sample
    close = bars["close"]
    try:
        out["rsi_14"] = _clean(rsi(close, 14).iloc[bar_idx])
    except Exception:
        out["rsi_14"] = None
    try:
        out["atr_14"] = _clean(atr(bars, 14).iloc[bar_idx])
    except Exception:
        out["atr_14"] = None
    try:
        out["sma_20"] = _clean(sma(close, 20).iloc[bar_idx])
        out["sma_50"] = _clean(sma(close, 50).iloc[bar_idx])
    except Exception:
        out["sma_20"] = out["sma_50"] = None
    try:
        v = vwap(bars).iloc[bar_idx]
        out["vwap"] = _clean(v)
        if v and bar.get("close"):
            out["price_vs_vwap_pct"] = _clean((bar["close"] - v) / v)
    except Exception:
        out["vwap"] = None
    try:
        out["bbw_20"] = _clean(bollinger_width(close, 20).iloc[bar_idx])
    except Exception:
        out["bbw_20"] = None

    # Volume ratio vs. trailing 20 bars
    try:
        vol_sma = bars["volume"].rolling(20).mean().iloc[bar_idx]
        if vol_sma and vol_sma > 0:
            out["volume_ratio"] = _clean(float(bar["volume"]) / float(vol_sma))
    except Exception:
        out["volume_ratio"] = None

    # VIX (optional — usually supplied once per run)
    if vix_series is not None and not vix_series.empty:
        try:
            nearest_idx = vix_series.index.get_indexer([ts], method="nearest")[0]
            if nearest_idx >= 0:
                out["vix"] = _clean(vix_series.iloc[nearest_idx])
        except Exception:
            pass

    return out


def context_at(bars: pd.DataFrame, bar_idx: int, *, lookback: int = 120) -> dict:
    """Structural / regime features — not raw indicators, but useful
    for data-science analysis (e.g. 'trades entered at weekly highs')."""
    if bars is None or bars.empty or bar_idx < 0 or bar_idx >= len(bars):
        return {}

    out: dict = {}
    ts = bars.index[bar_idx]

    # Session phase
    hour = ts.hour
    if hour < 10:
        out["session_phase"] = "open"        # 09:30-10:00 ET roughly
    elif hour < 12:
        out["session_phase"] = "morning"
    elif hour < 14:
        out["session_phase"] = "midday"
    elif hour < 15:
        out["session_phase"] = "afternoon"
    else:
        out["session_phase"] = "close"

    # Bars since recent high / low
    start = max(0, bar_idx - lookback)
    window = bars.iloc[start:bar_idx + 1]
    if len(window) > 1:
        out["bars_since_high"] = int(len(window) - 1 - window["high"].values.argmax())
        out["bars_since_low"] = int(len(window) - 1 - window["low"].values.argmin())
        hi = float(window["high"].max())
        lo = float(window["low"].min())
        out["range_pct"] = _clean((hi - lo) / lo if lo else None)

    # Trend state vs SMA50 (if available)
    try:
        sma50 = sma(bars["close"], 50).iloc[bar_idx]
        if sma50 and sma50 > 0:
            diff = (bars["close"].iloc[bar_idx] - sma50) / sma50
            out["trend_vs_sma50_pct"] = _clean(diff)
            out["above_sma50"] = bool(diff > 0)
    except Exception:
        pass

    return out
