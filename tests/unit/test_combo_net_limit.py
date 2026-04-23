"""IB slippage fix: combo auto-limit unit tests.

Locks in the contract of ``_compute_combo_net_limit`` — the helper
that turns 4 per-leg mid-quotes into a single net LimitPrice for a
BAG/combo order submission. Without this, combos hit IB as pure
MarketOrders and eat the full cross-the-spread slippage on every
leg. See also ``docs/enh_050_combo_leg_fill_price.md`` for the
complementary fill-price-back-fill work.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _condor(strikes=(500, 510, 500, 490)):
    sc, lc, sp, lp = strikes
    return [
        {"symbol": f"X{sc}C", "direction": "SHORT", "contracts": 1,
         "strike": sc, "right": "C", "expiry": "20260515", "leg_role": "short_call"},
        {"symbol": f"X{lc}C", "direction": "LONG",  "contracts": 1,
         "strike": lc, "right": "C", "expiry": "20260515", "leg_role": "long_call"},
        {"symbol": f"X{sc}P", "direction": "SHORT", "contracts": 1,
         "strike": sc, "right": "P", "expiry": "20260515", "leg_role": "short_put"},
        {"symbol": f"X{lp}P", "direction": "LONG",  "contracts": 1,
         "strike": lp, "right": "P", "expiry": "20260515", "leg_role": "long_put"},
    ]


def _leg_contracts(legs):
    """Shape required by _compute_combo_net_limit: [(i, leg, contract)]."""
    return [(i, leg, SimpleNamespace(conId=12345 + i))
            for i, leg in enumerate(legs)]


class _MockMixin:
    """Stand-in for IBClient — only method used is get_option_price."""
    def __init__(self, prices):
        self._prices = prices
    def get_option_price(self, symbol):
        return self._prices.get(symbol, 0.0)


class TestComboNetLimit:
    def test_classic_credit_condor_returns_positive_net_credit(self):
        """Iron condor at the body:
          short_call + short_put = credits (we receive)
          long_call  + long_put  = debits  (we pay)
        Net should be a positive credit. Submit as SELL with a
        buffer-reduced limit."""
        from broker.ib_orders import _compute_combo_net_limit
        legs = _condor()
        prices = {"X500C": 2.50, "X510C": 0.80,   # calls
                   "X500P": 2.30, "X490P": 0.60}   # puts
        mixin = _MockMixin(prices)
        with patch("db.settings_cache.get_bool", return_value=True), \
             patch("db.settings_cache.get_float", return_value=200.0):
            limit = _compute_combo_net_limit(
                mixin, _leg_contracts(legs), action="SELL", legs=legs,
            )
        # Net premium = +2.50 -0.80 +2.30 -0.60 = +3.40
        # Buffer = 3.40 * 0.02 = 0.068 → rounded
        # SELL limit = 3.40 - 0.068 = ~3.33
        assert limit is not None
        assert 3.30 <= limit <= 3.36, f"unexpected limit {limit}"

    def test_auto_limit_disabled_returns_none(self):
        from broker.ib_orders import _compute_combo_net_limit
        mixin = _MockMixin({"X500C": 1.0})
        with patch("db.settings_cache.get_bool", return_value=False):
            limit = _compute_combo_net_limit(
                mixin, _leg_contracts(_condor()), action="SELL",
                legs=_condor(),
            )
        assert limit is None

    def test_missing_leg_quote_returns_none_so_caller_falls_back_to_mkt(self):
        """If any leg's quote is 0/unavailable, we can't trust the net
        so we return None → caller sends MarketOrder."""
        from broker.ib_orders import _compute_combo_net_limit
        legs = _condor()
        # long_call has no quote
        prices = {"X500C": 2.50, "X510C": 0.0, "X500P": 2.30, "X490P": 0.60}
        mixin = _MockMixin(prices)
        with patch("db.settings_cache.get_bool", return_value=True), \
             patch("db.settings_cache.get_float", return_value=200.0):
            limit = _compute_combo_net_limit(
                mixin, _leg_contracts(legs), action="SELL", legs=legs,
            )
        assert limit is None

    def test_slippage_buffer_widens_limit(self):
        """Higher slip_bps → more aggressive limit (SELL lower, BUY
        higher)."""
        from broker.ib_orders import _compute_combo_net_limit
        legs = _condor()
        prices = {"X500C": 2.50, "X510C": 0.80, "X500P": 2.30, "X490P": 0.60}
        mixin = _MockMixin(prices)
        # Tight buffer (50 bps = 0.5%)
        with patch("db.settings_cache.get_bool", return_value=True), \
             patch("db.settings_cache.get_float", return_value=50.0):
            tight = _compute_combo_net_limit(
                mixin, _leg_contracts(legs), action="SELL", legs=legs,
            )
        # Wide buffer (1000 bps = 10%)
        with patch("db.settings_cache.get_bool", return_value=True), \
             patch("db.settings_cache.get_float", return_value=1000.0):
            wide = _compute_combo_net_limit(
                mixin, _leg_contracts(legs), action="SELL", legs=legs,
            )
        # Wider slippage = LOWER SELL limit (we're willing to take
        # less credit to guarantee fill)
        assert wide < tight, (
            f"wider slip should lower SELL limit — tight={tight}, wide={wide}"
        )

    def test_never_returns_negative_limit(self):
        """IB rejects negative limit prices on spreads; we clamp."""
        from broker.ib_orders import _compute_combo_net_limit
        # Contrived: all long debits → net would be negative without clamp
        legs = [
            {"symbol": "A", "direction": "LONG", "contracts": 1,
             "strike": 100, "right": "C", "expiry": "20260515"},
            {"symbol": "B", "direction": "LONG", "contracts": 1,
             "strike": 100, "right": "P", "expiry": "20260515"},
        ]
        prices = {"A": 1.50, "B": 1.50}
        mixin = _MockMixin(prices)
        with patch("db.settings_cache.get_bool", return_value=True), \
             patch("db.settings_cache.get_float", return_value=500.0):
            limit = _compute_combo_net_limit(
                mixin, _leg_contracts(legs), action="SELL", legs=legs,
            )
        assert limit is not None and limit >= 0.05, (
            f"clamp must enforce >= 0.05, got {limit}"
        )
