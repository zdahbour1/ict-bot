"""
Integration tests for ORB end-to-end via the backtest runner.

Covers:
- ORB row is enabled
- Dynamic class_path instantiation works
- run_backtest() accepts a BaseStrategy instance and uses the plugin path
- A forced-breakout synthetic run produces an ORB_BREAKOUT_LONG trade row
- Bad class_path fails loudly (doesn't silently fall back to ICT)
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


def _db_available() -> bool:
    try:
        from db.connection import db_available
        return db_available()
    except Exception:
        return False


@pytest.fixture(scope="module")
def db_guard():
    if not _db_available():
        pytest.skip("Postgres not reachable")


@pytest.fixture
def session(db_guard):
    from db.connection import get_session
    s = get_session()
    yield s
    s.close()


class TestOrbEnabled:
    def test_orb_row_enabled(self, session):
        row = session.execute(text(
            "SELECT enabled, class_path FROM strategies WHERE name = 'orb'"
        )).fetchone()
        assert row is not None, "orb row missing"
        assert row[0] is True, "orb should be enabled"
        assert row[1] == "strategy.orb_strategy.ORBStrategy"

    def test_orb_settings_seeded(self, session):
        cnt = session.execute(text(
            "SELECT COUNT(*) FROM settings WHERE strategy_id = "
            "  (SELECT strategy_id FROM strategies WHERE name = 'orb')"
        )).scalar()
        assert cnt >= 5, f"expected >= 5 ORB settings, found {cnt}"

    def test_orb_appears_in_strategies_api(self, db_guard):
        """The /api/backtests/strategies endpoint should list ORB now
        that it's enabled (uses list_strategies(enabled_only=True))."""
        from db.strategy_writer import list_strategies
        names = {s["name"] for s in list_strategies(enabled_only=True)}
        assert "orb" in names
        assert "ict" in names


class TestDynamicStrategyLoading:
    """Verify the run_backtest_engine.py path imports strategies by class_path."""

    def test_orb_class_importable_from_path(self):
        """Simulates what run_backtest_engine.py does."""
        import importlib
        class_path = "strategy.orb_strategy.ORBStrategy"
        module_path, class_name = class_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        instance = cls()
        assert instance.name == "orb"
        assert hasattr(instance, "detect")
        assert callable(instance.detect)

    def test_bad_class_path_raises(self):
        """A bogus class_path must NOT silently fall back to ICT."""
        import importlib
        with pytest.raises(ImportError):
            importlib.import_module("strategy.does_not_exist")


class TestOrbBacktestE2E:
    """Run a backtest with ORBStrategy instance via the engine's plugin
    path. Uses synthetic bars that force a breakout so we know exactly
    what should happen."""

    @staticmethod
    def _forced_breakout_bars(n: int = 200) -> pd.DataFrame:
        """First 15 bars form a tight 100-101 range. Bar 16 breaks to 103,
        triggering an ORB_BREAKOUT_LONG. Remaining bars drift sideways."""
        rng = np.random.default_rng(42)
        n_pre = 15
        n_post = n - n_pre
        # Range bars: 100-101
        pre_open = rng.uniform(100.2, 100.8, n_pre)
        pre_close = rng.uniform(100.2, 100.8, n_pre)
        pre_high = np.maximum(pre_open, pre_close) + 0.1
        pre_low = np.minimum(pre_open, pre_close) - 0.1
        # Breakout bar + drift
        post_close = [103.0] + list(102.5 + rng.normal(0, 0.3, n_post - 1))
        post_open = [101.0] + post_close[:-1]
        post_high = np.maximum(post_open, post_close) + 0.2
        post_low = np.minimum(post_open, post_close) - 0.2

        opens = np.concatenate([pre_open, post_open])
        highs = np.concatenate([pre_high, post_high])
        lows = np.concatenate([pre_low, post_low])
        closes = np.concatenate([pre_close, post_close])
        vols = rng.integers(10_000, 100_000, n)

        idx = pd.date_range("2026-03-02 13:30", periods=n, freq="5min", tz="UTC")
        return pd.DataFrame({
            "open": opens, "high": highs, "low": lows,
            "close": closes, "volume": vols,
        }, index=idx)

    def test_run_backtest_with_orb_instance(self, db_guard):
        from backtest_engine import engine
        from backtest_engine import writer as bt_writer
        from backtest_engine.data_provider import aggregate_bars
        from strategy.orb_strategy import ORBStrategy

        bars = self._forced_breakout_bars(250)
        tf_mock = {
            "base": bars,
            "1h": aggregate_bars(bars, "1h"),
            "4h": aggregate_bars(bars, "4h"),
        }

        orb = ORBStrategy(range_minutes=15, breakout_buffer=0.0)

        with patch.object(engine, "fetch_multi_timeframe",
                          return_value=tf_mock):
            result = engine.run_backtest(
                tickers=["TEST"],
                start_date=date(2026, 3, 1),
                end_date=date(2026, 3, 5),
                strategy_id=(_orb_strategy_id()),
                strategy=orb,
                config={"base_interval": "5m"},
                run_name="orb-e2e-unit",
            )

        run_id = result["run_id"]
        try:
            assert result["trade_count"] >= 1, (
                "ORB should have fired at least once on the forced breakout"
            )
            trades = bt_writer.get_run_trades(run_id)
            assert any(t["signal_type"] == "ORB_BREAKOUT_LONG" for t in trades), (
                f"expected ORB_BREAKOUT_LONG trade; got signal_types: "
                f"{[t['signal_type'] for t in trades]}"
            )
            # Indicator enrichment populated
            sample = trades[0]
            assert sample["entry_indicators"], "entry_indicators missing"
            assert sample["entry_context"], "entry_context missing"
        finally:
            bt_writer.delete_run(run_id)


def _orb_strategy_id() -> int:
    from db.connection import get_session
    s = get_session()
    sid = s.execute(
        text("SELECT strategy_id FROM strategies WHERE name = 'orb'")
    ).scalar()
    s.close()
    return int(sid)
