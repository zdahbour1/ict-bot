"""ENH-066 — backtest delta-hedge simulation tests.

Locks in the math for the hedge sim added to dn_variants_engine.
The live ``strategy/delta_hedger.py`` and the sim share the same
sign convention so backtest predictions translate to production.
"""
from __future__ import annotations

import pytest

from backtest_engine.dn_variants_engine import (
    _net_option_delta_shares,
    _rebalance_hedge,
    _hedge_pnl_at_close,
)


def _condor_legs(atm: float = 500.0, wing: float = 10.0):
    """ATM iron butterfly (used for ZDN tests)."""
    return [
        {"role": "short_call", "right": "C", "strike": atm,
         "direction": "SHORT"},
        {"role": "long_call", "right": "C", "strike": atm + wing,
         "direction": "LONG"},
        {"role": "short_put", "right": "P", "strike": atm,
         "direction": "SHORT"},
        {"role": "long_put", "right": "P", "strike": atm - wing,
         "direction": "LONG"},
    ]


class TestNetOptionDelta:
    def test_atm_butterfly_is_near_zero_delta(self):
        """Defining property of an ATM iron butterfly: short call
        delta ≈ +0.5, short put delta ≈ -0.5; long wings small. Net
        ≈ zero before any hedge."""
        legs = _condor_legs(atm=500.0, wing=10.0)
        delta = _net_option_delta_shares(
            legs, underlying=500.0, dte_days=7.0, sigma=0.20,
            contracts=1)
        assert abs(delta) < 5.0, (
            f"ATM butterfly should be near-flat, got {delta:.1f}")

    def test_drift_creates_directional_delta(self):
        """When underlying moves up away from ATM, the structure
        gains negative delta (we're net short upside via short call)."""
        legs = _condor_legs(atm=500.0, wing=10.0)
        delta_at_atm = _net_option_delta_shares(
            legs, 500.0, 7.0, 0.20, 1)
        delta_drifted = _net_option_delta_shares(
            legs, 505.0, 7.0, 0.20, 1)
        assert delta_drifted < delta_at_atm
        # And meaningfully so (not just numerical noise) — at $5
        # drift on an ATM $10-wing structure with 7 DTE, delta swing
        # is ~6 share-equivalents per contract.
        assert delta_drifted - delta_at_atm < -3.0


class TestRebalanceHedge:
    def test_within_band_returns_zero(self):
        legs = _condor_legs(500.0, 10.0)
        trade = {"contracts": 1, "legs": legs, "hedge_shares": 0}
        # At ATM, net delta is tiny → within ±20 shares
        qty, cash = _rebalance_hedge(trade, 500.0, 7.0, 0.20,
                                       band_shares=20)
        assert qty == 0
        assert cash == 0.0

    def test_long_delta_residual_triggers_short_sell(self):
        """If position is net long shares, hedge sells (qty_change<0)
        and receives cash (cash_flow>0). Use band=2 so even a small
        delta drift triggers."""
        legs = _condor_legs(500.0, 10.0)
        trade = {"contracts": 1, "legs": legs, "hedge_shares": 0}
        # Move underlying down — net delta goes positive
        qty, cash = _rebalance_hedge(trade, 495.0, 7.0, 0.20,
                                       band_shares=2)
        assert qty < 0, f"Expected SELL (qty<0) for long residual, got {qty}"
        assert cash > 0, f"SELL must produce positive cash flow, got {cash}"
        # Cash flow magnitude is qty × price
        assert abs(cash - (-qty * 495.0)) < 0.01

    def test_short_delta_residual_triggers_buy(self):
        legs = _condor_legs(500.0, 10.0)
        trade = {"contracts": 1, "legs": legs, "hedge_shares": 0}
        # Move underlying up → short call gains ITM-ness → net short
        qty, cash = _rebalance_hedge(trade, 505.0, 7.0, 0.20,
                                       band_shares=2)
        assert qty > 0, f"Expected BUY (qty>0) for short residual, got {qty}"
        assert cash < 0, f"BUY must produce negative cash flow, got {cash}"

    def test_existing_hedge_reduces_residual(self):
        """If we already hold a hedge from a previous rebalance, the
        next rebalance fires for less than the bare-leg delta would
        suggest. Concretely: bare delta + hedge_shares gets compared
        to band, not bare delta alone."""
        legs = _condor_legs(500.0, 10.0)
        # Compute bare delta first, no hedge
        bare_qty, _ = _rebalance_hedge(
            {"contracts": 1, "legs": legs, "hedge_shares": 0},
            505.0, 7.0, 0.20, band_shares=2)
        # Apply that as existing hedge → now next rebalance is small/zero
        with_hedge_qty, _ = _rebalance_hedge(
            {"contracts": 1, "legs": legs, "hedge_shares": bare_qty},
            505.0, 7.0, 0.20, band_shares=2)
        assert abs(with_hedge_qty) < abs(bare_qty), (
            f"Existing hedge {bare_qty} should reduce additional "
            f"rebalance qty, but bare={bare_qty} → "
            f"with_hedge={with_hedge_qty}")


