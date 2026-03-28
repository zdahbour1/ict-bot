"""
Unit tests for the bar aggregator.
Tests: duplicate handling, aggregation, missing bars.
"""
import pytest
import pandas as pd
import pytz

from bot.data.aggregator import aggregate, _dedup, build_all_timeframes


UTC = pytz.utc


def _make_1m_bars(prices: list) -> pd.DataFrame:
    """Helper: create a 1m bar DataFrame from a list of close prices."""
    idx = pd.date_range("2024-01-15 14:00", periods=len(prices), freq="1min", tz=UTC)
    df = pd.DataFrame({
        "open":   prices,
        "high":   [p + 0.05 for p in prices],
        "low":    [p - 0.05 for p in prices],
        "close":  prices,
        "volume": [1000] * len(prices),
    }, index=idx)
    df.index.name = "timestamp"
    return df


def test_dedup_collapses_duplicate_timestamps():
    """Duplicate timestamps must be aggregated without raising."""
    idx = pd.to_datetime(["2024-01-15 14:00", "2024-01-15 14:00", "2024-01-15 14:01"]).tz_localize(UTC)
    df = pd.DataFrame({
        "open":   [100.0, 101.0, 102.0],
        "high":   [100.5, 101.5, 102.5],
        "low":    [99.5,  100.5, 101.5],
        "close":  [100.2, 101.2, 102.2],
        "volume": [500,   500,   1000],
    }, index=idx)
    df.index.name = "timestamp"

    result = _dedup(df)
    assert result.index.is_unique, "Index must be unique after dedup"
    assert len(result) == 2, "Two timestamps expected after dedup"
    # open=first, high=max, low=min, close=last, volume=sum
    assert result.iloc[0]["open"]   == 100.0
    assert result.iloc[0]["high"]   == 101.5
    assert result.iloc[0]["low"]    == 99.5
    assert result.iloc[0]["close"]  == 101.2
    assert result.iloc[0]["volume"] == 1000


def test_aggregate_5m_from_1m():
    """5m aggregation must group 5 bars correctly."""
    df_1m = _make_1m_bars([100 + i for i in range(10)])
    df_5m = aggregate(df_1m, 5)
    assert len(df_5m) == 2, "10 1m bars → 2 5m bars"
    # First 5m bar: open=100 (first), high=max(100..104)+0.05, low=min(100..104)-0.05
    assert df_5m.iloc[0]["open"]  == 100.0
    assert df_5m.iloc[0]["close"] == 104.0


def test_missing_bars_no_crash():
    """Gaps in 1m data (missing bars) must not cause errors."""
    df_1m = _make_1m_bars([100.0, 101.0, 103.0])  # gap between bars 2 and 3
    result = build_all_timeframes(df_1m)
    assert "5m" in result
    assert len(result["5m"]) > 0


def test_all_timeframes_keys():
    df_1m = _make_1m_bars([100.0] * 300)
    tfs = build_all_timeframes(df_1m)
    assert set(tfs.keys()) == {"1m", "5m", "15m", "1h", "4h"}
