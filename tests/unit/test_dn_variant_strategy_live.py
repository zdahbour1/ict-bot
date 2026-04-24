"""Tests for the live DN variant strategy plugins.

Validates that each of the 5 variants registers correctly, produces
signals with the right variant tag, and emits legs consistent with
the backtest engine's `run_variant_backtest` behavior.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest


def _make_bars(n=100, start_price=500.0):
    """Synthetic 1-min bar frame of shape required by detect()."""
    idx = pd.date_range("2026-04-24 09:30", periods=n, freq="1min",
                        tz="America/New_York")
    # Mild drift + noise so pct_change().std() is sensible
    import numpy as np
    rng = np.random.default_rng(42)
    rets = rng.normal(0, 0.002, n)
    closes = start_price * (1 + rets).cumprod()
    df = pd.DataFrame({
        "open": closes, "high": closes * 1.001, "low": closes * 0.999,
        "close": closes, "volume": [1000] * n,
    }, index=idx)
    return df


class TestVariantRegistration:
    def test_all_five_variants_in_registry(self):
        from strategy.base_strategy import StrategyRegistry
        # The import side-effect registers them
        import strategy.delta_neutral_variant_strategy  # noqa: F401
        names = set(StrategyRegistry._classes.keys())
        for expected in ("v1_baseline", "v2_hold_day", "v3_phaseB",
                          "v4_filtered", "v5_hedged"):
            assert expected in names, f"{expected} missing from registry"

    def test_each_variant_has_unique_name(self):
        from strategy.delta_neutral_variant_strategy import (
            DNVariantStrategyV1Baseline, DNVariantStrategyV2HoldDay,
            DNVariantStrategyV3PhaseB, DNVariantStrategyV4Filtered,
            DNVariantStrategyV5Hedged,
        )
        names = {cls().name for cls in (
            DNVariantStrategyV1Baseline, DNVariantStrategyV2HoldDay,
            DNVariantStrategyV3PhaseB, DNVariantStrategyV4Filtered,
            DNVariantStrategyV5Hedged,
        )}
        assert len(names) == 5


class TestDetectBehavior:
    def test_v1_fires_with_no_filters(self):
        from strategy.delta_neutral_variant_strategy import DNVariantStrategyV1Baseline
        s = DNVariantStrategyV1Baseline()
        bars = _make_bars()
        sigs = s.detect(bars, bars, bars, [], "SPY")
        assert len(sigs) == 1
        assert sigs[0].details["variant"] == "v1_baseline"
        assert sigs[0].details["target_dte"] == 7

    def test_v3_has_45_dte_target(self):
        from strategy.delta_neutral_variant_strategy import DNVariantStrategyV3PhaseB
        s = DNVariantStrategyV3PhaseB()
        bars = _make_bars()
        sigs = s.detect(bars, bars, bars, [], "SPY")
        assert len(sigs) == 1
        assert sigs[0].details["target_dte"] == 45

    def test_v4_blocks_on_event_blackout(self, monkeypatch):
        """When today is in the earnings/FOMC blackout window, V4
        should return no signals."""
        import strategy.delta_neutral_variant_strategy as mod
        monkeypatch.setattr(mod, "_is_earnings_blackout",
                             lambda *a, **kw: True)
        s = mod.DNVariantStrategyV4Filtered()
        bars = _make_bars()
        sigs = s.detect(bars, bars, bars, [], "AAPL")
        assert sigs == []

    def test_v1_ignores_event_blackout(self, monkeypatch):
        """V1 has no blackout filter; blackout stub must not block it."""
        import strategy.delta_neutral_variant_strategy as mod
        monkeypatch.setattr(mod, "_is_earnings_blackout",
                             lambda *a, **kw: True)
        s = mod.DNVariantStrategyV1Baseline()
        bars = _make_bars()
        sigs = s.detect(bars, bars, bars, [], "AAPL")
        assert len(sigs) == 1

    def test_setup_id_dedup(self):
        """Same ticker + same date → one signal per variant."""
        from strategy.delta_neutral_variant_strategy import DNVariantStrategyV1Baseline
        s = DNVariantStrategyV1Baseline()
        bars = _make_bars()
        s.detect(bars, bars, bars, [], "SPY")
        s.mark_used(f"v1_baseline-SPY-{bars.index[-1].date()}")
        sigs2 = s.detect(bars, bars, bars, [], "SPY")
        assert sigs2 == []


class TestPlaceLegs:
    def test_v1_places_4_legs_atm_straddle(self):
        from strategy.delta_neutral_variant_strategy import DNVariantStrategyV1Baseline
        from strategy.base_strategy import Signal
        s = DNVariantStrategyV1Baseline()
        sig = Signal(
            signal_type="X", direction="LONG", entry_price=500.0,
            sl=0, tp=0, setup_id="x", ticker="SPY",
            strategy_name="v1_baseline",
            details={"current_price": 500.0, "sigma": 0.20,
                      "expiry": "20260501"},
        )
        legs = s.place_legs(sig)
        assert len(legs) == 4
        roles = [l.leg_role for l in legs]
        assert roles == ["short_call", "long_call", "short_put", "long_put"]
        # V1 is ATM + fixed-wing mode
        strikes = [l.strike for l in legs]
        assert strikes[0] == strikes[2]   # short call strike == short put strike (both ATM)
        assert strikes[1] > strikes[0]    # long call above short call
        assert strikes[3] < strikes[2]    # long put below short put

    def test_v3_uses_delta_targeted_strikes(self):
        from strategy.delta_neutral_variant_strategy import DNVariantStrategyV3PhaseB
        from strategy.base_strategy import Signal
        s = DNVariantStrategyV3PhaseB()
        sig = Signal(
            signal_type="X", direction="LONG", entry_price=500.0,
            sl=0, tp=0, setup_id="x", ticker="SPY",
            strategy_name="v3_phaseB",
            details={"current_price": 500.0, "sigma": 0.20,
                      "expiry": "20260601"},
        )
        legs = s.place_legs(sig)
        sc = next(l for l in legs if l.leg_role == "short_call")
        sp = next(l for l in legs if l.leg_role == "short_put")
        # 16-delta short call should be meaningfully OTM at 45 DTE, 20% IV
        assert sc.strike > 500.0
        # 16-delta short put below spot
        assert sp.strike < 500.0
        # Unlike V1, short_call != short_put (they were both ATM in V1)
        assert sc.strike != sp.strike


class TestVariantMetadataPassThrough:
    def test_details_carries_variant_label(self):
        from strategy.delta_neutral_variant_strategy import DNVariantStrategyV5Hedged
        s = DNVariantStrategyV5Hedged()
        bars = _make_bars()
        sigs = s.detect(bars, bars, bars, [], "SPY")
        assert len(sigs) == 1
        assert sigs[0].details["variant"] == "v5_hedged"
        assert sigs[0].details["label"] == "V5"
