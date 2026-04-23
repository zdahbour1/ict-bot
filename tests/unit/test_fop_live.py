"""Unit tests for ENH-034 live FOP contract selection.

The selector is split into pure helpers (deterministic) and IB-touching
main entry that takes injected probe functions so tests can drive it
without a real IB connection.
"""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

from strategy.fop_selector import (
    FOPSelection, candidate_strikes, classify_expiry, passes_liquidity_gate,
    prefer_order, round_to_grid, select_liquid_fop_contract,
)


# ─── Pure helpers ────────────────────────────────────────────

class TestClassifyExpiry:
    def test_quarterly_june_2026_is_thursday_before_third_friday(self):
        # June 2026: 3rd Friday = 20260619 → option expires Thursday 20260618
        assert classify_expiry("20260618", today=date(2026, 4, 22)) == "quarterly"

    def test_monthly_may_2026(self):
        # May 2026: 3rd Friday = 20260515 → monthly option expires 20260514 (Thu)
        assert classify_expiry("20260514", today=date(2026, 4, 22)) == "monthly"

    def test_weekly_first_friday_of_may(self):
        # 20260501 is a Friday but NOT the 3rd — weekly.
        assert classify_expiry("20260501", today=date(2026, 4, 22)) == "weekly"

    def test_daily_arbitrary_wednesday(self):
        # 20260429 Wednesday — not 3rd-Thursday, not Friday → daily
        assert classify_expiry("20260429", today=date(2026, 4, 22)) == "daily"

    def test_past_expiry(self):
        assert classify_expiry("20260101", today=date(2026, 4, 22)) == "past"

    def test_unparseable_string(self):
        assert classify_expiry("not-a-date", today=date(2026, 4, 22)) == "daily"


class TestRoundToGrid:
    def test_rounds_up_when_halfway(self):
        assert round_to_grid(5437.5, 5) == 5440.0   # banker's rounding is fine

    def test_rounds_down_below_half(self):
        assert round_to_grid(5436.4, 5) == 5435.0

    def test_interval_zero_returns_price(self):
        assert round_to_grid(123.45, 0) == 123.45


class TestCandidateStrikes:
    def test_atm_first(self):
        strikes = candidate_strikes(5500, 5, "LONG", depth=2)
        assert strikes[0] == 5500.0

    def test_long_prefers_otm_call_after_atm(self):
        # LONG = buying calls → OTM call (higher strike) preferred after ATM.
        strikes = candidate_strikes(5500, 5, "LONG", depth=1)
        assert strikes == [5500.0, 5505.0, 5495.0]

    def test_short_prefers_otm_put_after_atm(self):
        strikes = candidate_strikes(5500, 5, "SHORT", depth=1)
        assert strikes == [5500.0, 5495.0, 5505.0]


class TestPassesLiquidityGate:
    def test_passes_when_all_metrics_healthy(self):
        q = {"bid": 10.0, "ask": 10.2, "volume": 500, "open_interest": 2000}
        ok, reason = passes_liquidity_gate(q, min_open_interest=500,
                                            min_volume=100, max_spread_pct=0.15)
        assert ok is True
        assert reason is None

    def test_rejects_low_open_interest(self):
        q = {"bid": 10, "ask": 10.1, "volume": 500, "open_interest": 100}
        ok, reason = passes_liquidity_gate(q, min_open_interest=500,
                                            min_volume=100, max_spread_pct=0.15)
        assert ok is False
        assert "OI too low" in reason

    def test_rejects_low_volume(self):
        q = {"bid": 10, "ask": 10.1, "volume": 5, "open_interest": 2000}
        ok, reason = passes_liquidity_gate(q, min_open_interest=500,
                                            min_volume=100, max_spread_pct=0.15)
        assert ok is False
        assert "volume too low" in reason

    def test_rejects_wide_spread(self):
        # mid = 10, spread = 4 → 40%
        q = {"bid": 8, "ask": 12, "volume": 500, "open_interest": 2000}
        ok, reason = passes_liquidity_gate(q, min_open_interest=500,
                                            min_volume=100, max_spread_pct=0.15)
        assert ok is False
        assert "spread too wide" in reason

    def test_rejects_bad_quote(self):
        q = {"bid": 0, "ask": 0, "volume": 500, "open_interest": 2000}
        ok, reason = passes_liquidity_gate(q, min_open_interest=500,
                                            min_volume=100, max_spread_pct=0.15)
        assert ok is False
        assert "bad quote" in reason


class TestPreferOrder:
    def test_defaults_used_when_config_missing(self):
        with patch("strategy.fop_selector.config") as cfg:
            cfg.FOP_EXPIRY_PREF = "quarterly,monthly,weekly"
            assert prefer_order() == ["quarterly", "monthly", "weekly"]

    def test_parses_daily_allowed(self):
        with patch("strategy.fop_selector.config") as cfg:
            cfg.FOP_EXPIRY_PREF = "monthly,daily"
            assert prefer_order() == ["monthly", "daily"]

    def test_ignores_invalid_entries(self):
        with patch("strategy.fop_selector.config") as cfg:
            cfg.FOP_EXPIRY_PREF = "quarterly,bogus,weekly"
            assert prefer_order() == ["quarterly", "weekly"]


