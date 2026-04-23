"""ENH-050 — combo per-leg fill-price recovery chain + price_source tag.

Four-stage fallback in ``_ib_place_combo``:
  1. ib_trade.fills         → price_source='exec'
  2. ib.executions() stream  → price_source='exec'
  3. post-fill mid quote     → price_source='quote'
  4. proportional split      → price_source='proportional'

Every leg in the result MUST carry a price_source tag so the UI can
show an "est" badge and audits can distinguish real fills from
estimates. Persistence mirrors the tag into trade_legs.price_source.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class _FakeFill:
    def __init__(self, conId, avgPrice):
        self.contract = SimpleNamespace(conId=conId)
        self.execution = SimpleNamespace(avgPrice=avgPrice)


class _FakeExecution:
    """Matches the shape of ib.executions() rows."""
    def __init__(self, orderId, conId, avgPrice):
        self.contract = SimpleNamespace(conId=conId)
        self.execution = SimpleNamespace(orderId=orderId, avgPrice=avgPrice)


class _FakeIBTrade:
    def __init__(self, order, fills=None, status="Filled", avg=1.85):
        self.order = order
        self.orderStatus = SimpleNamespace(status=status, avgFillPrice=avg)
        self.fills = fills or []


class _FakeIB:
    def __init__(self, executions=None):
        self._next_id = 5000
        self.placed: list = []
        self.client = SimpleNamespace(clientId=11,
                                      getReqId=self._next_req_id)
        self._executions = executions or []

    def _next_req_id(self):
        self._next_id += 1
        return self._next_id

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        order.permId = order.orderId * 10
        # Use pre-set ib_trade_fills if caller rigged it
        fills = getattr(self, "_next_fills", []) or []
        self._next_fills = []
        return _FakeIBTrade(order, fills=fills,
                            avg=getattr(self, "_next_avg", 1.85))

    def sleep(self, _s):
        return None

    def qualifyContracts(self, c):
        c.conId = 77700 + sum(ord(x) for x in getattr(c, "symbol", "") or "")
        return [c]

    def executions(self):
        return self._executions


def _build_mixin(client_stub):
    """Attach IBOrdersMixin methods to a minimal stub."""
    from broker.ib_orders import IBOrdersMixin

    class _C(IBOrdersMixin):
        def __init__(self, ib):
            self.ib = ib
            self._pool = None
            self._conn = None
        def _submit_to_ib(self, fn, *a, **kw):
            kw.pop("timeout", None)
            return fn(*a, **kw)
        def _occ_to_contract(self, symbol):
            # Deterministic conId from symbol so Stage 1/2 lookups match
            return SimpleNamespace(
                conId=77000 + sum(ord(c) for c in symbol),
                secType="OPT",
                tradingClass=symbol[:3], symbol=symbol[:3],
                localSymbol=symbol,
            )
        def _get_last_error(self, _oid):
            return None
        # Stage 3 stub: caller can override to test the quote fallback
        def get_option_price(self, symbol):
            return client_stub.get_option_price(symbol)

    return _C


def _iron_condor():
    return [
        {"sec_type": "OPT", "symbol": "SPY260515C00500000",
         "direction": "SHORT", "contracts": 1, "strike": 500, "right": "C",
         "expiry": "20260515", "leg_role": "short_call", "underlying": "SPY"},
        {"sec_type": "OPT", "symbol": "SPY260515C00510000",
         "direction": "LONG", "contracts": 1, "strike": 510, "right": "C",
         "expiry": "20260515", "leg_role": "long_call", "underlying": "SPY"},
        {"sec_type": "OPT", "symbol": "SPY260515P00500000",
         "direction": "SHORT", "contracts": 1, "strike": 500, "right": "P",
         "expiry": "20260515", "leg_role": "short_put", "underlying": "SPY"},
        {"sec_type": "OPT", "symbol": "SPY260515P00490000",
         "direction": "LONG", "contracts": 1, "strike": 490, "right": "P",
         "expiry": "20260515", "leg_role": "long_put", "underlying": "SPY"},
    ]


class TestRecoveryChain:
    def test_stage_1_fills_tag_as_exec(self, monkeypatch):
        """When ib_trade.fills has per-leg prices, every leg gets
        price_source='exec' (the best case)."""
        import config
        monkeypatch.setattr(config, "DRY_RUN", False, raising=False)
        monkeypatch.setattr(config, "IB_ACCOUNT", "", raising=False)

        legs = _iron_condor()
        # Compute the conIds the mixin will assign via _occ_to_contract
        con_ids = [77000 + sum(ord(c) for c in l["symbol"]) for l in legs]
        ib = _FakeIB()
        # Rig the next placeOrder to return fills covering every leg
        ib._next_fills = [_FakeFill(c, 2.0 + i*0.25)
                          for i, c in enumerate(con_ids)]
        stub = SimpleNamespace(get_option_price=lambda s: 999.0)  # should NOT be used
        Client = _build_mixin(stub)
        client = Client(ib)
        result = client.place_combo_order(legs, order_ref="x",
                                           action="SELL", limit_price=1.0)
        for leg in result["legs"]:
            assert leg["price_source"] == "exec", \
                f"leg {leg['leg_index']} got {leg['price_source']}"
            assert leg["fill_price"] > 0

    def test_stage_2_executions_when_fills_empty(self, monkeypatch):
        """When ib_trade.fills is empty, fall through to
        ib.executions() which has per-leg prices."""
        import config
        monkeypatch.setattr(config, "DRY_RUN", False, raising=False)
        monkeypatch.setattr(config, "IB_ACCOUNT", "", raising=False)

        legs = _iron_condor()
        con_ids = [77000 + sum(ord(c) for c in l["symbol"]) for l in legs]
        # Empty fills → Stage 2 must kick in
        executions = []   # will populate after we know orderId
        ib = _FakeIB(executions=executions)
        ib._next_fills = []

        # Patch placeOrder wrapper so we can grab the order.orderId
        Client = _build_mixin(SimpleNamespace(get_option_price=lambda s: 999.0))
        client = Client(ib)

        # Pre-seed executions with an orderId that matches what
        # placeOrder will assign. _next_req_id increments from 5000.
        # First call: contracts qualify bumps conIds. Actual order
        # creation uses getReqId = 5005 (after 4 legs + combo order).
        # Simplest: run the call, then inspect to know the orderId.
        # To do this cleanly, patch executions() dynamically after
        # the call — but the mixin calls executions() inside the
        # function. Instead: use a callable executions list.
        class _DynExec(list):
            def __init__(self, ib):
                super().__init__()
                self._ib = ib
            def __call__(self):
                # Rebuild against the latest placed orderId
                placed = self._ib.placed
                if not placed:
                    return []
                oid = placed[-1][1].orderId
                return [_FakeExecution(oid, c, 2.0 + i*0.25)
                        for i, c in enumerate(con_ids)]
        ib._executions = _DynExec(ib)
        # Bind executions() to use the dynamic callable
        ib.executions = ib._executions

        result = client.place_combo_order(legs, order_ref="x",
                                           action="SELL", limit_price=1.0)
        for leg in result["legs"]:
            assert leg["price_source"] == "exec", \
                f"leg {leg['leg_index']} fell through Stage 2 — src={leg['price_source']}"

    def test_stage_3_quote_when_executions_silent(self, monkeypatch):
        """When fills + executions are empty, each leg gets a quote
        via get_option_price and price_source='quote'."""
        import config
        monkeypatch.setattr(config, "DRY_RUN", False, raising=False)
        monkeypatch.setattr(config, "IB_ACCOUNT", "", raising=False)

        legs = _iron_condor()
        ib = _FakeIB(executions=[])
        ib._next_fills = []
        stub_mids = {l["symbol"]: 1.10 + i*0.15 for i, l in enumerate(legs)}
        stub = SimpleNamespace(
            get_option_price=lambda s: stub_mids.get(s, 0.0))
        Client = _build_mixin(stub)
        client = Client(ib)
        result = client.place_combo_order(legs, order_ref="x",
                                           action="SELL", limit_price=1.0)
        for leg in result["legs"]:
            assert leg["price_source"] == "quote", \
                f"leg {leg['leg_index']} src={leg['price_source']}"
            assert leg["fill_price"] == pytest.approx(
                stub_mids[leg["symbol"]], abs=0.01)

    def test_stage_4_proportional_last_resort(self, monkeypatch):
        """All quote paths fail (quote returns 0). Combo net_fill_price
        is positive. Each leg gets equal-share proportional price +
        price_source='proportional'."""
        import config
        monkeypatch.setattr(config, "DRY_RUN", False, raising=False)
        monkeypatch.setattr(config, "IB_ACCOUNT", "", raising=False)

        legs = _iron_condor()
        ib = _FakeIB(executions=[])
        ib._next_fills = []
        ib._next_avg = 4.0   # combo net_fill_price
        stub = SimpleNamespace(get_option_price=lambda s: 0.0)
        Client = _build_mixin(stub)
        client = Client(ib)
        result = client.place_combo_order(legs, order_ref="x",
                                           action="SELL", limit_price=1.0)
        for leg in result["legs"]:
            assert leg["price_source"] == "proportional", \
                f"leg {leg['leg_index']} src={leg['price_source']}"
            # 4.0 / 4 legs = 1.0 per leg
            assert leg["fill_price"] == pytest.approx(1.0, abs=0.01)


class TestWriterPersistsPriceSource:
    def test_leg_kwargs_include_price_source(self):
        """Static guard: db/writer.py::insert_multi_leg_trade must set
        leg_kwargs['price_source'] from leg.get('price_source') when
        present. Regression test for ENH-050 — without this, UI badges
        never show because the DB column stays NULL."""
        import inspect
        from db import writer
        src = inspect.getsource(writer.insert_multi_leg_trade)
        assert 'leg.get("price_source")' in src or \
               "leg.get('price_source')" in src, (
            "insert_multi_leg_trade must read price_source from the "
            "input leg dict and persist it on trade_legs."
        )
        assert "price_source" in src, (
            "price_source must appear somewhere in insert_multi_leg_trade"
        )
