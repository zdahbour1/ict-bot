"""Regression tests for the MSFT incident 2026-04-20.

Bug: STEP 3 treated ``PendingCancel`` as a terminal "cancelled" state
and let STEP 5 proceed to send the close SELL. But PendingCancel means
the cancel is still pending on IB's side — the order can still fill.
In the MSFT case IB reverted the cancel 1.3s later, the bracket LMT
SELL later filled, and the user went net-short.

See docs/bracket_cancel_strict_verification.md.

These tests cover both the new strict-verification logic in
exit_executor.py and the new negative-position recovery in
exit_manager._verify_close_on_ib.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ── Strict cancel verification ───────────────────────────────────

class TestPendingCancelIsNotTerminal:
    """The MSFT bug: PendingCancel was treated as safe. It isn't."""

    def _run_verify(self, status_sequence):
        """Helper — cancel_all_orders_and_verify with a mock whose
        find_open_orders_for_contract returns statuses in the given
        sequence across successive calls."""
        from strategy.exit_executor import cancel_all_orders_and_verify

        client = MagicMock()
        client.refresh_all_open_orders.return_value = 1

        call_count = [0]
        def _poll(*a, **kw):
            idx = min(call_count[0], len(status_sequence) - 1)
            call_count[0] += 1
            return status_sequence[idx]
        client.find_open_orders_for_contract.side_effect = _poll

        trade = {"ticker": "MSFT", "ib_con_id": 874403104,
                 "symbol": "MSFT260420C00417500"}
        with patch("strategy.exit_executor.time.sleep"):
            return cancel_all_orders_and_verify(client, trade), client

    def test_pending_cancel_does_NOT_pass_verification(self):
        """PendingCancel for every poll → verify must abort False.

        This is the MSFT bug reproducer: under the old code the poll
        filter only rejected {Submitted, PreSubmitted, PendingSubmit},
        so PendingCancel passed and the code returned True. Now it
        must return False (or keep polling until terminal)."""
        # Every poll: both orders stuck in PendingCancel
        pending = [
            {"orderId": 4383, "orderType": "LMT", "status": "PendingCancel", "action": "SELL"},
            {"orderId": 4384, "orderType": "STP", "status": "PendingCancel", "action": "SELL"},
        ]
        sequence = [pending] * 25  # plenty of polls, never resolves
        result, client = self._run_verify(sequence)
        assert result is False, (
            "PendingCancel was treated as terminal — MSFT bug regression"
        )

    def test_cancel_revert_triggers_retry(self):
        """PendingCancel → Submitted (IB reverted) must trigger another
        cancel call, not silently pass."""
        initial = [
            {"orderId": 4383, "orderType": "LMT", "status": "Submitted", "action": "SELL"},
        ]
        # Round 1 polls: goes to PendingCancel
        pc = [{"orderId": 4383, "orderType": "LMT", "status": "PendingCancel", "action": "SELL"}]
        # Then flips back to Submitted (revert scenario)
        reverted = [{"orderId": 4383, "orderType": "LMT", "status": "Submitted", "action": "SELL"}]
        # Round 2 polls: after retry cancel, this time reaches Cancelled
        cancelled = [{"orderId": 4383, "orderType": "LMT", "status": "Cancelled", "action": "SELL"}]
        sequence = [initial, pc, pc, pc, pc, pc, reverted, cancelled]
        result, client = self._run_verify(sequence)
        assert result is True
        # At least 2 cancels sent (initial + retry after revert)
        cancel_calls = client.cancel_order_by_id.call_args_list
        assert len(cancel_calls) >= 2, (
            f"Revert should trigger retry; got {len(cancel_calls)} cancels"
        )

    def test_actually_cancelled_passes(self):
        """All orders reach status=Cancelled → return True."""
        initial = [
            {"orderId": 4383, "orderType": "LMT", "status": "Submitted", "action": "SELL"},
            {"orderId": 4384, "orderType": "STP", "status": "PreSubmitted", "action": "SELL"},
        ]
        cancelled = [
            {"orderId": 4383, "orderType": "LMT", "status": "Cancelled", "action": "SELL"},
            {"orderId": 4384, "orderType": "STP", "status": "Cancelled", "action": "SELL"},
        ]
        sequence = [initial, cancelled]
        result, _ = self._run_verify(sequence)
        assert result is True

    def test_orders_disappearing_from_openTrades_counts_as_terminal(self):
        """If an orderId drops out of openTrades entirely, it's gone —
        count it as terminal. (IB sometimes purges fully-cancelled
        orders from the open list.)"""
        initial = [
            {"orderId": 4383, "orderType": "LMT", "status": "Submitted", "action": "SELL"},
        ]
        gone: list = []  # empty list = order no longer in openTrades
        sequence = [initial, gone]
        result, _ = self._run_verify(sequence)
        assert result is True

    def test_filled_counts_as_terminal(self):
        """If a bracket fills during our cancel attempt, it's terminal
        (though not the kind we wanted). Don't hang — return True and
        let the position check in STEP 4 detect the resulting qty."""
        initial = [
            {"orderId": 4383, "orderType": "LMT", "status": "Submitted", "action": "SELL"},
        ]
        filled = [
            {"orderId": 4383, "orderType": "LMT", "status": "Filled", "action": "SELL"},
        ]
        sequence = [initial, filled]
        result, _ = self._run_verify(sequence)
        assert result is True


