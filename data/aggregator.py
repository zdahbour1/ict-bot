"""
Bar Aggregator — converts 1m bars into higher timeframes.
Uses floor+groupby method (robust, no fragile resample).
"""
import logging
import pandas as pd

log = logging.getLogger(__name__)

# Map timeframe label → minutes
TF_MINUTES = {
    "5m":  5,
    "15m": 15,
    "1h":  60,
    "4h":  240,
}


def aggregate(bars_1m: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """
    Aggregate 1m bars into the target timeframe.

    :param bars_1m: DataFrame with UTC datetime index, columns: open/high/low/close/volume
    :param timeframe: one of "5m", "15m", "1h", "4h"
    :returns: Aggregated OHLCV DataFrame
    """
    if bars_1m.empty:
        return pd.DataFrame()

    minutes = TF_MINUTES.get(timeframe)
    if not minutes:
        raise ValueError(f"Unknown timeframe: {timeframe}. Use one of {list(TF_MINUTES)}")

    # Floor each bar timestamp to the target period boundary
    freq = f"{minutes}min"
    floored = bars_1m.index.floor(freq)

    df = bars_1m.copy()
    df["_bucket"] = floored

    # Group and aggregate
    agg = df.groupby("_bucket").agg(
        open=("open",   "first"),
        high=("high",   "max"),
        low=("low",     "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    agg.index.name = "datetime"

    # Drop incomplete last bar (current partial bar)
    if len(agg) > 1:
        agg = agg.iloc[:-1]

    log.debug(f"Aggregated {len(bars_1m)} 1m bars → {len(agg)} {timeframe} bars")
    return agg
