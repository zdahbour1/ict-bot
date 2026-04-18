"""
Unit tests for strategy/signal_engine.py — deduplication + state management.

Covers:
- Signal dataclass + dedup_key
- SignalEngine dedup by (signal_type, entry_price)
- mark_used() prevents re-signaling same setup_id
- reset_daily() clears state
- clear_seen_setups() allows re-entry after close
- alerts_today counter
"""
import pytest
from unittest.mock import patch
import pandas as pd

from strategy.signal_engine import Signal, SignalEngine


# ── Signal dataclass ─────────────────────────────────────

class TestSignal:
    def test_dedup_key_rounds_price(self):
        s = Signal("LONG_iFVG", "LONG", 634.127, 630.0, 640.0, "setup1")
        assert s.dedup_key == "LONG_iFVG_634.13"

    def test_different_types_different_keys(self):
        s1 = Signal("LONG_iFVG", "LONG", 634.0, 630.0, 640.0, "a")
        s2 = Signal("SHORT_OB", "SHORT", 634.0, 638.0, 630.0, "b")
        assert s1.dedup_key != s2.dedup_key


# ── SignalEngine ─────────────────────────────────────────

def _mock_signal(signal_type="LONG_iFVG", entry_price=634.0, setup_id="s1",
                 direction="LONG"):
    """Build a raw dict as returned by ict_long/ict_short."""
    return {
        "signal_type": signal_type,
        "direction": direction,
        "entry_price": entry_price,
        "sl": entry_price * 0.98,
        "tp": entry_price * 1.02,
        "setup_id": setup_id,
    }


@pytest.fixture
def bars():
    """Minimal bars dataframe — contents don't matter since we mock strategies."""
    df = pd.DataFrame({"close": [634.0] * 10})
    return df


class TestSignalEngineDedup:
    def test_empty_signals(self, bars):
        eng = SignalEngine("QQQ")
        with patch("strategy.signal_engine.run_strategy", return_value=[]), \
             patch("strategy.signal_engine.run_strategy_short", return_value=[]):
            signals = eng.detect(bars, bars, bars, [])
        assert signals == []

    def test_single_long_signal(self, bars):
        eng = SignalEngine("QQQ")
        with patch("strategy.signal_engine.run_strategy",
                   return_value=[_mock_signal()]), \
             patch("strategy.signal_engine.run_strategy_short", return_value=[]):
            signals = eng.detect(bars, bars, bars, [])
        assert len(signals) == 1
        assert signals[0].signal_type == "LONG_iFVG"
        assert signals[0].ticker == "QQQ"
        assert signals[0].entry_price == 634.0

    def test_dedup_same_type_and_price(self, bars):
        """Two identical signals (same type + price) collapse to one."""
        eng = SignalEngine("QQQ")
        dup = [_mock_signal(setup_id="a"), _mock_signal(setup_id="b")]
        with patch("strategy.signal_engine.run_strategy", return_value=dup), \
             patch("strategy.signal_engine.run_strategy_short", return_value=[]):
            signals = eng.detect(bars, bars, bars, [])
        assert len(signals) == 1

    def test_dedup_allows_different_price(self, bars):
        eng = SignalEngine("QQQ")
        sigs = [_mock_signal(entry_price=634.0, setup_id="a"),
                _mock_signal(entry_price=635.0, setup_id="b")]
        with patch("strategy.signal_engine.run_strategy", return_value=sigs), \
             patch("strategy.signal_engine.run_strategy_short", return_value=[]):
            signals = eng.detect(bars, bars, bars, [])
        assert len(signals) == 2

    def test_dedup_allows_different_type(self, bars):
        """A LONG and SHORT at same price are not duplicates."""
        eng = SignalEngine("QQQ")
        long_s = _mock_signal("LONG_iFVG", 634.0, "l1", "LONG")
        short_s = _mock_signal("SHORT_OB", 634.0, "s1", "SHORT")
        with patch("strategy.signal_engine.run_strategy", return_value=[long_s]), \
             patch("strategy.signal_engine.run_strategy_short", return_value=[short_s]):
            signals = eng.detect(bars, bars, bars, [])
        assert len(signals) == 2


class TestSignalEngineState:
    def test_mark_used_filters_future_detections(self, bars):
        eng = SignalEngine("QQQ")
        sig = _mock_signal(setup_id="setup-abc")
        with patch("strategy.signal_engine.run_strategy", return_value=[sig]), \
             patch("strategy.signal_engine.run_strategy_short", return_value=[]):
            first = eng.detect(bars, bars, bars, [])
            assert len(first) == 1

            eng.mark_used("setup-abc")

            second = eng.detect(bars, bars, bars, [])
            assert second == []

    def test_alerts_today_increments(self):
        eng = SignalEngine("QQQ")
        assert eng.alerts_today == 0
        eng.mark_used("s1")
        eng.mark_used("s2")
        assert eng.alerts_today == 2

    def test_reset_daily_clears_everything(self, bars):
        eng = SignalEngine("QQQ")
        eng.mark_used("s1")
        eng.mark_used("s2")
        assert eng.alerts_today == 2

        eng.reset_daily()
        assert eng.alerts_today == 0

        # Previously-used setup can be re-detected
        sig = _mock_signal(setup_id="s1")
        with patch("strategy.signal_engine.run_strategy", return_value=[sig]), \
             patch("strategy.signal_engine.run_strategy_short", return_value=[]):
            signals = eng.detect(bars, bars, bars, [])
        assert len(signals) == 1

    def test_clear_seen_setups_keeps_alert_count(self, bars):
        """clear_seen_setups allows re-entry but doesn't reset alert count."""
        eng = SignalEngine("QQQ")
        eng.mark_used("s1")
        eng.mark_used("s2")
        eng.clear_seen_setups()
        # Alert count is preserved (rate limit still applies)
        assert eng.alerts_today == 2

        # But the setup can trigger again
        sig = _mock_signal(setup_id="s1")
        with patch("strategy.signal_engine.run_strategy", return_value=[sig]), \
             patch("strategy.signal_engine.run_strategy_short", return_value=[]):
            signals = eng.detect(bars, bars, bars, [])
        assert len(signals) == 1


class TestSignalEngineFields:
    def test_preserves_raw_signal_in_details(self, bars):
        eng = SignalEngine("QQQ")
        raw = _mock_signal()
        raw["raid"] = {"high": 635.0}
        raw["confirmation"] = {"time": "10:00"}
        with patch("strategy.signal_engine.run_strategy", return_value=[raw]), \
             patch("strategy.signal_engine.run_strategy_short", return_value=[]):
            signals = eng.detect(bars, bars, bars, [])
        assert signals[0].details["raid"] == {"high": 635.0}
        assert signals[0].details["_raw"] is raw

    def test_missing_direction_defaults_to_long(self, bars):
        """signal_type is required by dedup, but direction/setup_id get defaults."""
        eng = SignalEngine("QQQ")
        minimal = {
            "signal_type": "LONG_iFVG",
            "entry_price": 100.0, "sl": 98.0, "tp": 102.0,
        }
        with patch("strategy.signal_engine.run_strategy", return_value=[minimal]), \
             patch("strategy.signal_engine.run_strategy_short", return_value=[]):
            signals = eng.detect(bars, bars, bars, [])
        assert signals[0].direction == "LONG"
        assert signals[0].setup_id == ""
