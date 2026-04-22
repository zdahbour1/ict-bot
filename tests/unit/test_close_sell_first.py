"""Tests for the sell-first close flow (ENH/ARCH: IB cross-client asymmetry).

Sell-first semantics (strategy/exit_executor.py::_execute_exit_sell_first):
  1. qty==0 → update DB only, set _bracket_fired if brackets were expected
  2. qty<0 → critical alert, abort (bot doesn't support short options)
  3. Otherwise: best-effort cancel (fire-and-forget), MKT SELL, verify
     brackets terminal, verify final qty not negative

These tests lock in the contract by mocking client + config.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _trade(**overrides):
    t = {
        "ticker": "AAPL",
        "symbol": "AAPL260430C00180000",
        "ib_con_id": 123456,
        "direction": "LONG",
        "contracts": 2,
        "db_id": 42,
        "ib_tp_order_id": 9001,
        "ib_sl_order_id": 9002,
    }
    t.update(overrides)
    return t


class TestSellFirstMode:
    def test_qty_zero_skips_sell_and_flags_bracket_fired(self):
        """Position already flat → no SELL, flag bracket_fired for DB update."""
        from strategy.exit_executor import execute_exit
        client = MagicMock()
        client.get_position_quantity.return_value = 0

        trade = _trade()
        with patch("config.CLOSE_MODE_SELL_FIRST", True):
            execute_exit(client, trade, reason="TEST")

        client.sell_call.assert_not_called()
        client.sell_put.assert_not_called()
        assert trade.get("_bracket_fired") is True
        assert trade["ib_tp_order_id"] is None
        assert trade["ib_sl_order_id"] is None

    def test_negative_qty_aborts_with_critical(self):
        """Negative qty on IB → refuse SELL, fire critical handler, return None."""
        from strategy.exit_executor import execute_exit
        client = MagicMock()
        client.get_position_quantity.return_value = -2

        with patch("config.CLOSE_MODE_SELL_FIRST", True), \
             patch("strategy.error_handler.handle_error") as mock_err:
            result = execute_exit(client, _trade(direction="SHORT"), reason="TEST")

        assert result is None
        client.sell_call.assert_not_called()
        client.sell_put.assert_not_called()
        assert mock_err.called
        # Critical flag must be set so dashboard shows red
        _, kwargs = mock_err.call_args
        assert kwargs.get("critical") is True

    def test_happy_path_long_fires_sell_call(self):
        """LONG + positive qty → sell_call with min(requested, ib_qty)."""
        from strategy.exit_executor import execute_exit
        client = MagicMock()
        client.get_position_quantity.return_value = 2
        # Post-cancel query: brackets terminal immediately
        client.find_open_orders_for_contract.return_value = []

        trade = _trade(direction="LONG", contracts=2)
        with patch("config.CLOSE_MODE_SELL_FIRST", True):
            execute_exit(client, trade, reason="TEST")

        client.sell_call.assert_called_once_with("AAPL260430C00180000", 2)
        client.sell_put.assert_not_called()

    def test_happy_path_short_fires_sell_put(self):
        """ICT 'SHORT' (long puts) + positive qty → sell_put."""
        from strategy.exit_executor import execute_exit
        client = MagicMock()
        client.get_position_quantity.return_value = 2
        client.find_open_orders_for_contract.return_value = []

        trade = _trade(direction="SHORT", symbol="AAPL260430P00180000", contracts=2)
        with patch("config.CLOSE_MODE_SELL_FIRST", True):
            execute_exit(client, trade, reason="TEST")

        client.sell_put.assert_called_once_with("AAPL260430P00180000", 2)
        client.sell_call.assert_not_called()

    def test_sell_never_blocked_by_stale_bracket(self):
        """Even if pre-SELL cancel raises (e.g. cross-client 10147), SELL
        must still fire — that's the whole point of sell-first."""
        from strategy.exit_executor import execute_exit
        client = MagicMock()
        client.get_position_quantity.return_value = 2
        # best-effort cancel sees an order owned by another client — raises 10147
        client.find_open_orders_for_contract.return_value = [
            {"orderId": 9001, "orderType": "LMT", "status": "PreSubmitted"},
        ]
        client.cancel_order_by_id.side_effect = RuntimeError("10147 not owner")

        trade = _trade()
        with patch("config.CLOSE_MODE_SELL_FIRST", True):
            execute_exit(client, trade, reason="TEST")

        # SELL fired despite cancel failure
        client.sell_call.assert_called_once()


