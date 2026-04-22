"""Unit tests for Phase 6b multi-leg exit path.

Verifies:
- execute_exit routes to _execute_multi_leg_exit when trade n_legs > 1
- Single-leg trades (n_legs=1 or missing) still go through
  _execute_exit_sell_first (zero regression)
- _close_action_for_leg maps (direction, right) → correct IB method
- _execute_multi_leg_exit sends one close order per leg
- Stock legs are skipped with a warning (TODO flagged)
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest


class TestCloseActionForLeg:
    def test_long_call_sells_to_close(self):
        from strategy.exit_executor import _close_action_for_leg
        leg = {"sec_type": "OPT", "direction": "LONG", "right": "C"}
        assert _close_action_for_leg(leg) == ("sell_call", "sell-to-close long call")

    def test_long_put_sells_to_close(self):
        from strategy.exit_executor import _close_action_for_leg
        leg = {"sec_type": "OPT", "direction": "LONG", "right": "P"}
        assert _close_action_for_leg(leg) == ("sell_put", "sell-to-close long put")

    def test_short_call_buys_to_close(self):
        from strategy.exit_executor import _close_action_for_leg
        leg = {"sec_type": "OPT", "direction": "SHORT", "right": "C"}
        assert _close_action_for_leg(leg) == ("buy_call", "buy-to-close short call")

    def test_short_put_buys_to_close(self):
        from strategy.exit_executor import _close_action_for_leg
        leg = {"sec_type": "OPT", "direction": "SHORT", "right": "P"}
        assert _close_action_for_leg(leg) == ("buy_put", "buy-to-close short put")

    def test_fop_treated_as_options(self):
        from strategy.exit_executor import _close_action_for_leg
        # FOP close semantics are identical to OPT — sell/buy_call/put
        # work on both. Only STK is rejected (needs its own close path).
        leg = {"sec_type": "FOP", "direction": "LONG", "right": "C"}
        assert _close_action_for_leg(leg) == ("sell_call", "sell-to-close long call")
        leg = {"sec_type": "FOP", "direction": "SHORT", "right": "P"}
        assert _close_action_for_leg(leg) == ("buy_put", "buy-to-close short put")

    def test_stock_leg_returns_none(self):
        from strategy.exit_executor import _close_action_for_leg
        leg = {"sec_type": "STK", "direction": "LONG", "right": None}
        assert _close_action_for_leg(leg) is None

    def test_bad_shape_returns_none(self):
        from strategy.exit_executor import _close_action_for_leg
        assert _close_action_for_leg({"sec_type": "OPT"}) is None
        assert _close_action_for_leg({}) is None


class TestExecuteExitRouting:
    def test_single_leg_goes_to_sell_first_path(self):
        """n_legs=1 or missing → sell_first path (unchanged behavior)."""
        from strategy.exit_executor import execute_exit
        client = MagicMock()
        with patch("strategy.exit_executor._execute_exit_sell_first",
                   return_value=None) as mock_sf, \
             patch("strategy.exit_executor._execute_multi_leg_exit") as mock_ml:
            execute_exit(client, {"ticker": "SPY", "db_id": 1, "n_legs": 1}, "TEST")
            mock_sf.assert_called_once()
            mock_ml.assert_not_called()

    def test_missing_n_legs_goes_to_single_leg(self):
        """Legacy trade without n_legs key → single-leg path."""
        from strategy.exit_executor import execute_exit
        client = MagicMock()
        with patch("strategy.exit_executor._execute_exit_sell_first",
                   return_value=None) as mock_sf, \
             patch("strategy.exit_executor._execute_multi_leg_exit") as mock_ml:
            execute_exit(client, {"ticker": "SPY", "db_id": 1}, "TEST")
            mock_sf.assert_called_once()
            mock_ml.assert_not_called()

    def test_multi_leg_routes_to_multi_leg_path(self):
        """n_legs>1 → multi-leg path."""
        from strategy.exit_executor import execute_exit
        client = MagicMock()
        with patch("strategy.exit_executor._execute_multi_leg_exit",
                   return_value=None) as mock_ml, \
             patch("strategy.exit_executor._execute_exit_sell_first") as mock_sf:
            execute_exit(client, {"ticker": "SPY", "db_id": 1, "n_legs": 4}, "TEST")
            mock_ml.assert_called_once()
            mock_sf.assert_not_called()


class TestExecuteMultiLegExit:
    def _iron_condor_legs(self):
        """4-leg iron condor: short ATM call + long OTM call + short ATM put + long OTM put."""
        return [
            {"leg_id": 1, "leg_index": 0, "leg_role": "short_call",
             "sec_type": "OPT", "symbol": "SPY260515C00450000",
             "right": "C", "strike": 450.0, "expiry": "20260515",
             "direction": "SHORT", "contracts_open": 2, "entry_price": 2.5,
             "ib_con_id": 101, "ib_tp_perm_id": 1001, "ib_sl_perm_id": None},
            {"leg_id": 2, "leg_index": 1, "leg_role": "long_call",
             "sec_type": "OPT", "symbol": "SPY260515C00460000",
             "right": "C", "strike": 460.0, "expiry": "20260515",
             "direction": "LONG", "contracts_open": 2, "entry_price": 1.2,
             "ib_con_id": 102, "ib_tp_perm_id": None, "ib_sl_perm_id": 1002},
            {"leg_id": 3, "leg_index": 2, "leg_role": "short_put",
             "sec_type": "OPT", "symbol": "SPY260515P00440000",
             "right": "P", "strike": 440.0, "expiry": "20260515",
             "direction": "SHORT", "contracts_open": 2, "entry_price": 2.2,
             "ib_con_id": 103, "ib_tp_perm_id": 1003, "ib_sl_perm_id": None},
            {"leg_id": 4, "leg_index": 3, "leg_role": "long_put",
             "sec_type": "OPT", "symbol": "SPY260515P00430000",
             "right": "P", "strike": 430.0, "expiry": "20260515",
             "direction": "LONG", "contracts_open": 2, "entry_price": 1.1,
             "ib_con_id": 104, "ib_tp_perm_id": None, "ib_sl_perm_id": 1004},
        ]

    def test_sends_one_close_order_per_leg(self):
        from strategy.exit_executor import _execute_multi_leg_exit
        client = MagicMock()
        trade = {"ticker": "SPY", "db_id": 42, "n_legs": 4}
        with patch("strategy.exit_executor._fetch_open_legs",
                   return_value=self._iron_condor_legs()):
            _execute_multi_leg_exit(client, trade, reason="TEST")
        # 4 close orders total: 2 buy_call (short calls), 2 sell_put/sell_call
        # Actually: iron condor = short_call + long_call + short_put + long_put
        # Close actions: buy_call, sell_call, buy_put, sell_put
        client.buy_call.assert_called_once_with("SPY260515C00450000", 2)
        client.sell_call.assert_called_once_with("SPY260515C00460000", 2)
        client.buy_put.assert_called_once_with("SPY260515P00440000", 2)
        client.sell_put.assert_called_once_with("SPY260515P00430000", 2)

    def test_cancels_bracket_perm_ids_per_leg(self):
        from strategy.exit_executor import _execute_multi_leg_exit
        client = MagicMock()
        trade = {"ticker": "SPY", "db_id": 42, "n_legs": 4}
        with patch("strategy.exit_executor._fetch_open_legs",
                   return_value=self._iron_condor_legs()):
            _execute_multi_leg_exit(client, trade, reason="TEST")
        # Each non-null perm_id should produce a cancel attempt
        expected_perm_ids = sorted({1001, 1002, 1003, 1004})
        actual = sorted([c.args[0] for c in client.cancel_order_by_perm_id.call_args_list])
        assert actual == expected_perm_ids

    def test_no_open_legs_is_noop(self):
        from strategy.exit_executor import _execute_multi_leg_exit
        client = MagicMock()
        trade = {"ticker": "SPY", "db_id": 42, "n_legs": 4}
        with patch("strategy.exit_executor._fetch_open_legs", return_value=[]):
            _execute_multi_leg_exit(client, trade, reason="TEST")
        client.buy_call.assert_not_called()
        client.sell_put.assert_not_called()

    def test_stock_leg_is_skipped_with_warn(self):
        from strategy.exit_executor import _execute_multi_leg_exit
        legs = [
            {"leg_id": 1, "leg_index": 0, "leg_role": "short_call",
             "sec_type": "OPT", "symbol": "SPY260515C00450000", "right": "C",
             "direction": "SHORT", "contracts_open": 1, "entry_price": 2,
             "ib_con_id": 1, "ib_tp_perm_id": None, "ib_sl_perm_id": None},
            {"leg_id": 5, "leg_index": 1, "leg_role": "hedge_stock",
             "sec_type": "STK", "symbol": "SPY", "right": None,
             "direction": "LONG", "contracts_open": 100, "entry_price": 447,
             "ib_con_id": 2, "ib_tp_perm_id": None, "ib_sl_perm_id": None},
        ]
        client = MagicMock()
        trade = {"ticker": "SPY", "db_id": 99, "n_legs": 2}
        with patch("strategy.exit_executor._fetch_open_legs", return_value=legs):
            _execute_multi_leg_exit(client, trade, reason="TEST")
        # Option leg closed; stock leg skipped (not yet supported)
        client.buy_call.assert_called_once()
        # No generic "close stock" method called
        assert not any(
            name in ("sell_stock", "close_stock")
            for name in [c[0] for c in client.mock_calls]
        )

    def test_missing_db_id_aborts(self):
        from strategy.exit_executor import _execute_multi_leg_exit
        client = MagicMock()
        result = _execute_multi_leg_exit(client, {"ticker": "SPY"}, reason="X")
        assert result is None
        client.buy_call.assert_not_called()
