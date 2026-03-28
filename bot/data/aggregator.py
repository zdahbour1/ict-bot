"""
Bar aggregator — converts 1m bars to higher timeframes.
Uses floor+groupby (robust, not fragile resample).
Handles duplicate timestamps and missing bars per spec section 5.3.
"""
import pandas as pd
from loguru import logger


def _dedup(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate duplicate timestamps: open=first, high=max, low=min, close=last, volume=sum."""
    if not df.index.is_unique:
        count = df.index.duplicated().sum()
        logger.warning(f"Found {count} duplicate timestamps — aggregating.")
        df = df.groupby(df.index).agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
    return df.sort_index()


def aggregate(df_1m: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """
    Aggregate 1m bars to `minutes`-minute bars.
    Uses floor division on timestamps — no fragile resample().
    """
    df = _dedup(df_1m.copy())

    # Floor timestamps to the target timeframe
    freq = f"{minutes}min"
    floored = df.index.floor(freq)

    agg = df.groupby(floored).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    agg.index.name = "timestamp"
    return agg.sort_index()


def build_all_timeframes(df_1m: pd.DataFrame) -> dict:
    """
    Returns dict of DataFrames for all required timeframes.
    Keys: '1m', '5m', '15m', '1h', '4h'
    """
    df_1m = _dedup(df_1m.copy())
    return {
        "1m":  df_1m,
        "5m":  aggregate(df_1m, 5),
        "15m": aggregate(df_1m, 15),
        "1h":  aggregate(df_1m, 60),
        "4h":  aggregate(df_1m, 240),
    }
