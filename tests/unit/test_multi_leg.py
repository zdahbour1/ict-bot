"""
Unit tests for Phase 6 (multi-strategy v2) multi-leg trade execution.

Covers:
  1. LegSpec dataclass creation with required fields.
  2. IBClient.place_multi_leg_order returns expected result shape
     with an entry per leg (mocked IB).
  3. insert_multi_leg_trade writes one trades envelope + N trade_legs
     rows in ONE session transaction.
  4. TradeEntryManager routes to place_multi_leg_order when plugin.
     place_legs() returns legs.
  5. TradeEntryManager falls back to the legacy single-leg path when
     place_legs() returns None.
  6. DeltaNeutralStrategy.place_legs returns 4 LegSpec objects forming
     an iron condor with the expected leg_role values.
"""
from __future__ import annotations

from unittest.mock import MagicMock
from types import SimpleNamespace

import pytest

from strategy.base_strategy import LegSpec, Signal, BaseStrategy
from strategy.delta_neutral_strategy import DeltaNeutralStrategy
from strategy.trade_entry_manager import TradeEntryManager


# ─── 1. LegSpec basics ────────────────────────────────────────

def test_leg_spec_dataclass_basic():
    leg = LegSpec(
        sec_type="OPT",
        symbol="SPY260501C00500000",
        direction="SHORT",
        contracts=1,
        strike=500.0,
        right="C",
        expiry="20260501",
        leg_role="short_call",
        underlying="SPY",
    )
    assert leg.sec_type == "OPT"
    assert leg.symbol == "SPY260501C00500000"
    assert leg.direction == "SHORT"
    assert leg.contracts == 1
    assert leg.strike == 500.0
    assert leg.right == "C"
    assert leg.expiry == "20260501"
    assert leg.leg_role == "short_call"
    assert leg.underlying == "SPY"
    # Defaults
    assert leg.multiplier == 100
    assert leg.exchange == "SMART"
    assert leg.currency == "USD"


# ─── 2. place_multi_leg_order shape (mocked IB) ───────────────

class _FakeIBTrade:
    def __init__(self, order, status="Filled", fill=1.25):
        self.order = order
        self.orderStatus = SimpleNamespace(status=status, avgFillPrice=fill)
        self.fills = []


class _FakeIBClientNative:
    """Simulates ib_async IB object enough for place_multi_leg_order."""
    def __init__(self):
        self._next_id = 1000
        self.placed = []
        self.client = SimpleNamespace(clientId=7,
                                      getReqId=self._next_req_id)

    def _next_req_id(self):
        self._next_id += 1
        return self._next_id

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        order.permId = order.orderId * 10
        return _FakeIBTrade(order, status="Filled", fill=1.25)

    def sleep(self, _sec):
        return None

    def qualifyContracts(self, contract):
        contract.conId = 9999
        return [contract]


def _build_min_client():
    """Build an object that has the IBOrdersMixin methods bound."""
    from broker.ib_orders import IBOrdersMixin

    class _C(IBOrdersMixin):
        def __init__(self):
            self.ib = _FakeIBClientNative()
            self._pool = None
            self._conn = None

        def _submit_to_ib(self, func, *a, **kw):
            # Strip the 'timeout' kwarg the mixin passes
            kw.pop("timeout", None)
            return func(*a, **kw)

        def _occ_to_contract(self, symbol):
            return SimpleNamespace(conId=123456,
                                    secType="OPT",
                                    tradingClass=symbol[:3],
                                    symbol=symbol[:3],
                                    localSymbol=symbol)

        def _get_last_error(self, _oid):
            return None

    return _C()


