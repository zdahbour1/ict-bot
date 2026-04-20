"""Regression tests for the MU incident 2026-04-20 (second half).

After the strict cancel-verification fix (commit 363380f), MU stopped
silently shorting on rolls — BUT rolls still couldn't complete
because the cancels never reached terminal state. IB was returning
Error 10147 ('OrderId X that needs to be cancelled is not found')
on every attempt, despite openTrades() clearly showing the orders
as Submitted.

Root cause: IB's ``cancelOrder`` only succeeds on the client that
PLACED the order. The bracket was placed by the scanner's client
(clientId=N+1); the close runs on the exit manager's client
(clientId=N). Same IB account, different API clients — cancel
cross-client returns 10147.

Fix: pass a pool reference to IBClient, fan out cancel_order_by_id
to every connection. Owning client processes the cancel; others
emit 10147 which we swallow.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestCrossClientCancelFanOut:
    """cancel_order_by_id must fan out across the pool when pool=set."""

    def test_cancel_fans_out_to_other_connections(self):
        """With pool=set, cancel is submitted on OWN connection + every
        other connection. That way the owning client picks it up
        regardless of which client placed the order."""
        from broker.ib_client import IBClient

        # Build a mock pool with 3 connections — one exit (our own) and
        # two scanners. The client wraps the exit connection.
        own_conn = MagicMock()
        own_conn.label = "exit-mgr"
        scanner_a = MagicMock()
        scanner_a.label = "scanner-A"
        scanner_b = MagicMock()
        scanner_b.label = "scanner-B"

        pool = MagicMock()
        pool.all_connections = [own_conn, scanner_a, scanner_b]

        # Build the client. We bypass __init__ since it expects a real
        # IBConnection and we want to focus only on cancel routing.
        client = IBClient.__new__(IBClient)
        client._conn = own_conn
        client._pool = pool
        client._pool_mode = True
        client.ib = MagicMock()

        # Stub: submit on own connection is done via _submit_to_ib
        client._submit_to_ib = MagicMock()

        client.cancel_order_by_id(4773)

        # Own connection received the cancel via _submit_to_ib
        assert client._submit_to_ib.called, "own-connection cancel must fire"

        # And the fan-out submitted to the OTHER two connections
        fanout_targets = {
            call.args[0] for call in scanner_a.submit.call_args_list
        } | {
            call.args[0] for call in scanner_b.submit.call_args_list
        }
        # Both scanner connections got a submit call
        assert scanner_a.submit.called, "scanner-A must receive the fan-out cancel"
        assert scanner_b.submit.called, "scanner-B must receive the fan-out cancel"

    def test_cancel_without_pool_only_uses_own_connection(self):
        """Legacy mode (no pool) must keep the original behavior."""
        from broker.ib_client import IBClient
        client = IBClient.__new__(IBClient)
        client._conn = MagicMock()
        client._pool = None                  # legacy / no pool
        client._pool_mode = False
        client.ib = MagicMock()
        client._submit_to_ib = MagicMock()

        client.cancel_order_by_id(4773)
        # Still calls own _submit_to_ib
        assert client._submit_to_ib.called

    def test_fanout_failure_on_one_connection_does_not_break_others(self):
        """If one scanner connection errors during submit, the other
        still gets called. A 10147 on one client must not short-circuit
        the others."""
        from broker.ib_client import IBClient

        own_conn = MagicMock()
        scanner_a = MagicMock()
        scanner_a.submit.side_effect = RuntimeError("10147")
        scanner_b = MagicMock()

        pool = MagicMock()
        pool.all_connections = [own_conn, scanner_a, scanner_b]

        client = IBClient.__new__(IBClient)
        client._conn = own_conn
        client._pool = pool
        client._pool_mode = True
        client.ib = MagicMock()
        client._submit_to_ib = MagicMock()

        # Must NOT raise
        client.cancel_order_by_id(4773)

        # Both scanners were attempted, despite A raising
        assert scanner_a.submit.called
        assert scanner_b.submit.called


class TestCancelHelperRunsOnGivenIB:
    """_ib_cancel_single_order_on_conn is a @staticmethod that takes
    any ib instance — the fan-out relies on this to route cancels to
    the right connection's IB thread."""

    def test_helper_uses_the_ib_passed_in(self):
        """Calling the helper with conn_A's ib must cancel on conn_A,
        not on some other client."""
        from broker.ib_client import IBClient

        ib_a = MagicMock()
        ib_a.openTrades.return_value = [
            MagicMock(order=MagicMock(orderId=4773, clientId=2)),
        ]
        ib_b = MagicMock()
        ib_b.openTrades.return_value = []

        IBClient._ib_cancel_single_order_on_conn(ib_a, 4773)

        assert ib_a.cancelOrder.called
        assert not ib_b.cancelOrder.called
