"""Unit tests for the compensating-transaction (bracket restoration)
path in strategy.reconciliation.

See docs/bracket_rollback_semantics.md — when a close flow cancels
a bracket but the close SELL doesn't complete, reconcile PASS 4
must place fresh protection (SELL LMT + SELL STP) on the existing
position. These tests lock in that contract with a mocked client
+ mocked SQL session.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _session_row(contracts=2, entry_price=1.50, profit_target=3.00, stop_loss_level=0.60,
                  client_trade_id=None):
    """Build a mock session whose execute().fetchone() returns a row."""
    session = MagicMock()
    first = MagicMock()
    first.fetchone.return_value = (contracts, entry_price, profit_target, stop_loss_level,
                                    client_trade_id)
    session.execute.side_effect = [first, MagicMock()]  # first = SELECT, second = UPDATE
    return session


class TestRestoreBracketsFor:
    def test_happy_path_calls_place_protection_brackets_with_trade_values(self):
        """Uses the trade row's profit_target + stop_loss_level as
        the new TP + SL prices, not global defaults."""
        from strategy.reconciliation import _restore_brackets_for

        session = _session_row(
            contracts=2, entry_price=1.50, profit_target=3.00, stop_loss_level=0.60,
        )
        client = MagicMock()
        client.place_protection_brackets.return_value = {
            "tp_order_id": 5001, "tp_perm_id": 999001,
            "sl_order_id": 5002, "sl_perm_id": 999002,
            "tp_status": "Submitted", "sl_status": "PreSubmitted",
            "oca_group": "RESTORE-1",
        }

        _restore_brackets_for(session, client, trade_id=42,
                               ticker="AAPL", symbol="AAPL260420C00272500")

        # Called with trade's recorded TP / SL prices
        client.place_protection_brackets.assert_called_once_with(
            "AAPL260420C00272500", 2, 3.00, 0.60, order_ref=None
        )

    def test_skips_when_contracts_zero(self):
        """If contracts_open is 0 (position closed), do nothing."""
        from strategy.reconciliation import _restore_brackets_for

        session = _session_row(contracts=0)
        client = MagicMock()

        _restore_brackets_for(session, client, trade_id=42,
                               ticker="AAPL", symbol="AAPL...C")

        assert not client.place_protection_brackets.called

    def test_fallback_to_config_defaults_when_levels_null(self):
        """If profit_target / stop_loss_level columns are NULL, fall
        back to config.PROFIT_TARGET and config.STOP_LOSS."""
        from strategy.reconciliation import _restore_brackets_for
        import config

        session = _session_row(
            contracts=2, entry_price=1.00, profit_target=None, stop_loss_level=None,
        )
        client = MagicMock()
        client.place_protection_brackets.return_value = {
            "tp_order_id": 1, "tp_perm_id": 1, "tp_status": "Submitted",
            "sl_order_id": 2, "sl_perm_id": 2, "sl_status": "PreSubmitted",
            "oca_group": "x",
        }

        _restore_brackets_for(session, client, trade_id=42,
                               ticker="X", symbol="X")
        args, _ = client.place_protection_brackets.call_args
        symbol, contracts, tp_price, sl_price = args
        assert tp_price == round(1.00 * (1 + config.PROFIT_TARGET), 2)
        assert sl_price == round(1.00 * (1 - config.STOP_LOSS), 2)

    def test_writes_new_permids_back_to_db(self):
        from strategy.reconciliation import _restore_brackets_for

        session = _session_row()
        client = MagicMock()
        client.place_protection_brackets.return_value = {
            "tp_order_id": 5001, "tp_perm_id": 999001,
            "sl_order_id": 5002, "sl_perm_id": 999002,
            "tp_status": "Submitted", "sl_status": "PreSubmitted",
        }

        _restore_brackets_for(session, client, trade_id=42, ticker="X", symbol="X")

        # Second execute call is the UPDATE — grab its params
        update_call = session.execute.call_args_list[1]
        params = update_call.args[1]
        assert params["tpp"] == 999001
        assert params["slp"] == 999002
        assert params["id"] == 42

    def test_raises_on_place_failure_so_caller_can_catch(self):
        """If place_protection_brackets fails, raise so the outer
        reconcile loop logs `bracket_restore_failed` audit."""
        from strategy.reconciliation import _restore_brackets_for

        session = _session_row()
        client = MagicMock()
        client.place_protection_brackets.side_effect = RuntimeError("IB rejected")

        import pytest
        with pytest.raises(RuntimeError, match="IB rejected"):
            _restore_brackets_for(session, client, trade_id=42,
                                   ticker="X", symbol="X")