# ── Negative position recovery in verify_close ───────────────────

class TestNegativePositionRecovery:
    def _mgr(self, client):
        from strategy.exit_manager import ExitManager
        mgr = ExitManager.__new__(ExitManager)
        mgr.client = client
        return mgr

    def test_negative_post_sweep_triggers_recovery_buy(self):
        """After position reaches 0 and we sweep stragglers, a race
        can fill a bracket and flip position to -N. Detect it and
        issue a BUY to restore flat. The MSFT-incident safety net."""
        client = MagicMock()
        # Position progression: reaches 0 during the poll, becomes -2
        # after the straggler sweep.
        qty_sequence = [0, -2]   # first call → 0, second call → -2
        client.get_position_quantity.side_effect = qty_sequence
        # The sweep finds a straggler
        client.refresh_all_open_orders.return_value = 0
        client.find_open_orders_for_contract.return_value = [
            {"orderId": 4383, "status": "PreSubmitted", "orderType": "LMT"},
        ]
        # Recovery BUY path
        client.buy_call = MagicMock(return_value={"status": "Filled"})

        trade = {"ticker": "MSFT", "db_id": 1023,
                 "ib_con_id": 874403104,
                 "symbol": "MSFT260420C00417500",
                 "direction": "LONG"}
        mgr = self._mgr(client)
        with patch("strategy.exit_manager.time.sleep"):
            result = mgr._verify_close_on_ib(trade)

        # Must NOT finalize close — return False so caller retries
        assert result is False
        # Recovery BUY must have fired
        assert client.buy_call.called, (
            "Negative position after sweep must trigger recovery BUY"
        )
        # Count was 2 (the size of the short)
        args, _ = client.buy_call.call_args
        assert args[0] == "MSFT260420C00417500"
        assert args[1] == 2

    def test_negative_during_poll_triggers_recovery_buy(self):
        """Direct negative qty during the initial poll (bracket fired
        before our SELL even went out) also recovers."""
        client = MagicMock()
        client.get_position_quantity.return_value = -2
        client.buy_call = MagicMock(return_value={"status": "Filled"})
        client.refresh_all_open_orders.return_value = 0
        client.find_open_orders_for_contract.return_value = []

        trade = {"ticker": "MSFT", "db_id": 999,
                 "ib_con_id": 999,
                 "symbol": "MSFT260420C00417500",
                 "direction": "LONG"}
        mgr = self._mgr(client)
        with patch("strategy.exit_manager.time.sleep"):
            result = mgr._verify_close_on_ib(trade)
        assert result is False
        assert client.buy_call.called

    def test_put_direction_recovery_uses_buy_put(self):
        """For a bearish (long-puts) trade, recovery from a short put
        requires BUY_PUT, not BUY_CALL."""
        client = MagicMock()
        client.get_position_quantity.return_value = -2
        client.buy_put = MagicMock(return_value={"status": "Filled"})
        client.buy_call = MagicMock()
        client.refresh_all_open_orders.return_value = 0
        client.find_open_orders_for_contract.return_value = []

        trade = {"ticker": "TSLA", "db_id": 2,
                 "ib_con_id": 111,
                 "symbol": "TSLA260425P00400000",
                 "direction": "SHORT"}  # ICT SHORT = long puts
        mgr = self._mgr(client)
        with patch("strategy.exit_manager.time.sleep"):
            result = mgr._verify_close_on_ib(trade)
        assert result is False
        assert client.buy_put.called
        assert not client.buy_call.called

    def test_recovery_buy_failure_is_logged_not_raised(self):
        """If the recovery BUY itself fails, we must NOT raise — just
        audit-log loudly. The trade stays in abnormal state; reconcile
        will fight that later."""
        client = MagicMock()
        client.get_position_quantity.return_value = -2
        client.buy_call = MagicMock(side_effect=RuntimeError("IB conn lost"))
        client.refresh_all_open_orders.return_value = 0
        client.find_open_orders_for_contract.return_value = []

        trade = {"ticker": "MSFT", "db_id": 1,
                 "ib_con_id": 1, "symbol": "MSFTx",
                 "direction": "LONG"}
        mgr = self._mgr(client)
        with patch("strategy.exit_manager.time.sleep"):
            # Must not raise
            result = mgr._verify_close_on_ib(trade)
        assert result is False
