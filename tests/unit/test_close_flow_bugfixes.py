"""Regression tests for the three roll/close bugs caught on 2026-04-20.

See ``docs/roll_close_bug_fixes.md`` for the incident writeup.

All three fixes are exercised with a mocked BrokerClient — no live IB
required.  The tests are deliberately narrow:

  Fix A: refresh_all_open_orders() is called before the cancel poll.
  Fix B: direction=SHORT with ib_qty>0 no longer trips the mismatch abort.
  Fix C: ExitManager._verify_close_on_ib() polls for position=0 and
         returns False when the position stays non-zero.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ── Fix A: cross-client bracket visibility ───────────────────────

class TestFixA_RefreshAllOpenOrders:
    def test_cancel_flow_refreshes_before_query(self):
        """cancel_all_orders_and_verify must call refresh_all_open_orders
        BEFORE find_open_orders_for_contract so cross-client brackets
        show up."""
        from strategy.exit_executor import cancel_all_orders_and_verify

        client = MagicMock()
        client.refresh_all_open_orders.return_value = 5
        # Bracket from a different clientId now visible after refresh
        client.find_open_orders_for_contract.return_value = [
            {"orderId": 3094, "orderType": "LMT", "status": "PreSubmitted",
             "action": "SELL"},
            {"orderId": 3095, "orderType": "STP", "status": "PreSubmitted",
             "action": "SELL"},
        ]
        # After cancel, subsequent polls see nothing active
        def _poll_side_effect(*args, **kwargs):
            # First call: returns the stragglers. Subsequent calls: empty.
            if client.find_open_orders_for_contract.call_count == 1:
                return [
                    {"orderId": 3094, "status": "PreSubmitted", "orderType": "LMT"},
                    {"orderId": 3095, "status": "PreSubmitted", "orderType": "STP"},
                ]
            return []
        client.find_open_orders_for_contract.side_effect = _poll_side_effect

        trade = {"ticker": "IWM", "ib_con_id": 871520135,
                 "symbol": "IWM260420C00275000"}
        with patch("strategy.exit_executor.time.sleep"):
            result = cancel_all_orders_and_verify(client, trade)

        assert result is True
        # Critical assertion: refresh was called
        assert client.refresh_all_open_orders.called
        # And both brackets were sent for cancellation
        cancelled_ids = {call.args[0] for call in client.cancel_order_by_id.call_args_list}
        assert 3094 in cancelled_ids
        assert 3095 in cancelled_ids

    def test_refresh_helper_exists_on_ib_client(self):
        """The ib_client public surface must expose refresh_all_open_orders."""
        from broker.ib_client import IBClient  # type: ignore[attr-defined]
        assert hasattr(IBClient, "refresh_all_open_orders")


# ── Fix B: direction-mismatch check inverted for SHORT ───────────

class TestFixB_DirectionCheck:
    def _setup(self, direction: str, ib_qty: int):
        """Returns a client mock wired up so execute_exit reaches Step 4
        with the given IB qty, then we can see whether it proceeds to
        Step 5 (SELL) or aborts."""
        client = MagicMock()
        client.refresh_all_open_orders.return_value = 0
        client.find_open_orders_for_contract.return_value = []  # no stragglers
        client.get_position_quantity.return_value = ib_qty
        # If we reach Step 5, these are the calls we'd see
        client.sell_call = MagicMock()
        client.sell_put = MagicMock()
        trade = {
            "ticker": "TSLA", "ib_con_id": 999, "symbol": "TSLAx",
            "direction": direction, "contracts": 2,
        }
        return client, trade

    def test_short_direction_with_positive_qty_proceeds(self):
        """ICT convention: direction=SHORT means long puts (ib_qty > 0).
        This is the CORRECT state — must NOT abort with 'direction mismatch'."""
        from strategy.exit_executor import execute_exit
        client, trade = self._setup(direction="SHORT", ib_qty=2)
        with patch("strategy.exit_executor.time.sleep"):
            execute_exit(client, trade, "ROLL at +23%")
        # Step 5 must have fired a sell_put (close by selling our long puts)
        assert client.sell_put.called, "SHORT with qty=2 must proceed to SELL"
        assert not client.sell_call.called

    def test_long_direction_with_positive_qty_proceeds(self):
        """LONG calls at qty>0 is the normal case."""
        from strategy.exit_executor import execute_exit
        client, trade = self._setup(direction="LONG", ib_qty=2)
        with patch("strategy.exit_executor.time.sleep"):
            execute_exit(client, trade, "ROLL at +23%")
        assert client.sell_call.called
        assert not client.sell_put.called

    def test_negative_qty_aborts(self):
        """Any active trade should have ib_qty > 0. Negative qty means
        we're already net-short (prior bug effect, or a naked-short
        strategy we don't support yet) — SELL would widen the short."""
        from strategy.exit_executor import execute_exit
        client, trade = self._setup(direction="LONG", ib_qty=-2)
        with patch("strategy.exit_executor.time.sleep"):
            execute_exit(client, trade, "ROLL at +23%")
        # Must NOT fire a sell
        assert not client.sell_call.called
        assert not client.sell_put.called

    def test_short_with_negative_qty_also_aborts(self):
        """direction=SHORT + ib_qty<0 is the same situation — we're short,
        SELL would widen. Must abort."""
        from strategy.exit_executor import execute_exit
        client, trade = self._setup(direction="SHORT", ib_qty=-2)
        with patch("strategy.exit_executor.time.sleep"):
            execute_exit(client, trade, "ROLL at +23%")
        assert not client.sell_call.called
        assert not client.sell_put.called


