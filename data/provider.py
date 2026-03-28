"""
Data Provider — fetches 1-minute OHLCV bars for QQQ.
Uses yfinance (free, no API key needed).
"""
import logging
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
import pytz

log = logging.getLogger(__name__)


def get_bars_1m(symbol: str = "QQQ", days_back: int = 5) -> pd.DataFrame:
    """
    Fetch 1-minute OHLCV bars for the last N trading days.
    Returns a DataFrame with columns: open, high, low, close, volume
    Index: UTC datetime
    """
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=f"{days_back}d", interval="1m")
        if df.empty:
            log.warning(f"No 1m data returned for {symbol}")
            return pd.DataFrame()

        # Normalize column names to lowercase
        df.columns = [c.lower() for c in df.columns]
        df = df[["open", "high", "low", "close", "volume"]]

        # Ensure UTC index
        if df.index.tzinfo is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")

        # Remove duplicates and sort
        df = df[~df.index.duplicated(keep="first")]
        df = df.sort_index()

        log.info(f"Fetched {len(df)} 1m bars for {symbol} "
                 f"({df.index[0]} → {df.index[-1]})")
        return df

    except Exception as e:
        log.error(f"Failed to fetch {symbol} bars: {e}")
        return pd.DataFrame()
