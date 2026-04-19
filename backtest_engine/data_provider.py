"""
Historical bar loader for backtests.

Uses yfinance as the source. Caches downloads to disk (parquet) so
repeated backtest runs don't re-hit the network — 5m and 1h bars are
fetched once per ticker per date range.

Limitations (yfinance free tier):
- 1m bars: last 7 days only
- 5m bars: last 60 days
- 1h bars: last 730 days
The engine defaults to 5m/1h/4h aggregation which aligns with how the
live bot builds multi-timeframe views from 1m inside bot/data/aggregator.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

_CACHE_DIR = Path(os.getenv("BACKTEST_CACHE_DIR",
                            Path.home() / ".ict_bot_cache" / "backtest"))
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_key(ticker: str, interval: str, start: date, end: date) -> Path:
    return _CACHE_DIR / f"{ticker.upper()}_{interval}_{start}_{end}.parquet"


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """yfinance returns MultiIndex columns in some versions. Flatten."""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() if isinstance(c, tuple) else str(c).lower()
                      for c in df.columns]
    else:
        df.columns = [str(c).lower() for c in df.columns]
    # yfinance uses "adj close" for adjusted close — drop it if present
    if "adj close" in df.columns:
        df = df.drop(columns=["adj close"])
    # Some versions title-case
    rename = {c: c.lower() for c in df.columns if c != c.lower()}
    if rename:
        df = df.rename(columns=rename)
    return df


def fetch_bars(
    ticker: str,
    *,
    interval: str = "5m",
    start: Optional[date] = None,
    end: Optional[date] = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Download (or load from cache) OHLCV bars.

    Returns a DataFrame with UTC tz-aware index and lowercase columns:
    open, high, low, close, volume.
    """
    if end is None:
        end = date.today()
    if start is None:
        # Default: as far back as yfinance allows per interval
        default_days = {"1m": 7, "5m": 60, "15m": 60, "1h": 730,
                        "4h": 730, "1d": 3650}
        start = end - timedelta(days=default_days.get(interval, 60))

    cache_file = _cache_key(ticker, interval, start, end)
    if use_cache and cache_file.exists():
        try:
            df = pd.read_parquet(cache_file)
            log.debug(f"cache hit: {cache_file.name} ({len(df)} bars)")
            return df
        except Exception as e:
            log.warning(f"cache read failed for {cache_file.name}: {e}")

    import yfinance as yf
    log.info(f"yfinance: {ticker} {interval} {start}→{end}")
    df = yf.download(
        ticker,
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),   # yfinance end is exclusive
        interval=interval,
        auto_adjust=False,
        progress=False,
        prepost=False,
    )
    if df is None or df.empty:
        log.warning(f"yfinance returned empty for {ticker} {interval}")
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = _normalize_columns(df)

    # Ensure UTC tz-aware
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    # Keep only the 5 canonical columns
    df = df[["open", "high", "low", "close", "volume"]]

    try:
        df.to_parquet(cache_file)
    except Exception as e:
        log.warning(f"cache write failed for {cache_file.name}: {e}")

    return df


def aggregate_bars(bars_1m_or_5m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample OHLCV bars to a larger timeframe."""
    if bars_1m_or_5m.empty:
        return bars_1m_or_5m
    agg = bars_1m_or_5m.resample(rule, label="left", closed="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()
    return agg


def fetch_multi_timeframe(
    ticker: str,
    *,
    base_interval: str = "5m",
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> dict[str, pd.DataFrame]:
    """Return a dict of DataFrames for the timeframes the strategy needs.

    Keys: 'base' (base_interval), '1h', '4h'.
    """
    base = fetch_bars(ticker, interval=base_interval, start=start, end=end)
    if base.empty:
        return {"base": base, "1h": base, "4h": base}
    return {
        "base": base,
        "1h": aggregate_bars(base, "1h"),
        "4h": aggregate_bars(base, "4h"),
    }
