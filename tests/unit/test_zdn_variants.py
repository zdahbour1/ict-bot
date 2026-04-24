"""Tests for the ZDN (zero-delta-neutral) variant family.

Four variants share:
  - iron butterfly structure (both shorts at ATM, same strike)
  - 10:00 ET entry gate
  - 15-min-before-close exit gate (static config)
  - 50% TP / 25% SL (static config)
  - tight ±10-share delta hedge band
  - 100-contract liquidity floor

They differ only in expiry: 0DTE, weekly, monthly, next-month.

These tests validate the parts that are wired in live code today:
  - variants exist in registry with the expected names
  - iron butterfly strike math (same ATM for short call + short put)
  - entry time gate blocks entries before 10:00 ET
  - delta_hedger honors per-variant `hedge_delta_band_shares` override
  - expiry selection logic returns plausible Fridays for each mode
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


def _make_bars(n=100, start_price=500.0):
    idx = pd.date_range("2026-04-24 09:30", periods=n, freq="1min",
                        tz="America/New_York")
    import numpy as np
    rng = np.random.default_rng(42)
    rets = rng.normal(0, 0.002, n)
    closes = start_price * (1 + rets).cumprod()
    return pd.DataFrame({
        "open": closes, "high": closes * 1.001, "low": closes * 0.999,
        "close": closes, "volume": [1000] * n,
    }, index=idx)


class TestZDNRegistration:
    def test_all_four_zdn_variants_in_registry(self):
        from strategy.base_strategy import StrategyRegistry
        import strategy.delta_neutral_variant_strategy  # noqa: F401
        names = set(StrategyRegistry._classes.keys())
        for expected in ("zdn_0dte", "zdn_weekly",
                          "zdn_monthly", "zdn_next_month"):
            assert expected in names, f"{expected} missing from registry"

    def test_zdn_variants_have_butterfly_structure(self):
        from strategy.delta_neutral_variants import (
            ZDN_0DTE, ZDN_WEEKLY, ZDN_MONTHLY, ZDN_NEXT_MONTH,
        )
        for v in (ZDN_0DTE, ZDN_WEEKLY, ZDN_MONTHLY, ZDN_NEXT_MONTH):
            assert v.structure == "iron_butterfly"
            assert v.delta_hedge is True
            assert v.hedge_delta_band_shares == 10
            assert v.entry_time_et == "10:00"
            assert v.exit_before_close_min == 15
            assert v.profit_target_pct == 0.50
            assert v.stop_loss_pct == 0.25
            assert v.min_option_volume == 100


class TestIronButterflyStrikes:
    def test_both_shorts_at_same_atm_strike(self):
        """Defining feature of an iron butterfly: short call and short
        put share the ATM strike. Wings are width offset."""
        from strategy.base_strategy import Signal
        from strategy.delta_neutral_variant_strategy import (
            DNVariantStrategyZDN0DTE,
        )
        strat = DNVariantStrategyZDN0DTE()
        sig = Signal(
            signal_type="DELTA_NEUTRAL_ZDN-0",
            direction="LONG",
            entry_price=500.0, sl=0.0, tp=0.0,
            setup_id="zdn-SPY",
            ticker="SPY",
            strategy_name="zdn_0dte",
            confidence=0.7,
            details={
                "current_price": 500.0, "sigma": 0.20,
                "target_dte": 0,
                "expiry": "20260424",
            },
        )
        legs = strat.place_legs(sig)
        by_role = {l.leg_role: l for l in legs}
        # Both shorts at the same ATM strike
        assert by_role["short_call"].strike == by_role["short_put"].strike
        # Wings are $5 wide per _ZDN_COMMON
        atm = by_role["short_call"].strike
        assert by_role["long_call"].strike == atm + 5.0
        assert by_role["long_put"].strike == atm - 5.0


class TestEntryTimeGate:
    def test_before_10am_blocks_entry(self):
        from strategy.delta_neutral_variant_strategy import (
            _is_before_entry_time_et,
        )
        # Mock current NY time as 09:15 ET
        fake_now = datetime(2026, 4, 24, 9, 15)
        with patch("strategy.delta_neutral_variant_strategy.datetime") as m:
            m.now.return_value = fake_now
            m.utcnow.return_value = fake_now
            assert _is_before_entry_time_et("10:00") is True

    def test_after_10am_allows_entry(self):
        from strategy.delta_neutral_variant_strategy import (
            _is_before_entry_time_et,
        )
        fake_now = datetime(2026, 4, 24, 10, 30)
        with patch("strategy.delta_neutral_variant_strategy.datetime") as m:
            m.now.return_value = fake_now
            m.utcnow.return_value = fake_now
            assert _is_before_entry_time_et("10:00") is False

    def test_none_disables_gate(self):
        from strategy.delta_neutral_variant_strategy import (
            _is_before_entry_time_et,
        )
        assert _is_before_entry_time_et(None) is False
        assert _is_before_entry_time_et("") is False


class TestExpiryModes:
    def test_0dte_returns_today_on_weekday(self):
        from strategy.delta_neutral_variant_strategy import _expiry_for_mode
        # 2026-04-24 is a Friday
        expected = date.today().strftime("%Y%m%d")
        result = _expiry_for_mode("0dte", 0, 0, 1)
        assert result == expected

    def test_weekly_returns_a_friday(self):
        from strategy.delta_neutral_variant_strategy import _expiry_for_mode
        yyyymmdd = _expiry_for_mode("weekly", 5, 1, 10)
        d = datetime.strptime(yyyymmdd, "%Y%m%d").date()
        assert d.weekday() == 4        # Friday
        assert d > date.today()

    def test_monthly_returns_third_friday(self):
        from strategy.delta_neutral_variant_strategy import (
            _expiry_for_mode, _third_friday,
        )
        yyyymmdd = _expiry_for_mode("monthly", 20, 10, 45)
        d = datetime.strptime(yyyymmdd, "%Y%m%d").date()
        # Must be a 3rd Friday (of some month)
        assert d.weekday() == 4
        third = _third_friday(d.year, d.month)
        assert d == third

    def test_next_month_is_at_least_as_far_as_monthly(self):
        """When the current month's 3rd Friday has already passed,
        'monthly' rolls to next-month's 3rd Friday — which is exactly
        where 'next_month' points too. Otherwise next_month is
        strictly later. So the contract is >=, not >."""
        from strategy.delta_neutral_variant_strategy import _expiry_for_mode
        m = datetime.strptime(
            _expiry_for_mode("monthly", 20, 10, 45), "%Y%m%d").date()
        nm = datetime.strptime(
            _expiry_for_mode("next_month", 45, 30, 60), "%Y%m%d").date()
        assert nm >= m


class TestDeltaHedgerPerVariantBand:
    def test_variant_override_beats_global_band(self):
        """ZDN trades should hedge at ±10 shares even when the global
        DN_DELTA_BAND_SHARES is 20."""
        from strategy.delta_hedger import DeltaHedger
        client = MagicMock()
        client.get_realtime_equity_price.return_value = 500.0
        client.buy_stock.return_value = {"fill_price": 500.0, "order_id": 1}
        client.sell_stock.return_value = {"fill_price": 500.0, "order_id": 2}

        hedger = DeltaHedger(client, interval_sec=30, band_shares=20)
        # Fake trade: a ZDN_0DTE trade with net delta of 15 shares —
        # between the global 20 band and the ZDN 10 band.
        # With override it MUST fire; without it must not.
        trade = {
            "trade_id": 42,
            "ticker": "SPY",
            "hedge_shares": 0,
            "strategy_name": "zdn_0dte",
            "legs": [
                {"sec_type": "STK", "contracts_open": 15,
                 "direction": "LONG", "multiplier": 1,
                 "strike": None, "right": None, "expiry": None},
            ],
        }
        with patch("strategy.delta_hedger._update_trade_hedge_shares"), \
             patch("strategy.delta_hedger._record_hedge_event"), \
             patch("strategy.delta_hedger._sys_log"):
            hedger._rebalance_one(trade)

        # net_delta = +15 (STK leg long 15). Residual = 15 > band 10 → SELL.
        assert client.sell_stock.called, (
            "ZDN per-variant ±10 band override must fire hedge even "
            "when global band is ±20"
        )

    def test_non_hedged_variant_skips_hedge(self):
        """V1_BASELINE has delta_hedge=False — hedger must skip."""
        from strategy.delta_hedger import DeltaHedger
        client = MagicMock()
        client.get_realtime_equity_price.return_value = 500.0

        hedger = DeltaHedger(client, interval_sec=30, band_shares=10)
        trade = {
            "trade_id": 1,
            "ticker": "SPY",
            "hedge_shares": 0,
            "strategy_name": "v1_baseline",
            "legs": [{"sec_type": "STK", "contracts_open": 100,
                       "direction": "LONG", "multiplier": 1,
                       "strike": None, "right": None, "expiry": None}],
        }
        with patch("strategy.delta_hedger._update_trade_hedge_shares"), \
             patch("strategy.delta_hedger._record_hedge_event"), \
             patch("strategy.delta_hedger._sys_log"):
            hedger._rebalance_one(trade)

        client.buy_stock.assert_not_called()
        client.sell_stock.assert_not_called()


class TestLookupVariant:
    def test_returns_none_for_legacy_delta_neutral(self):
        from strategy.delta_hedger import _lookup_variant
        assert _lookup_variant("delta_neutral") is None

    def test_returns_variant_for_zdn(self):
        from strategy.delta_hedger import _lookup_variant
        v = _lookup_variant("zdn_weekly")
        assert v is not None
        assert v.hedge_delta_band_shares == 10
