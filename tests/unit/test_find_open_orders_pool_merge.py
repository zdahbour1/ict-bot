"""Regression test for SPY 2026-04-21 stale-cache bug.

Bug: exit-mgr client cancels an order placed by scanner-A client. The
cancel lands on scanner-A's wrapper and status updates there. exit-mgr's
wrapper still has "Submitted" in its cache. reqAllOpenOrders() doesn't
evict the stale entry because IB only returns currently-OPEN orders.

Fix: find_open_orders_for_contract fans out across every pool connection,
dedupes by permId, and picks the MOST-TERMINAL status. Even if exit-mgr
has Submitted (stale), scanner-A's Cancelled wins and the order is
correctly reported as gone.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestPoolMergeStaleCache:
    def test_terminal_from_other_connection_wins_over_stale_submitted(self):
        """exit-mgr cache says Submitted, scanner-A cache says Cancelled
        (fresh truth). Merged result must treat the order as terminal
        and return it as NOT alive."""
        from broker.ib_client import IBClient

        client = IBClient.__new__(IBClient)
        client.ib = MagicMock()
        own_conn = MagicMock()
        other_conn = MagicMock()
        client._conn = own_conn

        # own (exit-mgr) view — STALE
        stale = [{
            "orderId": 3576, "permId": 1335331456, "action": "SELL",
            "orderType": "LMT", "totalQty": 2.0,
            "status": "Submitted",   # stale!
            "conId": 871676716, "parentId": 3575, "lmtPrice": 2.12,
            "auxPrice": 0.0, "orderRef": "SPY-260421-01", "clientId": 3,
        }]
        # fan-out (scanner-A) view — FRESH
        fresh = [{
            "orderId": 3576, "permId": 1335331456, "action": "SELL",
            "orderType": "LMT", "totalQty": 2.0,
            "status": "Cancelled",   # truth
            "conId": 871676716, "parentId": 3575, "lmtPrice": 2.12,
            "auxPrice": 0.0, "orderRef": "SPY-260421-01", "clientId": 3,
        }]

        client._submit_to_ib = MagicMock(return_value=stale)
        pool = MagicMock()
        pool.all_connections = [own_conn, other_conn]
        other_conn.submit = MagicMock(return_value=fresh)
        client._pool = pool

        result = client.find_open_orders_for_contract(871676716, "SPY260421P00710000")

        # Cancelled won the merge — the order is NOT returned as alive.
        assert result == []

    def test_live_order_from_any_connection_shows_up(self):
        """If one connection still shows Submitted and no connection shows
        terminal, the order should be returned as alive."""
        from broker.ib_client import IBClient

        client = IBClient.__new__(IBClient)
        client.ib = MagicMock()
        client._conn = MagicMock()
        client._submit_to_ib = MagicMock(return_value=[{
            "orderId": 3576, "permId": 1335331456, "action": "SELL",
            "orderType": "LMT", "totalQty": 2.0, "status": "Submitted",
            "conId": 871676716, "parentId": 3575, "lmtPrice": 2.12,
            "auxPrice": 0.0, "orderRef": "x", "clientId": 3,
        }])
        pool = MagicMock()
        pool.all_connections = [client._conn]
        client._pool = pool

        result = client.find_open_orders_for_contract(871676716, "X")
        assert len(result) == 1
        assert result[0]["status"] == "Submitted"

    def test_no_pool_falls_back_to_single_connection_filter(self):
        """Without a pool, the method must still filter terminal orders."""
        from broker.ib_client import IBClient

        client = IBClient.__new__(IBClient)
        client.ib = MagicMock()
        client._conn = MagicMock()
        client._pool = None
        client._submit_to_ib = MagicMock(return_value=[
            {"orderId": 1, "permId": 100, "status": "Submitted",
             "conId": 1, "clientId": 1},
            {"orderId": 2, "permId": 101, "status": "Cancelled",
             "conId": 1, "clientId": 1},
        ])
        result = client.find_open_orders_for_contract(1, "X")
        # Cancelled filtered out, Submitted returned
        ids = sorted(r["orderId"] for r in result)
        assert ids == [1]

    def test_dedup_by_perm_id_across_connections(self):
        """Same permId appearing in multiple connections is deduped."""
        from broker.ib_client import IBClient

        client = IBClient.__new__(IBClient)
        client.ib = MagicMock()
        client._conn = MagicMock()
        entry = {
            "orderId": 3576, "permId": 999, "status": "PreSubmitted",
            "conId": 1, "clientId": 3, "action": "SELL", "orderType": "LMT",
            "totalQty": 2.0, "parentId": 0, "lmtPrice": 1.0, "auxPrice": 0.0,
            "orderRef": "x",
        }
        client._submit_to_ib = MagicMock(return_value=[entry])
        other = MagicMock()
        other.submit = MagicMock(return_value=[entry])  # same permId
        pool = MagicMock()
        pool.all_connections = [client._conn, other]
        client._pool = pool

        result = client.find_open_orders_for_contract(1, "X")
        # Not duplicated
        assert len(result) == 1
