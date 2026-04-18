"""
Unit tests for strategy/orb_strategy.py — Opening Range Breakout (ENH-024).
"""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from strategy.orb_strategy import ORBStrategy


def _make_bars(opens: list[float], highs: list[float], lows: list[float],
               closes: list[float], start: str = "2026-04-15 13:30") -> pd.DataFrame:
    """Build a minute-resolution OHLC DataFrame (UTC index)."""
    n = len(opens)
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": [1000] * n,
    }, index=idx)


def _flat_range(price: float, minutes: int) -> pd.DataFrame:
    """A dead-flat range — every bar high = price + 0.5, low = price - 0.5."""
    opens = [price] * minutes
    highs = [price + 0.5] * minutes
    lows = [price - 0.5] * minutes
    closes = [price] * minutes
    return _make_bars(opens, highs, lows, closes)


class TestORBRange:
    def test_no_signal_before_range_complete(self):
        orb = ORBStrategy(range_minutes=15)
        bars = _flat_range(100.0, 10)  # only 10 bars, range needs 15
        out = orb.detect(bars, bars, bars, [], "QQQ")
        assert out == []

    def test_no_signal_no_breakout(self):
        """Price stays inside the range — no signal."""
        orb = ORBStrategy(range_minutes=15)
        bars = _flat_range(100.0, 60)  # 15 bar range + 45 bars inside it
        out = orb.detect(bars, bars, bars, [], "QQQ")
        assert out == []

    def test_long_breakout_fires(self):
        orb = ORBStrategy(range_minutes=15, breakout_buffer=0.0)
        # Range 100-101 for 15 bars, then push to 102
        opens = [100.5] * 15 + [101.5] * 5
        highs = [101.0] * 15 + [102.5] * 5
        lows = [100.0] * 15 + [101.0] * 5
        closes = [100.5] * 15 + [102.0] * 5
        bars = _make_bars(opens, highs, lows, closes)
        out = orb.detect(bars, bars, bars, [], "QQQ")
        assert len(out) == 1
        sig = out[0]
        assert sig.signal_type == "ORB_BREAKOUT_LONG"
        assert sig.direction == "LONG"
        assert sig.strategy_name == "orb"
        assert sig.ticker == "QQQ"
        assert sig.entry_price == 102.0
        assert sig.sl == pytest.approx(100.5)           # range mid
        assert sig.tp == pytest.approx(102.0 + 1.0)      # entry + range width
        assert sig.details["range_high"] == 101.0
        assert sig.details["range_low"] == 100.0

    def test_short_breakout_fires(self):
        orb = ORBStrategy(range_minutes=15, breakout_buffer=0.0)
        # Range 100-101 for 15 bars, then drop to 99
        opens = [100.5] * 15 + [99.5] * 5
        highs = [101.0] * 15 + [100.0] * 5
        lows = [100.0] * 15 + [98.5] * 5
        closes = [100.5] * 15 + [99.0] * 5
        bars = _make_bars(opens, highs, lows, closes)
        out = orb.detect(bars, bars, bars, [], "QQQ")
        assert len(out) == 1
        sig = out[0]
        assert sig.signal_type == "ORB_BREAKOUT_SHORT"
        assert sig.direction == "SHORT"
        assert sig.sl == pytest.approx(100.5)
        assert sig.tp == pytest.approx(99.0 - 1.0)       # entry - range width

    def test_both_directions_can_fire(self):
        """Price breaks up, then whipsaws down through range low."""
        orb = ORBStrategy(range_minutes=15, breakout_buffer=0.0)
        # Range 100-101, then up to 102, then all the way down to 99
        opens = [100.5] * 15 + [101.5, 101.0, 100.0, 99.5]
        highs = [101.0] * 15 + [102.5, 101.5, 100.5, 99.8]
        lows = [100.0] * 15 + [101.0, 100.0,  99.5, 98.8]
        closes = [100.5] * 15 + [102.0, 100.5, 99.8, 99.0]
        bars = _make_bars(opens, highs, lows, closes)
        out = orb.detect(bars, bars, bars, [], "QQQ")
        types = {s.signal_type for s in out}
        assert types == {"ORB_BREAKOUT_LONG", "ORB_BREAKOUT_SHORT"}

    def test_mark_used_suppresses_resignal(self):
        orb = ORBStrategy(range_minutes=15, breakout_buffer=0.0)
        opens = [100.5] * 15 + [101.5] * 5
        highs = [101.0] * 15 + [102.5] * 5
        lows = [100.0] * 15 + [101.0] * 5
        closes = [100.5] * 15 + [102.0] * 5
        bars = _make_bars(opens, highs, lows, closes)

        out1 = orb.detect(bars, bars, bars, [], "QQQ")
        assert len(out1) == 1
        orb.mark_used(out1[0].setup_id)

        out2 = orb.detect(bars, bars, bars, [], "QQQ")
        assert out2 == []

    def test_reset_daily_allows_refire(self):
        orb = ORBStrategy(range_minutes=15, breakout_buffer=0.0)
        opens = [100.5] * 15 + [102.0] * 3
        highs = [101.0] * 15 + [102.5] * 3
        lows = [100.0] * 15 + [101.5] * 3
        closes = [100.5] * 15 + [102.0] * 3
        bars = _make_bars(opens, highs, lows, closes)
        out = orb.detect(bars, bars, bars, [], "QQQ")
        orb.mark_used(out[0].setup_id)
        orb.reset_daily()
        out2 = orb.detect(bars, bars, bars, [], "QQQ")
        assert len(out2) == 1

    def test_buffer_suppresses_marginal_breakout(self):
        """With 1% buffer, a 0.5% poke above range high does NOT fire."""
        orb = ORBStrategy(range_minutes=15, breakout_buffer=0.01)
        opens = [100.5] * 15 + [101.3] * 5
        highs = [101.0] * 15 + [101.5] * 5  # 0.5% above range high only
        lows = [100.0] * 15 + [101.0] * 5
        closes = [100.5] * 15 + [101.3] * 5  # needs >101.0 * 1.01 = 102.01
        bars = _make_bars(opens, highs, lows, closes)
        out = orb.detect(bars, bars, bars, [], "QQQ")
        assert out == []

    def test_configure_updates_params(self):
        orb = ORBStrategy()
        assert orb.range_minutes == 15
        orb.configure({"ORB_RANGE_MINUTES": "30", "ORB_BREAKOUT_BUFFER": "0.005"})
        assert orb.range_minutes == 30
        assert orb.breakout_buffer == 0.005

    def test_configure_ignores_invalid_values(self):
        orb = ORBStrategy()
        orb.configure({"ORB_RANGE_MINUTES": "not-an-int"})
        assert orb.range_minutes == 15  # unchanged

    def test_empty_bars_returns_empty(self):
        orb = ORBStrategy()
        assert orb.detect(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), [], "QQQ") == []
