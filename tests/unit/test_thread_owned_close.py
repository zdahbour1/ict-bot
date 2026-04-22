"""Phase 5 (multi-strategy v2): thread-owned close routing.

At entry time the placing pool slot's clientId is captured and persisted
on ``trades.ib_client_id``. On close, ``cancel_order_by_id`` prefers that
specific slot when known — falls back to the pool fan-out otherwise.

These tests cover:
  1. place_bracket returns ``client_id`` in its result dict.
  2. cancel_order_by_id routes to the owning slot when preferred_client_id
     matches a pool member (fan-out is SKIPPED).
  3. cancel_order_by_id falls back to fan-out when the preferred slot
     isn't in the pool.
  4. cancel_order_by_id with preferred_client_id=None keeps legacy
     fan-out semantics exactly.
  5. insert_trade persists ``ib_client_id`` on the Trade envelope.

See docs/multi_strategy_architecture_v2.md §7 Phase 5.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ── 1. place_bracket captures and returns client_id ──────────────
class TestPlaceBracketReturnsClientId:
    def test_place_bracket_returns_client_id(self):
        """_ib_place_bracket reads self.ib.client.clientId and puts it in
        the result dict as 'client_id' so option_selector can stamp it
        onto the trade envelope."""
        from broker.ib_client import IBClient

        # Build a minimal IBClient that routes _ib_place_bracket to a
        # mocked IB instance whose client.clientId = 7.
        client = IBClient.__new__(IBClient)
        client._pool = None
        client._conn = None
        client._pool_mode = False

        ib = MagicMock()
        ib.client.clientId = 7
        ib.client.getReqId.side_effect = [100, 101, 102]

        # Stub ib.placeOrder → return a fake Trade with a Filled status
        def _make_trade(order_id, perm_id):
            fake = MagicMock()
            fake.order.orderId = order_id
            fake.order.permId = perm_id
            fake.orderStatus.status = "Filled"
            fake.orderStatus.avgFillPrice = 1.23
            return fake

        ib.placeOrder.side_effect = [
            _make_trade(100, 9001),  # parent
            _make_trade(101, 9002),  # tp
            _make_trade(102, 9003),  # sl
        ]
        ib.sleep = MagicMock()

        client.ib = ib

        # Stub contract resolver + flex check + error read
        fake_contract = MagicMock()
        fake_contract.conId = 424242
        fake_contract.secType = "OPT"
        fake_contract.tradingClass = "SPY"
        fake_contract.symbol = "SPY"
        client._occ_to_contract = MagicMock(return_value=fake_contract)
        client._get_last_error = MagicMock(return_value=None)

        result = client._ib_place_bracket(
            "SPY250101C00100000", 1, "BUY", 2.0, 0.5, order_ref="TEST-REF"
        )

        assert isinstance(result, dict)
        assert "client_id" in result, "place_bracket result must expose client_id"
        assert result["client_id"] == 7


# ── 2. Smart routing: prefer owning slot, skip fan-out ────────────
class TestCancelPrefersOwningClient:
    def _build_client_with_pool(self, client_ids):
        """Build an IBClient wrapping a mock pool whose connections have
        the given client_ids. Returns (client, conns)."""
        from broker.ib_client import IBClient

        conns = []
        for cid in client_ids:
            c = MagicMock()
            c.client_id = cid
            c.label = f"conn-{cid}"
            conns.append(c)

        pool = MagicMock()
        pool.all_connections = conns

        client = IBClient.__new__(IBClient)
        # Treat first conn as "own"
        client._conn = conns[0]
        client._pool = pool
        client._pool_mode = True
        client.ib = MagicMock()
        client._submit_to_ib = MagicMock()
        return client, conns

    def test_cancel_order_by_id_prefers_owning_client(self):
        """With preferred_client_id=3 and a pool of clientIds [2,3,4],
        the cancel must land on conns[1] (clientId=3) ONLY — fan-out
        to 2 and 4 must not happen."""
        client, conns = self._build_client_with_pool([2, 3, 4])

        client.cancel_order_by_id(123, preferred_client_id=3)

        # conns[1] got the targeted submit
        assert conns[1].submit.called, "owning client must receive the cancel"
        # The other (non-owning) scanner slots did NOT get a submit call
        assert not conns[2].submit.called, "non-owning client must not be called"
        # Own conn's _submit_to_ib was not invoked (we routed elsewhere)
        assert not client._submit_to_ib.called, \
            "preferred routing skipped own conn (owning is a different slot)"

    def test_cancel_prefers_own_conn_when_it_is_the_owner(self):
        """If the preferred slot IS the IBClient's own connection, we go
        through _submit_to_ib (not .submit on the pool conn) and skip
        fan-out."""
        client, conns = self._build_client_with_pool([2, 3, 4])
        # conns[0] is own_conn, clientId=2. Prefer it.
        client.cancel_order_by_id(456, preferred_client_id=2)

        assert client._submit_to_ib.called, "own-conn path uses _submit_to_ib"
        # Fan-out must NOT have run.
        assert not conns[1].submit.called
        assert not conns[2].submit.called


# ── 3. Falls back when preferred client isn't in the pool ─────────
class TestCancelFallsBackToFanout:
    def test_cancel_falls_back_to_fanout_when_preferred_not_in_pool(self):
        """preferred_client_id=99 is not in the pool. We must fall through
        to the legacy fan-out (own + every other conn)."""
        from broker.ib_client import IBClient

        conns = []
        for cid in (2, 3, 4):
            c = MagicMock()
            c.client_id = cid
            c.label = f"conn-{cid}"
            conns.append(c)

        pool = MagicMock()
        pool.all_connections = conns

        client = IBClient.__new__(IBClient)
        client._conn = conns[0]
        client._pool = pool
        client._pool_mode = True
        client.ib = MagicMock()
        client._submit_to_ib = MagicMock()

        client.cancel_order_by_id(789, preferred_client_id=99)

        # Fan-out ran: own via _submit_to_ib + the two non-own connections.
        assert client._submit_to_ib.called
        assert conns[1].submit.called
        assert conns[2].submit.called

    def test_cancel_falls_back_when_preferred_raises(self):
        """If the preferred client raises, we still fall back to fan-out."""
        from broker.ib_client import IBClient

        conns = []
        for cid in (2, 3, 4):
            c = MagicMock()
            c.client_id = cid
            c.label = f"conn-{cid}"
            conns.append(c)
        # preferred (clientId=3, conns[1]) raises
        conns[1].submit.side_effect = RuntimeError("boom")

        pool = MagicMock()
        pool.all_connections = conns

        client = IBClient.__new__(IBClient)
        client._conn = conns[0]
        client._pool = pool
        client._pool_mode = True
        client.ib = MagicMock()
        client._submit_to_ib = MagicMock()

        # Must not raise
        client.cancel_order_by_id(101, preferred_client_id=3)

        # Fell through to fan-out
        assert client._submit_to_ib.called
        # conns[2] was fanned out to as well
        assert conns[2].submit.called


# ── 4. Backward compat: preferred_client_id=None behaves as before ─
class TestCancelBackwardCompat:
    def test_cancel_no_preferred_works_as_before(self):
        """With preferred_client_id omitted / None, behavior matches the
        pre-Phase-5 fan-out exactly."""
        from broker.ib_client import IBClient

        own_conn = MagicMock()
        own_conn.client_id = 1
        scanner_a = MagicMock()
        scanner_a.client_id = 2
        scanner_b = MagicMock()
        scanner_b.client_id = 3

        pool = MagicMock()
        pool.all_connections = [own_conn, scanner_a, scanner_b]

        client = IBClient.__new__(IBClient)
        client._conn = own_conn
        client._pool = pool
        client._pool_mode = True
        client.ib = MagicMock()
        client._submit_to_ib = MagicMock()

        # Call the legacy way — no preferred_client_id
        client.cancel_order_by_id(9999)

        assert client._submit_to_ib.called
        assert scanner_a.submit.called
        assert scanner_b.submit.called


# ── 5. insert_trade persists ib_client_id ──────────────────────────
class TestInsertTradePersistsIbClientId:
    def test_insert_trade_persists_ib_client_id(self):
        """When the trade dict includes ib_client_id, the envelope row
        must carry that clientId into the DB."""
        from db import writer as writer_mod
        from db.models import Trade

        captured = {"envelope": None}

        class FakeSession:
            def __init__(self):
                self._next_id = 42

            def add(self, obj):
                if isinstance(obj, Trade):
                    captured["envelope"] = obj
                    if obj.id is None:
                        obj.id = self._next_id

            def flush(self):
                if captured["envelope"] and captured["envelope"].id is None:
                    captured["envelope"].id = self._next_id

            def commit(self):
                pass

            def close(self):
                pass

            def rollback(self):
                pass

        fake = FakeSession()

        # Patch get_session to return our fake; keep everything else real.
        original_get_session = writer_mod.get_session
        writer_mod.get_session = lambda: fake

        try:
            trade_dict = {
                "ticker": "SPY",
                "symbol": "SPY250101C00500000",
                "contracts": 1,
                "entry_price": 1.50,
                "profit_target": 3.00,
                "stop_loss": 0.75,
                "ib_client_id": 3,
            }
            result = writer_mod.insert_trade(trade_dict, account="TESTACCT")
        finally:
            writer_mod.get_session = original_get_session

        assert result == 42
        assert captured["envelope"] is not None
        assert captured["envelope"].ib_client_id == 3, \
            "insert_trade must stamp ib_client_id on the envelope"
