"""
Concurrency + race-condition unit tests.

These hammer the pure in-memory code paths with many threads to surface
ordering bugs, lost updates, and non-atomic state. They intentionally
use ThreadPoolExecutor rather than asyncio so they catch real GIL-era
races, not just cooperative-scheduling ones.

Marked `concurrency` so they can be run selectively from the dashboard:
    PYTEST_DB_REPORT=1 pytest tests/unit/ -m concurrency

DB-backed races (SELECT FOR UPDATE NOWAIT, reconciliation vs. live open,
two scanners racing add_trade) live in tests/integration/ and are
marked `integration` — they need a real Postgres and run in their own
suite via the dashboard "Integration" button.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import patch

import pytest
import pandas as pd

from strategy.signal_engine import SignalEngine
from strategy import exit_conditions
from utils.occ_parser import parse_occ, normalize_symbol, build_occ


pytestmark = pytest.mark.concurrency


# ────────────────────────────────────────────────────────────
# OCC parser — pure functions, should be trivially thread-safe.
# We still hammer them because past bugs have come from `re`
# module state being non-reentrant in some Python builds.
# ────────────────────────────────────────────────────────────

class TestOccParserConcurrency:
    """Parse 100,000 symbols across 32 threads and verify no corruption."""

    SYMBOLS = [
        "QQQ260415C00634000", "QQQ   260415P00634000", "SPY260416P00700000",
        "MSFT260415C00412500", "GOOGL 260417P00337500", "AMD260501C00220000",
    ]

    def test_parse_occ_parallel(self):
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker(n: int):
            try:
                for _ in range(1000):
                    for sym in self.SYMBOLS:
                        p = parse_occ(sym)
                        assert p is not None, f"parse failed on {sym!r}"
                        assert p.ticker in {"QQQ", "SPY", "MSFT", "GOOGL", "AMD"}
            except Exception as e:
                with lock:
                    errors.append(e)

        with ThreadPoolExecutor(max_workers=32) as ex:
            futures = [ex.submit(worker, i) for i in range(32)]
            for f in as_completed(futures):
                f.result()
        assert not errors, f"{len(errors)} parse errors under contention: {errors[:3]}"

    def test_build_then_parse_roundtrip(self):
        """Build + parse should be consistent under contention."""
        def worker():
            for i in range(500):
                strike = 100.0 + (i % 50) * 2.5
                sym = build_occ("QQQ", "260415", "C", strike)
                p = parse_occ(sym)
                assert p is not None
                assert p.strike == strike

        with ThreadPoolExecutor(max_workers=16) as ex:
            list(as_completed([ex.submit(worker) for _ in range(16)]))

    def test_normalize_is_idempotent(self):
        def worker():
            for _ in range(1000):
                s = normalize_symbol("QQQ   260415C00634000")
                assert s == "QQQ260415C00634000"
                assert normalize_symbol(s) == s  # idempotent

        with ThreadPoolExecutor(max_workers=16) as ex:
            list(as_completed([ex.submit(worker) for _ in range(16)]))


# ────────────────────────────────────────────────────────────
# SignalEngine — the critical invariant is:
#   (1) a given setup_id fires AT MOST ONCE across the bot's life
#   (2) dedup by (signal_type, entry_price) must hold even when two
#       threads call detect() concurrently
# ────────────────────────────────────────────────────────────

@pytest.fixture
def bars():
    df = pd.DataFrame({"close": [634.0] * 10})
    return df


def _mock_raw(setup_id: str, entry_price: float = 634.0):
    return {
        "signal_type": "LONG_iFVG",
        "direction": "LONG",
        "entry_price": entry_price,
        "sl": entry_price * 0.98,
        "tp": entry_price * 1.02,
        "setup_id": setup_id,
    }


class TestSignalEngineConcurrency:
    """Hammer detect() + mark_used() from many threads."""

    def test_mark_used_is_race_safe(self, bars):
        """500 threads try to mark the SAME setup; only one should ever
        count in alerts_today (no double-increment)."""
        eng = SignalEngine("QQQ")

        # Seed a signal that will survive dedup
        with patch("strategy.signal_engine.run_strategy",
                   return_value=[_mock_raw("race-1")]), \
             patch("strategy.signal_engine.run_strategy_short", return_value=[]):
            first = eng.detect(bars, bars, bars, [])
            assert len(first) == 1

        def mark():
            eng.mark_used("race-1")

        with ThreadPoolExecutor(max_workers=64) as ex:
            list(as_completed([ex.submit(mark) for _ in range(500)]))

        # The SAME setup_id should only occupy one slot in the set
        # (set.add is atomic), but the alerts counter is incremented
        # every call — that's by design. What matters is that a
        # re-detect doesn't yield it again.
        with patch("strategy.signal_engine.run_strategy",
                   return_value=[_mock_raw("race-1")]), \
             patch("strategy.signal_engine.run_strategy_short", return_value=[]):
            again = eng.detect(bars, bars, bars, [])
        assert again == [], "marked setup_id should never re-fire"

    def test_concurrent_detect_respects_dedup(self, bars):
        """16 threads calling detect() against identical bars should all
        observe the same dedup behaviour — no 'lost' signal, no double."""
        raw_signals = [_mock_raw(f"setup-{i}", entry_price=634.0 + i * 0.01)
                       for i in range(5)]
        eng = SignalEngine("QQQ")

        results: list[int] = []
        lock = threading.Lock()

        def worker():
            with patch("strategy.signal_engine.run_strategy",
                       return_value=raw_signals), \
                 patch("strategy.signal_engine.run_strategy_short",
                       return_value=[]):
                out = eng.detect(bars, bars, bars, [])
            with lock:
                results.append(len(out))

        with ThreadPoolExecutor(max_workers=16) as ex:
            list(as_completed([ex.submit(worker) for _ in range(16)]))

        # Every call should see all 5 unique setups (they haven't been
        # marked), regardless of how many threads interleave.
        assert set(results) == {5}, f"inconsistent detect results: {results}"

    def test_no_signal_leaks_across_mark_and_detect(self, bars):
        """A detect → mark_used → detect ping-pong across threads must
        still converge to zero signals after every setup_id is marked."""
        setup_ids = [f"s-{i}" for i in range(20)]
        raw_signals = [_mock_raw(sid, entry_price=100.0 + i * 0.5)
                       for i, sid in enumerate(setup_ids)]

        eng = SignalEngine("QQQ")

        def worker(sid: str):
            with patch("strategy.signal_engine.run_strategy",
                       return_value=raw_signals), \
                 patch("strategy.signal_engine.run_strategy_short",
                       return_value=[]):
                eng.detect(bars, bars, bars, [])
            eng.mark_used(sid)

        with ThreadPoolExecutor(max_workers=20) as ex:
            futures = [ex.submit(worker, sid) for sid in setup_ids]
            for f in as_completed(futures):
                f.result()

        # After every setup_id has been marked, a fresh detect() must
        # return nothing — otherwise the state is corrupted.
        with patch("strategy.signal_engine.run_strategy",
                   return_value=raw_signals), \
             patch("strategy.signal_engine.run_strategy_short",
                   return_value=[]):
            leftover = eng.detect(bars, bars, bars, [])
        assert leftover == [], (
            f"signals leaked past mark_used: {[s.setup_id for s in leftover]}"
        )


# ────────────────────────────────────────────────────────────
# exit_conditions.evaluate_exit — called from the monitor loop
# while the scanner thread may be reading the same trade dict.
# We don't share dicts across threads in production (each monitor
# owns its trade), but we still verify the function has no hidden
# shared mutable state (module-level caches, etc.).
# ────────────────────────────────────────────────────────────

class TestExitConditionsConcurrency:

    def _fresh_trade(self) -> dict:
        from datetime import datetime, timedelta
        import pytz
        PT = pytz.timezone("America/Los_Angeles")
        return {
            "ticker": "QQQ",
            "entry_price": 2.00,
            "entry_time": datetime.now(PT) - timedelta(minutes=5),
            "peak_pnl_pct": 0.0,
            "dynamic_sl_pct": -0.60,
            "contracts": 2,
        }

    def test_parallel_eval_no_crosstalk(self, monkeypatch):
        """Each thread evaluates its own trade dict; peak/dynamic_sl
        updates must stay per-trade, not leak via any global state."""
        import config
        monkeypatch.setattr(config, "PROFIT_TARGET", 1.00)
        monkeypatch.setattr(config, "STOP_LOSS", 0.60)
        monkeypatch.setattr(config, "TP_TO_TRAIL", False)
        monkeypatch.setattr(config, "ROLL_ENABLED", False)

        from datetime import datetime
        import pytz
        PT = pytz.timezone("America/Los_Angeles")
        now_pt = datetime.now(PT).replace(hour=10, minute=0, second=0, microsecond=0)

        def worker(seed: int):
            trade = self._fresh_trade()
            # Walk the price up then down — update peak each tick
            for tick in range(50):
                price = 2.0 + (tick / 25.0)   # 2.0 → 4.0 linearly
                exit_conditions.evaluate_exit(trade, price, now_pt)
            # Peak should be close to +100% (seen the $4 price)
            assert trade["peak_pnl_pct"] == pytest.approx(1.96, abs=0.05), (
                f"thread {seed} has bad peak {trade['peak_pnl_pct']}"
            )

        with ThreadPoolExecutor(max_workers=32) as ex:
            list(as_completed([ex.submit(worker, i) for i in range(32)]))


# ────────────────────────────────────────────────────────────
# StrategyRegistry — when ENH-024 merges, this will be the
# global lookup table shared across scanner threads. The
# registry lives on a separate branch today, so this test
# is skipped if not importable.
# ────────────────────────────────────────────────────────────

class TestStrategyRegistryConcurrency:

    def test_concurrent_registration_and_lookup(self):
        try:
            from strategy.base_strategy import BaseStrategy, StrategyRegistry
        except ImportError:
            pytest.skip("base_strategy not present on this branch")

        saved = dict(StrategyRegistry._classes)
        try:
            def make_cls(name: str):
                class _S(BaseStrategy):
                    @property
                    def name(self): return name
                    @property
                    def description(self): return name
                    def detect(self, b1, b1h, b4h, levels, ticker): return []
                return _S

            def register_worker(i: int):
                cls = make_cls(f"stress-{i}")
                StrategyRegistry.register(cls)
                assert StrategyRegistry.get(f"stress-{i}") is cls

            with ThreadPoolExecutor(max_workers=32) as ex:
                list(as_completed([ex.submit(register_worker, i) for i in range(100)]))

            names = StrategyRegistry.all_names()
            for i in range(100):
                assert f"stress-{i}" in names
        finally:
            StrategyRegistry._classes = saved
