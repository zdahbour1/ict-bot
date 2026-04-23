"""ENH-046 — BAG/combo order placement tests.

Locks in the contract that ``IBClient.place_combo_order`` submits ONE
order against an IB Bag with N ComboLegs, returns a result dict shaped
like ``place_multi_leg_order`` plus ``combo_order_id`` +
``net_fill_price``. Uses the same ``_FakeIBClientNative`` pattern as
tests/unit/test_multi_leg.py.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# ── Test harness (mirrors the pattern from test_multi_leg.py) ──

class _FakeIBTrade:
    def __init__(self, order, status="Filled", fill=1.85):
        self.order = order
        self.orderStatus = SimpleNamespace(status=status, avgFillPrice=fill)
        # Fake per-leg fills so the combo serializer can attribute
        # prices to each conId.
        self.fills = []


class _FakeIBClientNative:
    """Simulates ib_async IB object enough for place_combo_order."""
    def __init__(self):
        self._next_id = 2000
        self.placed = []
        self.client = SimpleNamespace(clientId=9,
                                      getReqId=self._next_req_id)

    def _next_req_id(self):
        self._next_id += 1
        return self._next_id

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        order.permId = order.orderId * 10
        return _FakeIBTrade(order, status="Filled", fill=1.85)

    def sleep(self, _sec):
        return None

    def qualifyContracts(self, contract):
        contract.conId = 987654
        return [contract]


def _build_min_client():
    from broker.ib_orders import IBOrdersMixin

    class _C(IBOrdersMixin):
        def __init__(self):
            self.ib = _FakeIBClientNative()
            self._pool = None
            self._conn = None

        def _submit_to_ib(self, func, *a, **kw):
            kw.pop("timeout", None)
            return func(*a, **kw)

        def _occ_to_contract(self, symbol):
            # Unique conId per symbol so Bag.comboLegs has distinct legs.
            h = sum(ord(c) for c in symbol) * 10_000 + 100
            return SimpleNamespace(
                conId=h, secType="OPT",
                tradingClass=symbol[:3], symbol=symbol[:3],
                localSymbol=symbol,
            )

        def _get_last_error(self, _oid):
            return None

    return _C()


def _iron_condor_legs():
    return [
        {"sec_type": "OPT", "symbol": "SPY260501C00500000",
         "direction": "SHORT", "contracts": 1, "strike": 500, "right": "C",
         "expiry": "20260501", "leg_role": "short_call", "underlying": "SPY"},
        {"sec_type": "OPT", "symbol": "SPY260501C00510000",
         "direction": "LONG", "contracts": 1, "strike": 510, "right": "C",
         "expiry": "20260501", "leg_role": "long_call", "underlying": "SPY"},
        {"sec_type": "OPT", "symbol": "SPY260501P00500000",
         "direction": "SHORT", "contracts": 1, "strike": 500, "right": "P",
         "expiry": "20260501", "leg_role": "short_put", "underlying": "SPY"},
        {"sec_type": "OPT", "symbol": "SPY260501P00490000",
         "direction": "LONG", "contracts": 1, "strike": 490, "right": "P",
         "expiry": "20260501", "leg_role": "long_put", "underlying": "SPY"},
    ]


class TestComboOrderPlacement:
    def test_returns_expected_shape(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DRY_RUN", False, raising=False)
        monkeypatch.setattr(config, "IB_ACCOUNT", "", raising=False)

        client = _build_min_client()
        result = client.place_combo_order(
            _iron_condor_legs(), order_ref="dn-SPY-260501-01",
            action="SELL", limit_price=0.85,
        )

        assert result["combo_order_id"] is not None
        assert result["combo_perm_id"] is not None
        assert result["order_ref"] == "dn-SPY-260501-01"
        assert result["net_fill_price"] == pytest.approx(1.85)
        assert result["all_filled"] is True
        assert result["fills_received"] == 4
        assert result["ib_client_id"] == 9
        assert len(result["legs"]) == 4
        # Every leg row carries combo=True and the SAME order_id/perm_id
        order_ids = {l["order_id"] for l in result["legs"]}
        perm_ids = {l["perm_id"] for l in result["legs"]}
        assert len(order_ids) == 1, "all legs share one combo order_id"
        assert len(perm_ids) == 1, "all legs share one combo perm_id"
        assert all(l["combo"] is True for l in result["legs"])
        # leg_index is preserved
        assert [l["leg_index"] for l in result["legs"]] == [0, 1, 2, 3]

    def test_submits_exactly_one_order_against_a_bag(self, monkeypatch):
        """The whole point of ENH-046 — ONE order hitting IB, not 4."""
        import config
        monkeypatch.setattr(config, "DRY_RUN", False, raising=False)
        monkeypatch.setattr(config, "IB_ACCOUNT", "", raising=False)

        client = _build_min_client()
        client.place_combo_order(_iron_condor_legs(), order_ref="x",
                                  action="SELL", limit_price=1.00)

        assert len(client.ib.placed) == 1, (
            "combo path must submit ONE IB order, not 4 independent ones"
        )
        contract, _order = client.ib.placed[0]
        assert contract.secType == "BAG"
        assert contract.symbol == "SPY"
        assert len(contract.comboLegs) == 4
        # Iron condor action mapping: short_call=SELL, long_call=BUY,
        # short_put=SELL, long_put=BUY
        actions = [cl.action for cl in contract.comboLegs]
        assert actions == ["SELL", "BUY", "SELL", "BUY"]
        # Each comboLeg has a distinct conId (harness produces unique
        # conIds per symbol)
        con_ids = [cl.conId for cl in contract.comboLegs]
        assert len(set(con_ids)) == 4, "every leg must carry its own conId"

    def test_uses_limit_price_when_provided(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DRY_RUN", False, raising=False)
        monkeypatch.setattr(config, "IB_ACCOUNT", "", raising=False)

        client = _build_min_client()
        client.place_combo_order(_iron_condor_legs()[:2],
                                  order_ref="x",
                                  action="SELL", limit_price=2.50)

        _, order = client.ib.placed[0]
        # Limit price threaded through; ib_async LimitOrder exposes it
        # as lmtPrice.
        assert getattr(order, "lmtPrice", None) == pytest.approx(2.50)
        assert order.orderType in ("LMT", "LIMIT")

    def test_market_order_when_no_limit_price(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DRY_RUN", False, raising=False)
        monkeypatch.setattr(config, "IB_ACCOUNT", "", raising=False)

        client = _build_min_client()
        client.place_combo_order(_iron_condor_legs()[:2], order_ref="x",
                                  action="SELL", limit_price=None)

        _, order = client.ib.placed[0]
        assert order.orderType in ("MKT", "MARKET")

    def test_dry_run_returns_stub_without_hitting_ib(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DRY_RUN", True, raising=False)

        client = _build_min_client()
        result = client.place_combo_order(_iron_condor_legs(),
                                           order_ref="x",
                                           action="SELL", limit_price=1.0)

        assert result["dry_run"] is True
        assert len(result["legs"]) == 4
        # Nothing was actually submitted to IB
        assert len(client.ib.placed) == 0

    def test_rejects_empty_leg_list(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DRY_RUN", False, raising=False)

        client = _build_min_client()
        with pytest.raises(RuntimeError, match=">= 1 leg"):
            client.place_combo_order([], order_ref="x",
                                      action="SELL", limit_price=1.0)

    def test_order_ref_is_stamped_on_ib_order(self, monkeypatch):
        """IB↔DB correlation depends on orderRef."""
        import config
        monkeypatch.setattr(config, "DRY_RUN", False, raising=False)
        monkeypatch.setattr(config, "IB_ACCOUNT", "", raising=False)

        client = _build_min_client()
        client.place_combo_order(_iron_condor_legs()[:2],
                                  order_ref="dn-SPY-260501-42",
                                  action="SELL", limit_price=1.0)

        _, order = client.ib.placed[0]
        assert order.orderRef == "dn-SPY-260501-42"