class TestHedgePnLAtClose:
    def test_long_hedge_realizes_at_exit_price(self):
        """If hedge_shares=+50 and we paid $25k to acquire them, then
        flatten at $510 → realize 50 × 510 = +$25,500. Net hedge_pnl
        running = -25,000 (cash out from buy) + 25,500 (cash in from
        flatten) = +$500."""
        # Caller tracks running cash: bought 50 shares at $500 → -25,000
        trade = {"hedge_shares": 50, "hedge_pnl": -25_000.0}
        final = _hedge_pnl_at_close(trade, exit_price=510.0)
        assert abs(final - 500.0) < 0.01

    def test_short_hedge_realizes_at_exit_price(self):
        """Sold 50 shares short at $500 → cash in $25,000. Cover at
        $490 → cash out $24,500. Net = +500."""
        trade = {"hedge_shares": -50, "hedge_pnl": 25_000.0}
        # Flatten at 490: hedge_shares × exit_price = -50 × 490 = -24,500
        # final = 25,000 + (-24,500) = 500
        final = _hedge_pnl_at_close(trade, exit_price=490.0)
        assert abs(final - 500.0) < 0.01

    def test_zero_hedge_returns_running_pnl(self):
        trade = {"hedge_shares": 0, "hedge_pnl": 123.45}
        final = _hedge_pnl_at_close(trade, exit_price=500.0)
        assert abs(final - 123.45) < 0.01

    def test_no_hedge_state_returns_zero(self):
        trade = {}
        final = _hedge_pnl_at_close(trade, exit_price=500.0)
        assert final == 0.0


class TestRebalanceAccountingIdentity:
    """Self-check: a rebalance that BUYs N shares followed by an
    immediate flatten at the same price should leave hedge_pnl at 0
    (no slippage in sim)."""

    def test_buy_then_flatten_zero_pnl(self):
        legs = _condor_legs(500.0, 10.0)
        trade = {"contracts": 1, "legs": legs,
                 "hedge_shares": 0, "hedge_pnl": 0.0}
        # Force a rebalance at 505 (drift up → buy shares)
        qty, cash = _rebalance_hedge(trade, 505.0, 7.0, 0.20,
                                       band_shares=2)
        assert qty > 0   # BUY
        trade["hedge_shares"] += qty
        trade["hedge_pnl"] += cash
        # Now flatten at the same 505
        final = _hedge_pnl_at_close(trade, exit_price=505.0)
        assert abs(final) < 0.01, (
            f"Buy→flatten at same price must net to 0, got {final:.4f}")

    def test_sell_then_flatten_zero_pnl(self):
        legs = _condor_legs(500.0, 10.0)
        trade = {"contracts": 1, "legs": legs,
                 "hedge_shares": 0, "hedge_pnl": 0.0}
        qty, cash = _rebalance_hedge(trade, 495.0, 7.0, 0.20,
                                       band_shares=2)
        assert qty < 0   # SELL
        trade["hedge_shares"] += qty
        trade["hedge_pnl"] += cash
        final = _hedge_pnl_at_close(trade, exit_price=495.0)
        assert abs(final) < 0.01
