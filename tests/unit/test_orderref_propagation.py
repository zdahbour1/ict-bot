"""Regression: every IB order-placement path must stamp
``order.orderRef`` so the TWS Activity tab's Order Ref column is
never blank.

Before 2026-04-24 afternoon, the single-leg path (buy_call / buy_put /
sell_call / sell_put via _ib_place_order) did not accept or set
orderRef. Same for the stock hedge (_ib_place_stock_order). As a
result, ICT single-leg trades + DN delta-hedge stock orders all
showed blank Order Ref in TWS.

Fix verified here:
- All four option helpers accept order_ref
- buy_stock / sell_stock accept order_ref
- _ib_place_order and _ib_place_stock_order actually set
  ``order.orderRef`` on the IB order before placeOrder.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


class _FakeIB:
    def __init__(self):
        self.placed: list = []
        self._next_id = 9000
        self.client = SimpleNamespace(clientId=1, getReqId=self._next_req_id)

    def _next_req_id(self):
        self._next_id += 1
        return self._next_id

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        order.permId = order.orderId * 10

        class _FakeStatus:
            status = "Filled"
            avgFillPrice = 1.0
        class _FakeTrade:
            orderStatus = _FakeStatus()
            fills = []
        t = _FakeTrade()
        t.order = order
        return t

    def sleep(self, _s):
        return None

    def qualifyContracts(self, c):
        c.conId = 55555
        return [c]


def _build_client():
    from broker.ib_orders import IBOrdersMixin

    class _C(IBOrdersMixin):
        def __init__(self):
            self.ib = _FakeIB()
            self._pool = None
            self._conn = None

        def _submit_to_ib(self, fn, *a, **kw):
            kw.pop("timeout", None)
            return fn(*a, **kw)

        def _occ_to_contract(self, symbol):
            return SimpleNamespace(
                conId=sum(ord(c) for c in symbol) + 1000,
                secType="OPT",
                tradingClass=symbol[:3],
                symbol=symbol[:3],
                localSymbol=symbol,
            )

        def _get_last_error(self, _oid):
            return None

    return _C()


class TestSingleLegOrderRef:
    @pytest.mark.parametrize("method,action", [
        ("buy_call", "BUY"), ("buy_put", "BUY"),
        ("sell_call", "SELL"), ("sell_put", "SELL"),
    ])
    def test_single_leg_stamps_orderRef(self, method, action, monkeypatch):
        import config
        monkeypatch.setattr(config, "DRY_RUN", False, raising=False)
        monkeypatch.setattr(config, "IB_ACCOUNT", "", raising=False)

        client = _build_client()
        getattr(client, method)("SPY260501C00500000", 1,
                                  order_ref="ict-SPY-260501-01")
        assert len(client.ib.placed) == 1
        _, order = client.ib.placed[0]
        assert order.orderRef == "ict-SPY-260501-01", (
            f"{method} must stamp orderRef on the IB order — got "
            f"{getattr(order, 'orderRef', None)!r}"
        )
        assert order.action == action

    def test_no_ref_leaves_blank_backward_compat(self, monkeypatch):
        """Calling without order_ref still works — orderRef stays
        whatever ib_async defaults to (usually empty string). No
        exception."""
        import config
        monkeypatch.setattr(config, "DRY_RUN", False, raising=False)
        monkeypatch.setattr(config, "IB_ACCOUNT", "", raising=False)
        client = _build_client()
        client.buy_call("SPY260501C00500000", 1)
        assert len(client.ib.placed) == 1
        # No AssertionError; backward compat holds.


class TestStockHedgeOrderRef:
    def test_buy_stock_stamps_orderRef(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DRY_RUN", False, raising=False)
        monkeypatch.setattr(config, "IB_ACCOUNT", "", raising=False)

        client = _build_client()
        client.buy_stock("SPY", 10, order_ref="hedge-SPY-tid123")
        _, order = client.ib.placed[0]
        assert order.orderRef == "hedge-SPY-tid123"

    def test_sell_stock_stamps_orderRef(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DRY_RUN", False, raising=False)
        monkeypatch.setattr(config, "IB_ACCOUNT", "", raising=False)

        client = _build_client()
        client.sell_stock("QQQ", 5, order_ref="hedge-QQQ-tid456")
        _, order = client.ib.placed[0]
        assert order.orderRef == "hedge-QQQ-tid456"


class TestCallerPropagation:
    """Static guards — catch if anyone removes the ref threading
    in the key call sites."""

    def test_option_selector_passes_order_ref_on_buy_call(self):
        import inspect
        from strategy import option_selector
        src = inspect.getsource(option_selector)
        assert "client.buy_call(option_symbol, contracts,\n" in src \
               or "client.buy_call(option_symbol, contracts, order_ref=" in src
        assert "client.buy_put(option_symbol, contracts,\n" in src \
               or "client.buy_put(option_symbol, contracts, order_ref=" in src

    def test_exit_executor_uses_close_ref_on_sell(self):
        import inspect
        from strategy import exit_executor
        src = inspect.getsource(exit_executor.close_position_on_ib)
        assert "-close" in src, (
            "close_position_on_ib must build a '-close' suffixed ref"
        )
        assert "order_ref=" in src
