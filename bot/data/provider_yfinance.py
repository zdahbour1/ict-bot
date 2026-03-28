"""
yfinance data provider — free, no API key required.
Returns 1-minute OHLCV bars for backtesting.
yfinance limits: 1m bars = last 30 days, 5m bars = last 60 days.
"""
import pandas as pd
import yfinance as yf
from loguru import logger


def fetch_1m_bars(symbol: str, lookback_hours: int = 120) -> pd.DataFrame:
    """
    Fetch 1-minute OHLCV bars.
    Returns DataFrame with UTC timestamps and columns: open, high, low, close, volume.
    """
    # yfinance caps 1m data at 30 days; we request as much as allowed
    days = min(int(lookback_hours / 24) + 1, 29)
    period = f"{days}d"

    logger.info(f"Fetching {symbol} 1m bars (period={period}) from yfinance...")
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval="1m", auto_adjust=True)
    except Exception as e:
        raise RuntimeError(f"yfinance fetch failed: {e}") from e

    if df.empty:
        raise RuntimeError(f"No data returned for {symbol}. Market may be closed or symbol invalid.")

    # Normalize columns to lowercase
    df.columns = [c.lower() for c in df.columns]
    df = df[["open", "high", "low", "close", "volume"]].copy()

    # Ensure UTC index
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    df.index.name = "timestamp"
    df = df.sort_index()

    logger.info(f"Fetched {len(df)} 1m bars from {df.index[0]} to {df.index[-1]}")
    return df
