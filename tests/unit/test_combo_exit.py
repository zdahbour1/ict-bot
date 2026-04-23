"""ENH-046 Phase 2 tests — combo close path + reconciliation skip.

Covers:
1. ``place_combo_close_order`` reverses every leg's direction and
   submits ONE Bag order (not 4 independent closes).
2. ``_execute_multi_leg_exit`` routes through combo close when the
   flag is on; falls back to per-leg on failure.
3. Reconciliation skips bracket restoration for n_legs > 1 rows —
   defined-risk spreads don't want per-leg SLs.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── Fake IB plumbing (mirrors test_combo_order harness) ──

class _FakeIBTrade:
    def __init__(self, order, status="Filled", fill=0.60):
        self.order = order
        self.orderStatus = SimpleNamespace(status=status, avgFillPrice=fill)
        self.fills = []


class _FakeIBClientNative:
    def __init__(self):
        self._next_id = 3000
        self.placed = []
        self.client = SimpleNamespace(clientId=9,
                                      getReqId=self._next_req_id)

    def _next_req_id(self):
        self._next_id += 1
        return self._next_id

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        order.permId = order.orderId * 10
        return _FakeIBTrade(order)

    def sleep(self, _s):
        return None

    def qualifyContracts(self, c):
        c.conId = 555555
        return [c]


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
            h = sum(ord(c) for c in symbol) * 1000 + 100
            return SimpleNamespace(
                conId=h, secType="OPT",
                tradingClass=symbol[:3], symbol=symbol[:3],
                localSymbol=symbol,
            )

        def _get_last_error(self, _oid):
            return None

    return _C()


def _iron_condor_open_legs():
    """Four iron-condor legs as stored at open (ready for closing)."""
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


class TestPlaceComboCloseOrder:
    def test_reverses_every_leg_direction(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DRY_RUN", False, raising=False)
        monkeypatch.setattr(config, "IB_ACCOUNT", "", raising=False)

        client = _build_min_client()
        result = client.place_combo_close_order(
            _iron_condor_open_legs(), order_ref="x-close",
            limit_price=None,
        )

        assert result["all_filled"] is True
        assert len(client.ib.placed) == 1, (
            "combo close must submit ONE Bag order"
        )
        bag, _order = client.ib.placed[0]
        assert bag.secType == "BAG"
        # Original iron condor: short_call, long_call, short_put, long_put
        # Reversed:              BUY,        SELL,      BUY,       SELL
        actions = [cl.action for cl in bag.comboLegs]
        assert actions == ["BUY", "SELL", "BUY", "SELL"]

    def test_order_ref_threaded_through(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DRY_RUN", False, raising=False)

        client = _build_min_client()
        client.place_combo_close_order(
            _iron_condor_open_legs(), order_ref="dn-SPY-260515-01-close",
        )
        _, order = client.ib.placed[0]
        assert order.orderRef == "dn-SPY-260515-01-close"


class TestReconciliationSkipsMultiLegBrackets:
    """ENH-046 Phase 2A: n_legs > 1 trades are defined-risk spreads —
    the unprotected-position restore path must not fire per-leg SLs
    on them. Regression test locks the skip in."""

    def test_multi_leg_row_continue_is_wired(self):
        """Static guard: reconciliation source must contain the early
        continue on n_legs > 1 or the restore path is unguarded."""
        import inspect
        from strategy import reconciliation as rc
        src = inspect.getsource(rc)
        # The query must SELECT n_legs
        assert "COALESCE(t.n_legs" in src, (
            "unprotected-position query must include n_legs so the "
            "restore path can skip multi-leg defined-risk trades"
        )
        # And must early-continue when n_legs > 1
        assert "int(n_legs or 1) > 1" in src, (
            "reconciliation must skip bracket restoration for "
            "defined-risk multi-leg trades"
        )


class TestMultiLegExitRoutesToCombo:
    """ENH-046 Phase 2C: when the combo flag is on, exit goes through
    place_combo_close_order (ONE order) instead of N per-leg closes."""

    def test_routes_to_combo_close_when_flag_on(self, monkeypatch):
        """Exit with flag True → client.place_combo_close_order called,
        per-leg close methods NOT called."""
        import config
        monkeypatch.setattr(config, "USE_COMBO_ORDERS_FOR_MULTI_LEG", True,
                             raising=False)

        fake_client = MagicMock()
        fake_client.place_combo_close_order = MagicMock(return_value={
            "all_filled": True,
            "combo_order_id": 1234,
            "net_fill_price": 0.35,
            "legs": [{"leg_index": i, "status": "Filled", "fill_price": 0.1}
                     for i in range(4)],
        })

        trade = {
            "db_id": 777, "ticker": "SPY",
            "client_trade_id": "dn-SPY-260515-01",
            "ib_client_id": 7,
        }

        # Stub out the DB-backed leg fetch with canned legs
        from strategy import exit_executor as ee
        canned_legs = [{
            "leg_index": i, "symbol": f"SPY260515C005{i}0000",
            "leg_role": ["short_call", "long_call", "short_put", "long_put"][i],
            "direction": ["SHORT", "LONG", "SHORT", "LONG"][i],
            "contracts_open": 1, "sec_type": "OPT",
            "strike": 500.0, "right": "C" if i < 2 else "P",
            "expiry": "20260515", "multiplier": 100,
            "ib_tp_perm_id": None, "ib_sl_perm_id": None,
        } for i in range(4)]
        monkeypatch.setattr(ee, "_fetch_open_legs", lambda _tid: canned_legs)
        monkeypatch.setattr(ee, "_trace", lambda *a, **kw: None)

        ee._execute_multi_leg_exit(fake_client, trade, "TIME_EXIT")

        fake_client.place_combo_close_order.assert_called_once()
        # The per-leg close methods should NOT have been called since
        # the combo path succeeded.
        assert not fake_client.sell_call.called
        assert not fake_client.sell_put.called

    def test_falls_back_to_per_leg_if_combo_raises(self, monkeypatch):
        """Combo path throws → exit still closes via per-leg methods."""
        import config
        monkeypatch.setattr(config, "USE_COMBO_ORDERS_FOR_MULTI_LEG", True,
                             raising=False)

        fake_client = MagicMock()
        fake_client.place_combo_close_order = MagicMock(
            side_effect=RuntimeError("IB combo rejected")
        )
        # Per-leg methods succeed
        fake_client.buy_call = MagicMock(return_value={"status": "Filled"})
        fake_client.sell_call = MagicMock(return_value={"status": "Filled"})
        fake_client.buy_put = MagicMock(return_value={"status": "Filled"})
        fake_client.sell_put = MagicMock(return_value={"status": "Filled"})

        trade = {
            "db_id": 778, "ticker": "SPY",
            "client_trade_id": "dn-SPY-260515-02",
            "ib_client_id": 7,
        }
        from strategy import exit_executor as ee
        canned_legs = [{
            "leg_index": 0, "symbol": "SPY260515C00500000",
            "leg_role": "short_call", "direction": "SHORT",
            "contracts_open": 1, "sec_type": "OPT",
            "strike": 500.0, "right": "C", "expiry": "20260515",
            "multiplier": 100,
            "ib_tp_perm_id": None, "ib_sl_perm_id": None,
        }]
        monkeypatch.setattr(ee, "_fetch_open_legs", lambda _tid: canned_legs)
        monkeypatch.setattr(ee, "_trace", lambda *a, **kw: None)

        ee._execute_multi_leg_exit(fake_client, trade, "TIME_EXIT")

        # Combo was attempted
        fake_client.place_combo_close_order.assert_called_once()
        # Per-leg fall-back also ran. For short_call (SHORT + C) the
        # close method is buy_call.
        assert fake_client.buy_call.called
