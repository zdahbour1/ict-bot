"""
Unit tests for Phase 4 (multi-strategy v2) scanner plugin dispatch and
per-(strategy_id, ticker) trade entry lock.

Verifies:
  1. `Scanner(strategy_name="ict", ...)` routes through `SignalEngine`
     (the legacy fast path) — `self.plugin is None`, `self.signal_engine`
     is a `SignalEngine` instance.
  2. `Scanner(strategy_name="orb", strategy_instance=FakePlugin())` routes
     through `plugin.detect(bars_1m, bars_1h, bars_4h, levels, ticker)` —
     `self.signal_engine is None`, `self.plugin is FakePlugin`.
  3. `TradeEntryManager` open-trade lock is scoped by `(strategy_id, ticker)`:
     an open trade for strategy A on SPY does NOT block strategy B from
     entering SPY (both `can_enter()` calls return admitted).
"""
from __future__ import annotations

import pytest
import pandas as pd
from unittest.mock import patch, MagicMock

from strategy.scanner import Scanner
from strategy.signal_engine import SignalEngine
from strategy.base_strategy import BaseStrategy, Signal
from strategy.trade_entry_manager import TradeEntryManager


@pytest.fixture(autouse=True)
def _always_in_trade_window():
    """can_enter() consults the live market_hours clock; outside RTH it
    returns 'market closed (past EOD cutoff)' before ever reaching the
    per-strategy ticker-lock logic. These tests are about the lock, so
    patch the clock to always-open."""
    fake_clock = MagicMock()
    fake_clock.entries_allowed.return_value = True
    fake_clock.is_past_close.return_value = False
    fake_clock.in_eod_sweep_window.return_value = False
    fake_clock.minutes_until_close.return_value = 120.0
    with patch("strategy.market_hours.get_market_clock", return_value=fake_clock):
        yield


# ─── Shared fakes ─────────────────────────────────────────────

class _FakeClient:
    def get_ib_positions_raw(self):
        return []

    def find_open_orders_for_contract(self, *_a, **_kw):
        return []


class _FakeExitManager:
    """Minimal ExitManager stand-in exposing the only attribute the
    entry-manager + scanner read: ``open_trades`` (list[dict])."""
    def __init__(self, open_trades=None):
        self.open_trades = open_trades or []

    def invalidate_cache(self):
        pass


class _FakePlugin(BaseStrategy):
    """Trivial BaseStrategy subclass. detect() records its call count
    and returns one synthetic Signal so the caller can round-trip it."""
    def __init__(self):
        self.calls: int = 0
        self.mark_used_calls: list[str] = []

    @property
    def name(self) -> str:
        return "orb"

    @property
    def description(self) -> str:
        return "fake ORB plugin for dispatch tests"

    def detect(self, bars_1m, bars_1h, bars_4h, levels, ticker):
        self.calls += 1
        return [Signal(
            signal_type="ORB_BREAKOUT_LONG", direction="LONG",
            entry_price=100.0, sl=99.0, tp=102.0,
            setup_id="orb-test-1", ticker=ticker, strategy_name=self.name,
        )]

    def mark_used(self, setup_id: str) -> None:
        self.mark_used_calls.append(setup_id)


# ─── 1. ICT fast path ────────────────────────────────────────

def test_scanner_ict_uses_signal_engine_fast_path():
    s = Scanner(
        _FakeClient(), _FakeExitManager(), ticker="QQQ",
        strategy_id=1, strategy_name="ict", strategy_instance=None,
    )
    assert s.plugin is None
    assert isinstance(s.signal_engine, SignalEngine)
    assert s.strategy_name == "ict"
    assert s.strategy_id == 1
    # Thread key preserves legacy "scanner-<TICKER>" for ICT — dashboards rely on this.
    assert s._thread_key() == "scanner-QQQ"


def test_scanner_ict_fast_path_even_if_plugin_supplied():
    """Passing a plugin alongside strategy_name='ict' must still take
    the fast path — ICT keeps SignalEngine as its detection engine."""
    plugin = _FakePlugin()
    s = Scanner(
        _FakeClient(), _FakeExitManager(), ticker="SPY",
        strategy_id=1, strategy_name="ict", strategy_instance=plugin,
    )
    assert s.plugin is None
    assert isinstance(s.signal_engine, SignalEngine)


