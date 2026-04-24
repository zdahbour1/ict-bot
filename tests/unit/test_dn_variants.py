"""Tests for the DN variant system + strike_by_delta helper."""
from __future__ import annotations

import pytest


class TestVariantRegistry:
    def test_canonical_variants_registered(self):
        """Original 5 canonical + V5b sweep-winner (ENH-061) + 4 ZDN
        gamma-scalping variants (2026-04-24) = 10 total."""
        from strategy.delta_neutral_variants import VARIANTS, VARIANT_BY_NAME
        assert len(VARIANTS) == 10
        assert set(VARIANT_BY_NAME.keys()) == {
            "v1_baseline", "v2_hold_day", "v3_phaseB",
            "v4_filtered", "v5_hedged", "v5b_sweep_winner",
            "zdn_0dte", "zdn_weekly", "zdn_monthly", "zdn_next_month",
        }

    def test_v5b_uses_sweep_winning_params(self):
        from strategy.delta_neutral_variants import V5B_SWEEP_WINNER
        assert V5B_SWEEP_WINNER.short_delta == 0.25
        assert V5B_SWEEP_WINNER.long_delta == 0.03
        assert V5B_SWEEP_WINNER.ivr_min == 50.0
        assert V5B_SWEEP_WINNER.hard_exit_dte == 30
        assert V5B_SWEEP_WINNER.delta_hedge is True

    def test_get_variant_unknown_raises(self):
        from strategy.delta_neutral_variants import get_variant
        with pytest.raises(KeyError):
            get_variant("nope")

    def test_variant_flags_progression(self):
        """V3 → V4 should ADD filters; V4 → V5 should ADD hedging."""
        from strategy.delta_neutral_variants import (
            V3_PHASEB, V4_FILTERED, V5_HEDGED,
        )
        # V3 has no filters
        assert V3_PHASEB.ivr_min == 0
        assert not V3_PHASEB.regime_filter
        assert not V3_PHASEB.event_blackout
        # V4 adds all filters
        assert V4_FILTERED.ivr_min > 0
        assert V4_FILTERED.regime_filter
        assert V4_FILTERED.event_blackout
        # V5 adds hedging
        assert not V4_FILTERED.delta_hedge
        assert V5_HEDGED.delta_hedge
        assert V5_HEDGED.gamma_vega_caps

    def test_tier_universe(self):
        from strategy.delta_neutral_variants import TIERS, all_tier_tickers
        # At least 3 tickers in tier 0 (indices), 6 in tier 1 (mega-caps)
        assert len(TIERS[0]) >= 3
        assert len(TIERS[1]) >= 6
        # No duplicate ticker across tiers
        all_syms = [s for _, s in all_tier_tickers()]
        assert len(all_syms) == len(set(all_syms))


class TestStrikeByDelta:
    def test_atm_call_returns_near_money_strike(self):
        from backtest_engine.dn_variants_engine import strike_by_delta
        # At-the-money call delta ≈ 0.5; target 0.5 should return ~underlying
        k = strike_by_delta(500.0, 0.5, dte_days=45, sigma=0.20,
                            right="C", strike_interval=5)
        assert 495 <= k <= 505

    def test_16_delta_short_call_is_otm(self):
        """16-delta short call should be ~1 sigma OTM."""
        from backtest_engine.dn_variants_engine import strike_by_delta
        k = strike_by_delta(500.0, 0.16, dte_days=45, sigma=0.20,
                            right="C", strike_interval=5)
        # 1-sigma move on SPY at 20% IV over 45 days ≈ 500 * 0.20 * sqrt(45/365) ≈ 35
        # So 16-delta call should be around 520-545
        assert 515 <= k <= 555, f"16-delta strike = {k}"

    def test_5_delta_long_call_is_deep_otm(self):
        from backtest_engine.dn_variants_engine import strike_by_delta
        k = strike_by_delta(500.0, 0.05, dte_days=45, sigma=0.20,
                            right="C", strike_interval=5)
        # Much deeper OTM than 16-delta
        k16 = strike_by_delta(500.0, 0.16, dte_days=45, sigma=0.20,
                              right="C", strike_interval=5)
        assert k > k16

    def test_put_target_delta_is_negative(self):
        """For short put at -0.16 delta, strike should be OTM (below spot)."""
        from backtest_engine.dn_variants_engine import strike_by_delta
        k = strike_by_delta(500.0, -0.16, dte_days=45, sigma=0.20,
                            right="P", strike_interval=5)
        assert k < 500

    def test_rounds_to_strike_interval(self):
        from backtest_engine.dn_variants_engine import strike_by_delta
        k = strike_by_delta(500.0, 0.16, dte_days=45, sigma=0.20,
                            right="C", strike_interval=5)
        assert k % 5 == 0


class TestEarningsBlackout:
    def test_indices_never_blacked_out(self):
        from backtest_engine.dn_variants_engine import _is_earnings_blackout
        from datetime import date
        for t in ("SPY", "QQQ", "IWM"):
            for d in [date(2026, 4, 30), date(2026, 5, 1)]:
                assert not _is_earnings_blackout(t, d)

    def test_macro_event_blackout_applies_to_any_ticker(self):
        from backtest_engine.dn_variants_engine import _is_earnings_blackout
        from datetime import date
        # 2026-04-30 is FOMC; any equity ticker blackout
        assert _is_earnings_blackout("NVDA", date(2026, 4, 30))
        assert _is_earnings_blackout("AMD", date(2026, 4, 29))    # 1d before
        assert not _is_earnings_blackout("NVDA", date(2026, 5, 10))  # far from event


class TestVariantResultMetrics:
    def test_zero_trades_gives_clean_zeros(self):
        from backtest_engine.dn_variants_engine import VariantResult
        r = VariantResult("v1_baseline", "SPY")
        m = r.metrics()
        assert m["trades"] == 0
        assert m["total_pnl"] == 0
        assert m["max_drawdown"] == 0
        assert m["win_rate"] == 0

    def test_max_drawdown_tracks_running_peak(self):
        """Three trades: +100, -200, +50 → running = 100, -100, -50.
        Peak = 100; DD = -200."""
        from backtest_engine.dn_variants_engine import VariantResult
        r = VariantResult("v1", "X")
        r.trades = [
            {"pnl_usd": 100}, {"pnl_usd": -200}, {"pnl_usd": 50},
        ]
        m = r.metrics()
        assert m["total_pnl"] == -50.0
        assert m["max_drawdown"] == -200.0
