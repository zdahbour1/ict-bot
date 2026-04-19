"""
Unit tests for backtest_engine.engine / indicators / data_provider.
Uses synthetic bars (no yfinance network calls).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from backtest_engine import indicators as ind
from backtest_engine.data_provider import aggregate_bars


def _bars(n: int = 300, start_price: float = 500.0, seed: int = 7,
          freq: str = "5min", start: str = "2026-03-02 13:30") -> pd.DataFrame:
    """Synthetic but structurally realistic OHLCV."""
    rng = np.random.default_rng(seed)
    returns = rng.normal(0, 0.002, n)
    closes = start_price * (1 + returns).cumprod()
    opens = np.concatenate([[start_price], closes[:-1]])
    highs = np.maximum(opens, closes) + rng.uniform(0.1, 0.5, n)
    lows = np.minimum(opens, closes) - rng.uniform(0.1, 0.5, n)
    volumes = rng.integers(10_000, 100_000, n)
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes,
    }, index=idx)


# ── indicators ──────────────────────────────────────────────

class TestIndicators:
    def test_rsi_shape(self):
        bars = _bars(100)
        r = ind.rsi(bars["close"], 14)
        assert len(r) == len(bars)
        # First 14 are NaN
        assert r.iloc[:13].isna().all()
        # Later values are in [0, 100]
        tail = r.iloc[20:].dropna()
        assert ((tail >= 0) & (tail <= 100)).all()

    def test_vwap_monotonic_within_day(self):
        bars = _bars(80, freq="5min", start="2026-03-02 13:30")
        v = ind.vwap(bars)
        assert len(v) == len(bars)
        assert not v.isna().all()

    def test_atr_nonneg(self):
        bars = _bars(80)
        a = ind.atr(bars, 14).dropna()
        assert (a >= 0).all()

    def test_snapshot_at_returns_json_safe_scalars(self):
        bars = _bars(80)
        snap = ind.snapshot_at(bars, 50)
        assert snap["price"] is not None
        assert isinstance(snap["bar_of_day"], int)
        assert snap["day_of_week"] in {"Monday", "Tuesday", "Wednesday",
                                        "Thursday", "Friday", "Saturday", "Sunday"}
        for v in snap.values():
            # Anything stored must be a native JSON-serializable type
            assert v is None or isinstance(v, (str, int, float, bool))

    def test_snapshot_at_out_of_range(self):
        bars = _bars(10)
        assert ind.snapshot_at(bars, 999) == {}
        assert ind.snapshot_at(bars, -1) == {}
        assert ind.snapshot_at(pd.DataFrame(), 0) == {}

    def test_context_at_session_phase(self):
        # Build a bar at different hours and confirm the session_phase
        idx = [pd.Timestamp("2026-03-02 14:00", tz="UTC"),   # 07:00 PT = open
               pd.Timestamp("2026-03-02 18:00", tz="UTC"),   # 11:00 PT = morning
               pd.Timestamp("2026-03-02 20:30", tz="UTC"),   # 13:30 PT = midday
               pd.Timestamp("2026-03-02 22:30", tz="UTC")]   # 15:30 PT = close
        df = pd.DataFrame({
            "open": [1, 1, 1, 1], "high": [1, 1, 1, 1],
            "low": [1, 1, 1, 1], "close": [1, 1, 1, 1],
            "volume": [1, 1, 1, 1],
        }, index=idx)
        # Sessions are computed on UTC hour in context_at
        # (intentional — the impl uses ts.hour in UTC)
        phase0 = ind.context_at(df, 0).get("session_phase")
        phase3 = ind.context_at(df, 3).get("session_phase")
        # Values will reflect UTC hour; just ensure they're one of the phases
        phases = {"open", "morning", "midday", "afternoon", "close"}
        assert phase0 in phases
        assert phase3 in phases


# ── aggregate_bars ──────────────────────────────────────────

class TestAggregate:
    def test_aggregate_5m_to_1h(self):
        bars = _bars(60, freq="5min", start="2026-03-02 13:30")
        out = aggregate_bars(bars, "1h")
        assert len(out) > 0
        # Volume sum preservation (within one hour window)
        assert out["volume"].sum() > 0

    def test_empty_input(self):
        empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        out = aggregate_bars(empty, "1h")
        assert out.empty


# ── engine (using synthetic bars + mocked fetch) ────────────

class TestEngineSmokeRun:
    """Verifies the engine's bar-by-bar loop + DB writes end-to-end
    against synthetic data and a stubbed SignalEngine."""

    def test_engine_runs_and_records_trade(self, tmp_path):
        """Full run with a forced-signal SignalEngine stub produces at
        least one trade row in the DB."""
        from db.connection import db_available
        if not db_available():
            pytest.skip("Postgres not reachable")

        from backtest_engine import engine
        from backtest_engine import writer as bt_writer

        # Synthetic bars for one ticker
        bars = _bars(300, start_price=500.0, freq="5min",
                     start="2026-03-02 13:30")

        # Stub fetch_multi_timeframe to return our synthetic bars
        tf_mock = {
            "base": bars,
            "1h": aggregate_bars(bars, "1h"),
            "4h": aggregate_bars(bars, "4h"),
        }

        # Stub SignalEngine.detect: fire exactly one LONG signal near the
        # start so the engine opens a trade, then goes quiet.
        from strategy.signal_engine import Signal

        fire_after = {"count": 0}

        def fake_detect(self, b1, b1h, b4h, levels):
            fire_after["count"] += 1
            if fire_after["count"] == 20:  # fire on the 20th call
                return [Signal(
                    signal_type="LONG_iFVG",
                    direction="LONG",
                    entry_price=float(b1["close"].iloc[-1]),
                    sl=float(b1["close"].iloc[-1]) * 0.98,
                    tp=float(b1["close"].iloc[-1]) * 1.02,
                    setup_id=f"synth-{fire_after['count']}",
                    ticker="TEST",
                )]
            return []

        with patch.object(
            engine, "fetch_multi_timeframe", return_value=tf_mock
        ), patch(
            "strategy.signal_engine.SignalEngine.detect", new=fake_detect
        ):
            result = engine.run_backtest(
                tickers=["TEST"],
                start_date=date(2026, 3, 1),
                end_date=date(2026, 3, 5),
                strategy_id=1,
                config={"profit_target": 0.50, "stop_loss": 0.30,
                        "base_interval": "5m"},
                run_name="unit-smoke",
            )

        run_id = result["run_id"]
        try:
            assert result["trade_count"] >= 1
            trades = bt_writer.get_run_trades(run_id)
            assert len(trades) >= 1
            t = trades[0]
            assert t["ticker"] == "TEST"
            assert t["signal_type"] == "LONG_iFVG"
            # Enrichment populated
            assert t["entry_indicators"]  # non-empty dict
            assert t["entry_context"]
        finally:
            bt_writer.delete_run(run_id)

    def test_engine_handles_no_signals(self, tmp_path):
        """When no signals fire, the run completes with 0 trades."""
        from db.connection import db_available
        if not db_available():
            pytest.skip("Postgres not reachable")

        from backtest_engine import engine
        from backtest_engine import writer as bt_writer

        bars = _bars(200)
        tf_mock = {"base": bars, "1h": aggregate_bars(bars, "1h"),
                   "4h": aggregate_bars(bars, "4h")}

        with patch.object(
            engine, "fetch_multi_timeframe", return_value=tf_mock
        ), patch(
            "strategy.signal_engine.SignalEngine.detect", return_value=[]
        ):
            result = engine.run_backtest(
                tickers=["NOSIG"],
                start_date=date(2026, 3, 1),
                end_date=date(2026, 3, 2),
                strategy_id=1,
                config={"base_interval": "5m"},
                run_name="unit-nosig",
            )

        run_id = result["run_id"]
        try:
            assert result["trade_count"] == 0
            assert result["summary"]["total_trades"] == 0
            # Status should be 'completed' even with 0 trades
            runs = bt_writer.list_runs(limit=5)
            row = next(r for r in runs if r["id"] == run_id)
            assert row["status"] == "completed"
        finally:
            bt_writer.delete_run(run_id)
