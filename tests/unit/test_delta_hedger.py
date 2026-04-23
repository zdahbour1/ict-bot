"""ENH-049 Stage 1+2 — DN delta-hedger unit tests.

Covers the pure-function math (compute_trade_net_delta,
compute_rebalance_order) plus the loop skip when the flag is off.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


def _condor_legs(underlying=500.0, expiry="20260515"):
    """Standard 4-leg iron condor around the money."""
    return [
        {"leg_role": "short_call", "sec_type": "OPT", "symbol": "X",
         "strike": underlying, "right": "C", "expiry": expiry,
         "multiplier": 100, "direction": "SHORT", "contracts_open": 1},
        {"leg_role": "long_call", "sec_type": "OPT", "symbol": "X",
         "strike": underlying + 10, "right": "C", "expiry": expiry,
         "multiplier": 100, "direction": "LONG", "contracts_open": 1},
        {"leg_role": "short_put", "sec_type": "OPT", "symbol": "X",
         "strike": underlying, "right": "P", "expiry": expiry,
         "multiplier": 100, "direction": "SHORT", "contracts_open": 1},
        {"leg_role": "long_put", "sec_type": "OPT", "symbol": "X",
         "strike": underlying - 10, "right": "P", "expiry": expiry,
         "multiplier": 100, "direction": "LONG", "contracts_open": 1},
    ]


class TestComputeTradeNetDelta:
    def test_condor_at_body_near_zero_delta(self):
        from strategy.delta_hedger import compute_trade_net_delta
        # At-the-money iron condor: delta ~0 because short+long calls
        # and short+long puts roughly cancel.
        net = compute_trade_net_delta(_condor_legs(500.0), 500.0)
        # A week out at 500 ATM — asymmetry tiny.
        assert abs(net) < 30, (
            f"ATM condor should have near-zero delta, got {net}"
        )

    def test_condor_skewed_up_has_negative_delta(self):
        """When underlying rallies above the short call strike, the
        short call dominates and net delta goes negative."""
        from strategy.delta_hedger import compute_trade_net_delta
        net = compute_trade_net_delta(_condor_legs(500.0), 520.0)
        assert net < 0, (
            f"Underlying above short-call strike should show negative "
            f"net delta, got {net}"
        )

    def test_condor_skewed_down_has_positive_delta(self):
        from strategy.delta_hedger import compute_trade_net_delta
        net = compute_trade_net_delta(_condor_legs(500.0), 480.0)
        assert net > 0

    def test_stk_leg_counts_as_raw_shares(self):
        """A stock hedge leg should pass through as share-count."""
        from strategy.delta_hedger import compute_trade_net_delta
        legs = [
            {"leg_role": "delta_hedge", "sec_type": "STK", "symbol": "SPY",
             "multiplier": 1, "direction": "LONG", "contracts_open": 5},
        ]
        net = compute_trade_net_delta(legs, 500.0)
        # LONG 5 shares of stock = +5 delta exactly.
        assert net == pytest.approx(5.0)

    def test_zero_qty_leg_contributes_nothing(self):
        from strategy.delta_hedger import compute_trade_net_delta
        legs = [{"leg_role": "short_call", "sec_type": "OPT", "symbol": "X",
                  "strike": 500, "right": "C", "expiry": "20260515",
                  "multiplier": 100, "direction": "SHORT", "contracts_open": 0}]
        assert compute_trade_net_delta(legs, 500.0) == 0.0


class TestComputeRebalanceOrder:
    def test_within_band_returns_none(self):
        from strategy.delta_hedger import compute_rebalance_order
        assert compute_rebalance_order(net_delta=10, current_hedge_shares=0,
                                        band=20) is None
        assert compute_rebalance_order(net_delta=-15, current_hedge_shares=0,
                                        band=20) is None

    def test_positive_residual_triggers_sell(self):
        """Position is net long → sell shares to flatten."""
        from strategy.delta_hedger import compute_rebalance_order
        # net_delta=+40, no hedge, band=20 → need to SELL 40 shares
        res = compute_rebalance_order(net_delta=40, current_hedge_shares=0,
                                       band=20)
        assert res == ("SELL", 40)

    def test_negative_residual_triggers_buy(self):
        """Position is net short → buy shares to flatten."""
        from strategy.delta_hedger import compute_rebalance_order
        res = compute_rebalance_order(net_delta=-35, current_hedge_shares=0,
                                       band=20)
        assert res == ("BUY", 35)

    def test_existing_hedge_reduces_residual(self):
        """Already hedged → smaller flat-to-zero order."""
        from strategy.delta_hedger import compute_rebalance_order
        # net_delta=+50, hedge=-40 (short 40) → residual=+10 → no action
        assert compute_rebalance_order(net_delta=50, current_hedge_shares=-40,
                                        band=20) is None
        # net_delta=+50, hedge=-10 → residual=+40 → SELL 40
        assert compute_rebalance_order(net_delta=50, current_hedge_shares=-10,
                                        band=20) == ("SELL", 40)

    def test_overhedged_reverses_direction(self):
        """If we're over-hedged short, we need to BUY back some."""
        from strategy.delta_hedger import compute_rebalance_order
        # Short 100 shares but option delta only +30 → residual=-70 → BUY 70
        res = compute_rebalance_order(net_delta=30, current_hedge_shares=-100,
                                       band=20)
        assert res == ("BUY", 70)