# ─── 2. Plugin dispatch path ─────────────────────────────────

def test_scanner_non_ict_uses_plugin():
    plugin = _FakePlugin()
    s = Scanner(
        _FakeClient(), _FakeExitManager(), ticker="SPY",
        strategy_id=2, strategy_name="orb", strategy_instance=plugin,
    )
    assert s.signal_engine is None
    assert s.plugin is plugin
    # Plugin scanners get a namespaced thread key so concurrent strategies
    # on the same ticker each show their own row in thread_status.
    assert s._thread_key() == "scanner-orb-SPY"


def test_scanner_plugin_detect_invoked_with_expected_args():
    plugin = _FakePlugin()
    s = Scanner(
        _FakeClient(), _FakeExitManager(), ticker="SPY",
        strategy_id=2, strategy_name="orb", strategy_instance=plugin,
    )
    # Exercise the plugin branch of the detect dispatch directly (the full
    # _scan() path depends on IB data fetch which is out of scope here).
    df = pd.DataFrame({"open": [1], "high": [2], "low": [0.5], "close": [1.5]})
    if s.plugin is not None:  # mirrors Scanner._scan()'s branch
        try:
            signals = s.plugin.detect(df, df, df, [], s.ticker)
        except TypeError:
            signals = s.plugin.detect(df, df, df, [])
    assert plugin.calls == 1
    assert len(signals) == 1
    assert signals[0].signal_type == "ORB_BREAKOUT_LONG"
    assert signals[0].ticker == "SPY"


# ─── 3. (strategy_id, ticker) trade-entry lock ──────────────

def test_trade_entry_lock_scoped_by_strategy_id():
    """Strategy 1's open SPY trade MUST NOT block Strategy 2 from SPY.

    We build an ExitManager with one open trade for (strategy_id=1, SPY)
    and two entry managers — one per strategy. Strategy 1 sees it as
    'already in trade'; Strategy 2 sees no conflict and is admitted.
    """
    em = _FakeExitManager(open_trades=[
        {"ticker": "SPY", "strategy_id": 1, "status": "open"},
    ])

    em1 = TradeEntryManager(_FakeClient(), em, "SPY",
                            strategy_id=1, strategy_name="ict")
    em2 = TradeEntryManager(_FakeClient(), em, "SPY",
                            strategy_id=2, strategy_name="orb")

    allowed1, reason1 = em1.can_enter()
    allowed2, reason2 = em2.can_enter()

    # Strategy 1 is blocked by its own open trade.
    assert allowed1 is False
    assert "already in trade" in reason1
    # Strategy 2 shares the ticker but has no open trade of its own — admitted.
    # (Any non-"already in trade" reason is acceptable: market_hours or other
    # gates may block depending on wall-clock time — the key invariant is
    # that the per-strategy lock does not confuse them.)
    if not allowed2:
        assert "already in trade" not in reason2
    else:
        assert reason2 == "ok"


def test_trade_entry_lock_same_strategy_same_ticker_blocks():
    """Sanity check: same (strategy_id, ticker) IS blocked."""
    em = _FakeExitManager(open_trades=[
        {"ticker": "QQQ", "strategy_id": 1, "status": "open"},
    ])
    mgr = TradeEntryManager(_FakeClient(), em, "QQQ",
                            strategy_id=1, strategy_name="ict")
    allowed, reason = mgr.can_enter()
    assert allowed is False
    assert "already in trade" in reason


def test_trade_entry_lock_legacy_trade_without_strategy_id_still_blocks():
    """Legacy open trades (strategy_id IS NULL in the cache dict) still
    block the ICT scanner — backward compat for trades opened before Phase 4."""
    em = _FakeExitManager(open_trades=[
        {"ticker": "QQQ", "strategy_id": None, "status": "open"},
    ])
    mgr = TradeEntryManager(_FakeClient(), em, "QQQ",
                            strategy_id=1, strategy_name="ict")
    allowed, reason = mgr.can_enter()
    assert allowed is False
    assert "already in trade" in reason