# ── Fix C: _verify_close_on_ib ───────────────────────────────────

class TestFixC_VerifyClose:
    def _make_manager(self, client):
        """Build an ExitManager without triggering its __init__ side
        effects (DB connection, etc). We only need the verify helpers."""
        from strategy.exit_manager import ExitManager
        mgr = ExitManager.__new__(ExitManager)
        mgr.client = client
        return mgr

    def test_passes_when_position_reaches_zero(self):
        client = MagicMock()
        client.get_position_quantity.return_value = 0
        client.find_open_orders_for_contract.return_value = []
        client.refresh_all_open_orders.return_value = 0
        trade = {"ticker": "IWM", "ib_con_id": 123, "symbol": "IWMx"}
        mgr = self._make_manager(client)
        with patch("strategy.exit_manager.time.sleep"):
            assert mgr._verify_close_on_ib(trade) is True

    def test_fails_when_position_stays_nonzero(self):
        """IWM-style bug reproducer: sell didn't flatten, must return False
        so caller releases lock without finalize_close."""
        client = MagicMock()
        client.get_position_quantity.return_value = 2  # never goes to 0
        client.find_open_orders_for_contract.return_value = []
        client.refresh_all_open_orders.return_value = 0
        trade = {"ticker": "IWM", "ib_con_id": 123, "symbol": "IWMx"}
        mgr = self._make_manager(client)
        with patch("strategy.exit_manager.time.sleep"):
            assert mgr._verify_close_on_ib(trade) is False

    def test_cancels_stragglers_after_flatten(self):
        """After position reaches 0, sweep any leftover working orders."""
        client = MagicMock()
        client.get_position_quantity.return_value = 0
        client.refresh_all_open_orders.return_value = 0
        client.find_open_orders_for_contract.return_value = [
            {"orderId": 3095, "status": "PreSubmitted", "orderType": "STP"},
        ]
        trade = {"ticker": "IWM", "ib_con_id": 123, "symbol": "IWM260420C00275000"}
        mgr = self._make_manager(client)
        with patch("strategy.exit_manager.time.sleep"):
            mgr._verify_close_on_ib(trade)
        # Straggler SL must have been cancelled
        assert any(call.args[0] == 3095 for call in client.cancel_order_by_id.call_args_list), \
            "straggler bracket SL must be cancelled after verified close"

    def test_no_conid_returns_true(self):
        """No conId means we can't verify — don't block the close."""
        client = MagicMock()
        trade = {"ticker": "X", "ib_con_id": None, "symbol": "Xx"}
        mgr = self._make_manager(client)
        assert mgr._verify_close_on_ib(trade) is True

    def test_bracket_fired_flag_skips_poll(self):
        """If execute_exit already saw position=0 via bracket fire,
        verification skips the 3s poll but still sweeps stragglers."""
        client = MagicMock()
        client.refresh_all_open_orders.return_value = 0
        client.find_open_orders_for_contract.return_value = []
        client.get_position_quantity = MagicMock()  # must NOT be called
        trade = {"ticker": "IWM", "ib_con_id": 123, "symbol": "IWMx",
                 "_bracket_fired": True}
        mgr = self._make_manager(client)
        assert mgr._verify_close_on_ib(trade) is True
        # Position poll was skipped
        assert not client.get_position_quantity.called