class TestHedgerFlagGating:
    def test_loop_noop_when_flag_off(self):
        from strategy.delta_hedger import DeltaHedger
        hedger = DeltaHedger(client=MagicMock())
        with patch("db.settings_cache.get_bool", return_value=False):
            with patch.object(hedger, "_refresh_config") as refresh:
                hedger._one_pass()
                # When disabled, we shouldn't even refresh config or hit DB.
                refresh.assert_not_called()

    def test_loop_runs_when_flag_on(self):
        from strategy.delta_hedger import DeltaHedger
        hedger = DeltaHedger(client=MagicMock())
        with patch("db.settings_cache.get_bool", return_value=True), \
             patch.object(hedger, "_refresh_config") as refresh, \
             patch("strategy.delta_hedger._fetch_open_dn_trades",
                    return_value=[]):
            hedger._one_pass()
            refresh.assert_called_once()


class TestThreadStatusHeartbeat:
    """ENH-049 — the hedger must register as 'delta-hedger' in
    thread_status on every pass so the Threads dashboard shows it."""

    def test_flag_off_heartbeats_idle(self):
        from strategy.delta_hedger import DeltaHedger
        hedger = DeltaHedger(client=MagicMock())
        with patch("db.settings_cache.get_bool", return_value=False), \
             patch("strategy.delta_hedger._update_thread_row") as hb:
            hedger._one_pass()
        hb.assert_called_once()
        args = hb.call_args.args
        assert args[0] == "idle"
        assert "false" in args[1] or "monitor" in args[1].lower()

    def test_flag_on_no_trades_heartbeats_running(self):
        from strategy.delta_hedger import DeltaHedger
        hedger = DeltaHedger(client=MagicMock())
        with patch("db.settings_cache.get_bool", return_value=True), \
             patch("strategy.delta_hedger._fetch_open_dn_trades",
                    return_value=[]), \
             patch.object(hedger, "_refresh_config"), \
             patch("strategy.delta_hedger._update_thread_row") as hb:
            hedger._one_pass()
        hb.assert_called_once()
        assert hb.call_args.args[0] == "running"

    def test_flag_on_with_trades_heartbeats_running_with_count(self):
        from strategy.delta_hedger import DeltaHedger
        hedger = DeltaHedger(client=MagicMock())
        fake_trades = [
            {"trade_id": 1, "ticker": "SPY", "legs": [], "hedge_shares": 0},
            {"trade_id": 2, "ticker": "QQQ", "legs": [], "hedge_shares": 0},
        ]
        with patch("db.settings_cache.get_bool", return_value=True), \
             patch("strategy.delta_hedger._fetch_open_dn_trades",
                    return_value=fake_trades), \
             patch.object(hedger, "_refresh_config"), \
             patch.object(hedger, "_rebalance_one"), \
             patch("strategy.delta_hedger._update_thread_row") as hb:
            hedger._one_pass()
        hb.assert_called_once()
        assert hb.call_args.args[0] == "running"
        assert "2" in hb.call_args.args[1]


class TestDTEdays:
    def test_future_expiry(self):
        from strategy.delta_hedger import _dte_days
        # 2026-05-01 is 8 days from 2026-04-23
        days = _dte_days("20260501",
                         now=datetime(2026, 4, 23, tzinfo=timezone.utc))
        assert days == 8.0

    def test_past_expiry_clamped(self):
        from strategy.delta_hedger import _dte_days
        days = _dte_days("20260101",
                         now=datetime(2026, 4, 23, tzinfo=timezone.utc))
        assert days == 0.0

    def test_missing_expiry_default(self):
        from strategy.delta_hedger import _dte_days
        assert _dte_days(None) == 7.0
        assert _dte_days("") == 7.0
