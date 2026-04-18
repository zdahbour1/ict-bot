"""
Unit tests for strategy/base_strategy.py — plugin framework (ENH-024).

Covers: Signal dataclass, BaseStrategy abstract contract, StrategyRegistry.
"""
import pytest
from typing import List
import pandas as pd

from strategy.base_strategy import BaseStrategy, Signal, StrategyRegistry


class TestSignal:
    def test_defaults(self):
        s = Signal("LONG_iFVG", "LONG", 100.0, 98.0, 104.0, "sid-1")
        assert s.ticker == ""
        assert s.strategy_name == ""
        assert s.confidence == 0.0
        assert s.details == {}

    def test_dedup_key(self):
        s = Signal("ORB_BREAKOUT_LONG", "LONG", 634.127, 630.0, 640.0, "x")
        assert s.dedup_key == "ORB_BREAKOUT_LONG_634.13"

    def test_to_dict_flattens_legacy_fields(self):
        s = Signal(
            signal_type="LONG_iFVG", direction="LONG",
            entry_price=100.0, sl=98.0, tp=104.0, setup_id="sid",
            ticker="QQQ", strategy_name="ict", confidence=0.7,
            details={"raid": {"high": 101}, "fvg": {"top": 99}, "foo": "bar"},
        )
        d = s.to_dict()
        # Core fields
        assert d["signal_type"] == "LONG_iFVG"
        assert d["strategy_name"] == "ict"
        # Legacy fields lifted up
        assert d["raid"] == {"high": 101}
        assert d["fvg"] == {"top": 99}
        # And full details preserved
        assert d["details"]["foo"] == "bar"

    def test_to_dict_without_optional_fields(self):
        s = Signal("X", "LONG", 1.0, 0.9, 1.1, "id")
        d = s.to_dict()
        assert "raid" not in d
        assert "fvg" not in d
        assert d["details"] == {}


class TestBaseStrategyContract:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            BaseStrategy()  # abstract — must subclass

    def test_minimal_concrete_subclass_works(self):
        class DummyStrategy(BaseStrategy):
            @property
            def name(self): return "dummy"
            @property
            def description(self): return "d"
            def detect(self, b1, b1h, b4h, levels, ticker): return []
        s = DummyStrategy()
        assert s.name == "dummy"
        # Optional hooks don't raise
        s.configure({})
        s.reset_daily()
        s.mark_used("x")

    def test_subclass_missing_detect_raises(self):
        class NoDetect(BaseStrategy):
            @property
            def name(self): return "x"
            @property
            def description(self): return "x"
        with pytest.raises(TypeError):
            NoDetect()


class TestStrategyRegistry:
    def test_register_and_lookup(self):
        # Snapshot the registry so we can restore it (other tests register real strategies)
        saved = dict(StrategyRegistry._classes)

        @StrategyRegistry.register
        class FooStrategy(BaseStrategy):
            @property
            def name(self): return "unit-foo"
            @property
            def description(self): return "foo"
            def detect(self, b1, b1h, b4h, levels, ticker): return []

        try:
            assert "unit-foo" in StrategyRegistry.all_names()
            got = StrategyRegistry.get("unit-foo")
            assert got is FooStrategy
            instance = StrategyRegistry.instantiate("unit-foo")
            assert isinstance(instance, FooStrategy)
            assert StrategyRegistry.get("does-not-exist") is None
            assert StrategyRegistry.instantiate("does-not-exist") is None
        finally:
            StrategyRegistry._classes = saved