class TestVerifyBracketsClearedPostSell:
    def test_returns_true_when_brackets_terminal_quickly(self):
        from strategy.exit_executor import verify_brackets_cleared_post_sell
        client = MagicMock()
        client.find_open_orders_for_contract.return_value = []
        trade = _trade()
        ok = verify_brackets_cleared_post_sell(client, trade, timeout=1.0)
        assert ok is True

    def test_issues_explicit_cancel_when_still_alive(self):
        """If brackets still live past timeout, must try explicit cancel."""
        from strategy.exit_executor import verify_brackets_cleared_post_sell
        client = MagicMock()
        # Persistent straggler
        client.find_open_orders_for_contract.return_value = [
            {"orderId": 9001, "status": "PreSubmitted", "orderType": "LMT"},
        ]
        trade = _trade()
        ok = verify_brackets_cleared_post_sell(client, trade, timeout=0.6)
        # Explicit cancel attempted for the straggler
        # Phase 5: cancel_order_by_id now takes preferred_client_id; when
        # the trade has no ib_client_id stamped, we pass None.
        client.cancel_order_by_id.assert_called_with(9001, preferred_client_id=None)
        assert ok is False

    def test_fires_handle_error_when_bracket_survives_explicit_cancel(self):
        """Bracket alive after our explicit cancel → alert (non-critical)."""
        from strategy.exit_executor import verify_brackets_cleared_post_sell
        client = MagicMock()
        client.find_open_orders_for_contract.return_value = [
            {"orderId": 9001, "status": "PreSubmitted", "orderType": "LMT"},
        ]
        trade = _trade()
        with patch("strategy.error_handler.handle_error") as mock_err:
            ok = verify_brackets_cleared_post_sell(client, trade, timeout=0.6)
        assert ok is False
        assert mock_err.called


class TestBestEffortCancel:
    def test_swallows_cancel_errors(self):
        """Must not raise — SELL must proceed regardless."""
        from strategy.exit_executor import best_effort_cancel_brackets
        client = MagicMock()
        client.find_open_orders_for_contract.return_value = [
            {"orderId": 1, "orderType": "LMT", "status": "PreSubmitted"},
        ]
        client.cancel_order_by_id.side_effect = RuntimeError("10147")
        # Should not raise
        best_effort_cancel_brackets(client, _trade())

    def test_noop_when_no_open_orders(self):
        from strategy.exit_executor import best_effort_cancel_brackets
        client = MagicMock()
        client.find_open_orders_for_contract.return_value = []
        best_effort_cancel_brackets(client, _trade())
        client.cancel_order_by_id.assert_not_called()


class TestLegacyModeStillReachable:
    def test_cancel_first_mode_still_works(self):
        """With CLOSE_MODE_SELL_FIRST=False, must take legacy path
        (cancel_all_orders_and_verify). Here we just verify the legacy
        function is invoked — its own semantics have their own tests."""
        from strategy import exit_executor
        client = MagicMock()
        client.get_position_quantity.return_value = 0

        with patch("config.CLOSE_MODE_SELL_FIRST", False), \
             patch.object(exit_executor, "cancel_all_orders_and_verify",
                          return_value=True) as mock_cancel:
            exit_executor.execute_exit(client, _trade(), reason="TEST")

        mock_cancel.assert_called_once()