def test_place_multi_leg_order_returns_expected_shape(monkeypatch):
    import config
    monkeypatch.setattr(config, "DRY_RUN", False, raising=False)
    monkeypatch.setattr(config, "IB_ACCOUNT", "", raising=False)

    client = _build_min_client()
    legs = [
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

    result = client.place_multi_leg_order(legs, order_ref="dn-SPY-260420-01")

    assert "oca_group" in result
    assert result["oca_group"].startswith("MULTILEG-")
    assert result["order_ref"] == "dn-SPY-260420-01"
    assert isinstance(result["legs"], list)
    assert len(result["legs"]) == 4
    assert result["all_filled"] is True
    assert result["fills_received"] == 4
    assert result["ib_client_id"] == 7
    for i, leg in enumerate(result["legs"]):
        assert leg["leg_index"] == i
        assert leg["status"] == "Filled"
        assert leg["fill_price"] == 1.25
        assert leg["order_id"] is not None
        assert leg["perm_id"] is not None
        assert leg["client_id"] == 7


def test_entry_legs_not_in_oca_group_on_ib(monkeypatch):
    """Regression for 2026-04-23 AVGO partial-condor bug.

    The earlier implementation stamped ``order.ocaGroup`` + ``ocaType=1``
    on every entry leg, which caused IB to cancel all siblings the
    instant one leg filled — so only 1 of 4 iron-condor legs ever made
    it into the position. Entry legs must NOT carry an OCA group on
    the IB-side; the oca_group label in our result dict is just for
    internal correlation.
    """
    import config
    monkeypatch.setattr(config, "DRY_RUN", False, raising=False)
    monkeypatch.setattr(config, "IB_ACCOUNT", "", raising=False)

    client = _build_min_client()
    legs = [
        {"sec_type": "OPT", "symbol": "SPY260501C00500000",
         "direction": "SHORT", "contracts": 1, "strike": 500, "right": "C",
         "expiry": "20260501", "leg_role": "short_call", "underlying": "SPY"},
        {"sec_type": "OPT", "symbol": "SPY260501C00510000",
         "direction": "LONG", "contracts": 1, "strike": 510, "right": "C",
         "expiry": "20260501", "leg_role": "long_call", "underlying": "SPY"},
    ]
    client.place_multi_leg_order(legs, order_ref="dn-SPY-260420-01")

    # Inspect every order that was actually handed to IB.
    assert len(client.ib.placed) == 2
    for _contract, order in client.ib.placed:
        oca_group_on_ib = getattr(order, "ocaGroup", "")
        oca_type_on_ib = getattr(order, "ocaType", 0)
        assert not oca_group_on_ib, (
            f"ENTRY order must not have ocaGroup set on IB — was "
            f"{oca_group_on_ib!r}; that causes cancel-on-fill and "
            f"breaks multi-leg entries."
        )
        assert not oca_type_on_ib, (
            f"ENTRY order must not set ocaType — was {oca_type_on_ib}"
        )


# ─── 3. insert_multi_leg_trade writes envelope + N legs in one tx ───

def test_insert_multi_leg_trade_writes_envelope_and_legs(monkeypatch):
    """Mock the session so we can observe: 1 Trade added, N TradeLegs
    added, exactly ONE commit call — i.e. all in the same transaction."""
    from db import writer as writer_mod

    added = []
    commits = []
    rollbacks = []

    class _FakeSession:
        def add(self, obj):
            added.append(obj)
        def flush(self):
            # assign envelope id
            for a in added:
                if getattr(a, "id", None) is None and hasattr(a, "ticker"):
                    a.id = 42
        def commit(self):
            commits.append(True)
        def rollback(self):
            rollbacks.append(True)
        def close(self):
            pass

    monkeypatch.setattr(writer_mod, "get_session", lambda: _FakeSession())

    envelope = {
        "strategy_id": 9,
        "ticker": "SPY",
        "signal_type": "DELTA_NEUTRAL_CONDOR",
        "client_trade_id": "delta_neutral-SPY-260420-01",
        "n_legs": 4,
        "ib_client_id": 7,
    }
    legs_result = {
        "oca_group": "MULTILEG-123",
        "order_ref": envelope["client_trade_id"],
        "legs": [
            {"leg_index": i, "symbol": f"SPY260501X0050000{i}",
             "leg_role": r, "sec_type": "OPT", "direction": d,
             "contracts": 1, "order_id": 1000 + i, "perm_id": 9000 + i,
             "con_id": 5000 + i, "status": "Filled", "fill_price": 1.25,
             "client_id": 7, "strike": 500 + i, "right": "C",
             "expiry": "20260501", "underlying": "SPY"}
            for i, (r, d) in enumerate([
                ("short_call", "SHORT"), ("long_call", "LONG"),
                ("short_put", "SHORT"), ("long_put", "LONG"),
            ])
        ],
        "all_filled": True, "fills_received": 4, "ib_client_id": 7,
    }

    trade_id = writer_mod.insert_multi_leg_trade(envelope, legs_result,
                                                  account="paper")
    assert trade_id == 42
    assert len(commits) == 1
    assert not rollbacks
    # 1 envelope + 4 legs
    from db.models import Trade, TradeLeg
    trades_added = [a for a in added if isinstance(a, Trade)]
    legs_added = [a for a in added if isinstance(a, TradeLeg)]
    assert len(trades_added) == 1
    assert len(legs_added) == 4
    assert trades_added[0].n_legs == 4
    assert trades_added[0].ticker == "SPY"
    assert trades_added[0].strategy_id == 9
    assert trades_added[0].client_trade_id == envelope["client_trade_id"]
    for i, leg in enumerate(legs_added):
        assert leg.leg_index == i
        assert leg.contracts_entered == 1
        assert leg.contracts_open == 1
        assert leg.trade_id == 42


# ─── 4/5. TradeEntryManager routing ──────────────────────────

class _FakeClientForEM:
    def __init__(self):
        self.multi_leg_calls = []
        self.bracket_calls = []

    def get_ib_positions_raw(self):
        return []
    def find_open_orders_for_contract(self, *_a, **_kw):
        return []
    def place_multi_leg_order(self, legs, order_ref=None):
        self.multi_leg_calls.append((legs, order_ref))
        return {
            "oca_group": "MULTILEG-1",
            "order_ref": order_ref,
            "legs": [dict(leg, leg_index=i, order_id=1000+i,
                          perm_id=9000+i, con_id=5000+i,
                          status="Filled", fill_price=1.0,
                          client_id=7) for i, leg in enumerate(legs)],
            "all_filled": True, "fills_received": len(legs),
            "ib_client_id": 7,
        }
    def place_combo_order(self, legs, order_ref=None, action="BUY",
                          limit_price=None):
        if not hasattr(self, "combo_calls"):
            self.combo_calls = []
        self.combo_calls.append((legs, order_ref, action, limit_price))
        return {
            "combo_order_id": 42_000, "combo_perm_id": 420_000,
            "order_ref": order_ref, "net_fill_price": 1.25,
            "legs": [dict(leg, leg_index=i, order_id=42_000,
                          perm_id=420_000, con_id=5000+i,
                          status="Filled", fill_price=1.0,
                          client_id=7, combo=True)
                     for i, leg in enumerate(legs)],
            "all_filled": True, "fills_received": len(legs),
            "ib_client_id": 7,
        }
    def place_bracket_order(self, *a, **kw):
        self.bracket_calls.append((a, kw))
        return {"symbol": "X", "status": "Filled", "fill_price": 1.0,
                "ib_order_id": 1, "ib_perm_id": 1}


class _FakeExitManager:
    def __init__(self):
        self.open_trades = []
    def invalidate_cache(self):
        pass
    def add_trade(self, t):
        self.open_trades.append(t)


class _MultiLegPlugin(BaseStrategy):
    @property
    def name(self): return "multi_test"
    @property
    def description(self): return "multi-leg test plugin"
    def detect(self, *a, **kw): return []
    def place_legs(self, signal):
        return [
            LegSpec(sec_type="OPT", symbol="AAA", direction="SHORT",
                    contracts=1, strike=100, right="C", expiry="20260501",
                    leg_role="short_call", underlying=signal.ticker),
            LegSpec(sec_type="OPT", symbol="BBB", direction="LONG",
                    contracts=1, strike=110, right="C", expiry="20260501",
                    leg_role="long_call", underlying=signal.ticker),
        ]


class _SingleLegPlugin(BaseStrategy):
    @property
    def name(self): return "single_test"
    @property
    def description(self): return "single-leg test plugin"
    def detect(self, *a, **kw): return []
    # No place_legs override → default returns None


def _bypass_gates(monkeypatch, em):
    """Patch market hours + pre-flight + DB writer to isolate routing."""
    # Bypass market clock
    from strategy import trade_entry_manager as tem_mod
    class _Clock:
        def entries_allowed(self): return True
        def is_past_close(self): return False
        def in_eod_sweep_window(self): return False
        def minutes_until_close(self): return 300
    monkeypatch.setattr("strategy.market_hours.get_market_clock",
                        lambda: _Clock())
    # Stub db writer
    from db import writer as writer_mod
    monkeypatch.setattr(writer_mod, "insert_multi_leg_trade",
                        lambda env, res, account="paper": 99)
    monkeypatch.setattr(writer_mod, "update_thread_status",
                        lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(writer_mod, "add_system_log",
                        lambda *a, **kw: None, raising=False)


def test_trade_entry_manager_routes_to_multi_leg_when_place_legs_returns(monkeypatch):
    client = _FakeClientForEM()
    em = _FakeExitManager()
    _bypass_gates(monkeypatch, em)

    plugin = _MultiLegPlugin()
    mgr = TradeEntryManager(client, em, "SPY",
                             strategy_id=9, strategy_name="multi_test",
                             plugin_instance=plugin)

    signal = Signal(signal_type="X", direction="LONG", entry_price=100.0,
                     sl=99.0, tp=101.0, setup_id="s1", ticker="SPY",
                     strategy_name="multi_test")

    trade = mgr.enter(signal)

    assert len(client.multi_leg_calls) == 1, "must route to place_multi_leg_order"
    assert len(client.bracket_calls) == 0, "must NOT call place_bracket_order"
    assert trade is not None
    assert trade["n_legs"] == 2
    assert trade["db_id"] == 99
    assert trade["oca_group"] == "MULTILEG-1"


def test_entry_manager_routes_to_combo_when_flag_enabled(monkeypatch):
    """ENH-046: when USE_COMBO_ORDERS_FOR_MULTI_LEG=True, the entry
    manager routes to place_combo_order (single IB Bag) instead of
    place_multi_leg_order (N independent orders)."""
    import config
    monkeypatch.setattr(config, "USE_COMBO_ORDERS_FOR_MULTI_LEG", True,
                         raising=False)

    client = _FakeClientForEM()
    em = _FakeExitManager()
    _bypass_gates(monkeypatch, em)

    plugin = _MultiLegPlugin()
    mgr = TradeEntryManager(client, em, "SPY",
                             strategy_id=9, strategy_name="multi_test",
                             plugin_instance=plugin)

    signal = Signal(signal_type="X", direction="LONG", entry_price=100.0,
                     sl=99.0, tp=101.0, setup_id="s1", ticker="SPY",
                     strategy_name="multi_test")

    mgr.enter(signal)

    assert hasattr(client, "combo_calls"), "combo path must be taken"
    assert len(client.combo_calls) == 1, "must call place_combo_order once"
    assert len(client.multi_leg_calls) == 0, "must NOT call place_multi_leg_order when combo flag is on"


def test_entry_manager_default_flag_uses_legacy_multi_leg(monkeypatch):
    """Default behavior (flag unset/False) keeps today's N-order path
    so the combo roll-out is strictly opt-in."""
    import config
    monkeypatch.setattr(config, "USE_COMBO_ORDERS_FOR_MULTI_LEG", False,
                         raising=False)

    client = _FakeClientForEM()
    em = _FakeExitManager()
    _bypass_gates(monkeypatch, em)

    plugin = _MultiLegPlugin()
    mgr = TradeEntryManager(client, em, "SPY",
                             strategy_id=9, strategy_name="multi_test",
                             plugin_instance=plugin)

    signal = Signal(signal_type="X", direction="LONG", entry_price=100.0,
                     sl=99.0, tp=101.0, setup_id="s1", ticker="SPY",
                     strategy_name="multi_test")

    mgr.enter(signal)

    assert len(client.multi_leg_calls) == 1, "legacy path must run when flag is off"
    # combo_calls attr may not exist yet if the path was never taken
    assert not getattr(client, "combo_calls", []), "combo must NOT run by default"


def test_trade_entry_manager_falls_back_to_single_leg_when_place_legs_none(monkeypatch):
    client = _FakeClientForEM()
    em = _FakeExitManager()
    _bypass_gates(monkeypatch, em)

    # Stub the single-leg entry path so we only assert routing
    from strategy import trade_entry_manager as tem_mod
    placed = []
    def _fake_place(self, signal):
        placed.append(signal)
        return {
            "symbol": "SPY260501C00500000", "contracts": 1,
            "entry_price": 1.0, "profit_target": 1.5, "stop_loss": 0.5,
            "entry_time": None, "direction": "LONG",
            "ib_order_id": 1, "ib_perm_id": 2, "ib_con_id": 3,
            "status": "Filled", "fill_price": 1.0,
        }
    monkeypatch.setattr(tem_mod.TradeEntryManager,
                         "_place_order_with_timeout", _fake_place)
    monkeypatch.setattr(tem_mod.TradeEntryManager,
                         "_enrich_trade", lambda self, t, b=None: None)

    plugin = _SingleLegPlugin()
    mgr = TradeEntryManager(client, em, "SPY",
                             strategy_id=9, strategy_name="single_test",
                             plugin_instance=plugin)

    signal = Signal(signal_type="X", direction="LONG", entry_price=100.0,
                     sl=99.0, tp=101.0, setup_id="s1", ticker="SPY",
                     strategy_name="single_test")

    mgr.enter(signal)

    assert len(client.multi_leg_calls) == 0, "must NOT call place_multi_leg_order"
    assert len(placed) == 1, "must use single-leg _place_order_with_timeout path"


# ─── 6. Delta-neutral iron-condor leg spec ────────────────────

def test_delta_neutral_place_legs_returns_4_leg_condor():
    strat = DeltaNeutralStrategy(strike_interval=5.0, wing_width=10.0,
                                  contracts=1, default_expiry="20260501")
    signal = Signal(
        signal_type="DELTA_NEUTRAL_CONDOR", direction="LONG",
        entry_price=503.0, sl=0.0, tp=0.0, setup_id="dn-1",
        ticker="SPY", strategy_name="delta_neutral",
        details={"current_price": 503.0, "strike_interval": 5.0,
                 "wing_width": 10.0, "expiry": "20260501"},
    )

    legs = strat.place_legs(signal)
    assert legs is not None
    assert len(legs) == 4

    roles = [l.leg_role for l in legs]
    assert roles == ["short_call", "long_call", "short_put", "long_put"]

    directions = [l.direction for l in legs]
    assert directions == ["SHORT", "LONG", "SHORT", "LONG"]

    rights = [l.right for l in legs]
    assert rights == ["C", "C", "P", "P"]

    # All legs — same underlying, same expiry, OPT
    for l in legs:
        assert l.underlying == "SPY"
        assert l.expiry == "20260501"
        assert l.sec_type == "OPT"
        assert l.contracts == 1
        assert l.multiplier == 100

    # ATM = round(503/5)*5 = 505. Wings ±10.
    strikes = [l.strike for l in legs]
    assert strikes[0] == 505.0  # short call ATM
    assert strikes[1] == 515.0  # long call wing
    assert strikes[2] == 505.0  # short put ATM
    assert strikes[3] == 495.0  # long put wing