# ─── Integration with injected probes (still no IB) ─────────

class TestSelectLiquidFopContract:
    def _chain_probe(self, underlying, exchange):
        # 4 expiries spanning the types: quarterly, monthly, weekly, daily
        return [
            {"expiry": "20260429"},  # Wed — daily
            {"expiry": "20260501"},  # Fri not-3rd — weekly
            {"expiry": "20260514"},  # Thu before 3rd-Friday (May monthly)
            {"expiry": "20260618"},  # Thu before 3rd-Friday (Jun quarterly)
        ]

    def test_prefers_quarterly_when_liquid(self):
        probe_calls = []
        def quote_probe(sym, exc, exp, strike, right, mult):
            probe_calls.append((exp, strike, right))
            # Make the quarterly liquid at ATM, everything else dead.
            if exp == "20260618" and strike == 5500.0:
                return {"bid": 20.0, "ask": 20.5, "volume": 2000,
                        "open_interest": 10000, "con_id": 999}
            return {"bid": 0, "ask": 0, "volume": 0, "open_interest": 0}

        sel = select_liquid_fop_contract(
            self._chain_probe, quote_probe,
            underlying="MES", direction="LONG",
            underlying_price=5500.0, today=date(2026, 4, 22),
        )
        assert sel is not None
        assert sel.expiry == "20260618"
        assert sel.expiry_type == "quarterly"
        assert sel.strike == 5500.0
        assert sel.right == "C"
        assert sel.con_id == 999
        assert sel.open_interest == 10000

    def test_falls_back_to_monthly_when_quarterly_thin(self):
        def quote_probe(sym, exc, exp, strike, right, mult):
            # Quarterly everywhere illiquid; monthly at ATM is fine.
            if exp == "20260514" and strike == 5500.0:
                return {"bid": 15.0, "ask": 15.2, "volume": 600,
                        "open_interest": 3000, "con_id": 888}
            return {"bid": 1.0, "ask": 3.0, "volume": 10, "open_interest": 5}

        sel = select_liquid_fop_contract(
            self._chain_probe, quote_probe,
            underlying="MES", direction="LONG",
            underlying_price=5500.0, today=date(2026, 4, 22),
        )
        assert sel is not None
        assert sel.expiry == "20260514"
        assert sel.expiry_type == "monthly"

    def test_returns_none_when_no_liquid_contract_exists(self):
        def quote_probe(sym, exc, exp, strike, right, mult):
            return {"bid": 0.1, "ask": 5.0, "volume": 1, "open_interest": 10}

        sel = select_liquid_fop_contract(
            self._chain_probe, lambda *a, **k: {"bid": 0, "ask": 0,
                                                  "volume": 0, "open_interest": 0},
            underlying="MES", direction="LONG",
            underlying_price=5500.0, today=date(2026, 4, 22),
        )
        assert sel is None

    def test_unknown_underlying_returns_none(self):
        sel = select_liquid_fop_contract(
            lambda *a: [], lambda *a, **k: {},
            underlying="XYZ",   # not in FOP_SPECS
            direction="LONG", underlying_price=100,
        )
        assert sel is None

    def test_short_direction_picks_put(self):
        def quote_probe(sym, exc, exp, strike, right, mult):
            if exp == "20260618" and strike == 5500.0 and right == "P":
                return {"bid": 18.0, "ask": 18.5, "volume": 2000,
                        "open_interest": 10000, "con_id": 777}
            return {"bid": 0, "ask": 0, "volume": 0, "open_interest": 0}
        sel = select_liquid_fop_contract(
            self._chain_probe, quote_probe,
            underlying="MES", direction="SHORT",
            underlying_price=5500.0, today=date(2026, 4, 22),
        )
        assert sel is not None
        assert sel.right == "P"

    def test_skips_expiries_beyond_max_dte(self):
        # Pin max_dte=10 so only 20260429 (daily) and 20260501 (weekly)
        # are in the window. Quarterly/monthly (22+ days out) are excluded.
        # With weekly in the prefer order, the Friday weekly wins.
        def quote_probe(sym, exc, exp, strike, right, mult):
            return {"bid": 5.0, "ask": 5.1, "volume": 1000,
                    "open_interest": 5000, "con_id": 111}
        with patch("strategy.fop_selector.config") as cfg:
            cfg.FOP_MAX_DTE = 10
            cfg.FOP_MIN_OPEN_INTEREST = 500
            cfg.FOP_MIN_VOLUME = 100
            cfg.FOP_MAX_SPREAD_PCT = 0.15
            cfg.FOP_EXPIRY_PREF = "quarterly,monthly,weekly,daily"
            sel = select_liquid_fop_contract(
                self._chain_probe, quote_probe,
                underlying="MES", direction="LONG",
                underlying_price=5500.0, today=date(2026, 4, 22),
            )
        # Quarterly + monthly skipped (beyond window). Weekly picked first
        # because it comes before daily in the prefer order.
        assert sel is not None
        assert sel.expiry == "20260501"
        assert sel.expiry_type == "weekly"
