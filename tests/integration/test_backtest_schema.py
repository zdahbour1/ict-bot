"""
Integration tests for backtest_engine.writer + backtest_runs/backtest_trades DDL.

Runs against a real Postgres. Set:
    DATABASE_URL=postgresql://ict_bot:ict_bot_dev@localhost:5432/ict_bot
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

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
        pytest.skip("Postgres not reachable — skipping backtest schema tests")


@pytest.fixture
def session(db_guard):
    from db.connection import get_session
    s = get_session()
    yield s
    s.close()


# ── Schema shape ─────────────────────────────────────────

class TestSchemaShape:
    def test_backtest_runs_exists(self, session):
        row = session.execute(text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name = 'backtest_runs'"
        )).scalar()
        assert row == 1

    def test_backtest_trades_exists(self, session):
        row = session.execute(text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name = 'backtest_trades'"
        )).scalar()
        assert row == 1

    def test_backtest_runs_has_strategy_id_fk(self, session):
        row = session.execute(text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name='backtest_runs' AND column_name='strategy_id' "
            "  AND is_nullable='NO'"
        )).scalar()
        assert row == 1

    def test_backtest_trades_has_strategy_id_fk(self, session):
        row = session.execute(text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name='backtest_trades' AND column_name='strategy_id' "
            "  AND is_nullable='NO'"
        )).scalar()
        assert row == 1

    def test_invalid_strategy_id_rejected(self, session):
        """FK enforcement — a run pointing at a non-existent strategy fails."""
        with pytest.raises(IntegrityError):
            session.execute(text(
                "INSERT INTO backtest_runs "
                "  (strategy_id, tickers, start_date, end_date, config) "
                "VALUES (999999, ARRAY['QQQ'], '2026-01-01', '2026-01-02', '{}'::jsonb)"
            ))
            session.commit()
        session.rollback()


# ── Writer round-trip ────────────────────────────────────

class TestBacktestWriter:
    def test_create_and_finalize_run(self, db_guard):
        from backtest_engine.writer import (
            create_run, mark_run_started, finalize_run, delete_run
        )
        from backtest_engine.metrics import BacktestSummary

        run_id = create_run(
            name="writer-test",
            strategy_id=1,
            tickers=["QQQ", "SPY"],
            start_date=date(2026, 3, 1),
            end_date=date(2026, 3, 5),
            config={"profit_target": 1.0, "stop_loss": 0.6},
        )
        assert run_id is not None

        try:
            mark_run_started(run_id)

            summary = BacktestSummary(
                total_trades=10, wins=6, losses=4,
                total_pnl=500.0, win_rate=60.0,
                avg_win=150.0, avg_loss=-100.0,
                max_drawdown=-200.0, sharpe_ratio=1.25,
                profit_factor=1.8, avg_hold_min=42.5,
                max_win_streak=3, max_loss_streak=2,
            )
            finalize_run(run_id, summary)

            # Verify shape
            from db.connection import get_session
            s = get_session()
            row = s.execute(text(
                "SELECT status, total_trades, wins, win_rate, sharpe_ratio "
                "FROM backtest_runs WHERE id = :id"
            ), {"id": run_id}).fetchone()
            s.close()
            assert row[0] == "completed"
            assert row[1] == 10
            assert row[2] == 6
            assert float(row[3]) == 60.0
            assert float(row[4]) == 1.25
        finally:
            delete_run(run_id)

    def test_record_trade_roundtrip(self, db_guard):
        from backtest_engine.writer import (
            create_run, record_trade, get_run_trades, delete_run
        )

        run_id = create_run(
            name="trade-rt",
            strategy_id=1,
            tickers=["QQQ"],
            start_date=date(2026, 3, 1),
            end_date=date(2026, 3, 1),
            config={},
        )
        assert run_id is not None

        try:
            tid = record_trade(run_id, 1, {
                "ticker": "QQQ",
                "symbol": "QQQ260301C00600000",
                "direction": "LONG",
                "contracts": 2,
                "entry_price": 2.40,
                "exit_price": 4.80,
                "pnl_pct": 1.0, "pnl_usd": 480.0,
                "peak_pnl_pct": 1.1,
                "entry_time": datetime(2026, 3, 1, 14, 30, tzinfo=timezone.utc),
                "exit_time": datetime(2026, 3, 1, 15, 15, tzinfo=timezone.utc),
                "hold_minutes": 45,
                "signal_type": "LONG_iFVG",
                "exit_reason": "TP", "exit_result": "WIN",
                "tp_level": 4.80, "sl_level": 0.96,
                "entry_indicators": {"rsi": 42.0, "vwap": 600.5, "atr": 1.2},
                "entry_context": {"day_of_week": "Monday", "session_phase": "open"},
                "signal_details": {"raid": {"high": 601.0}},
            })
            assert tid is not None

            trades = get_run_trades(run_id)
            assert len(trades) == 1
            t = trades[0]
            assert t["ticker"] == "QQQ"
            assert t["exit_result"] == "WIN"
            assert t["entry_indicators"]["rsi"] == 42.0
            assert t["entry_context"]["day_of_week"] == "Monday"
            assert t["signal_details"]["raid"]["high"] == 601.0
        finally:
            delete_run(run_id)

    def test_cascade_delete(self, db_guard):
        """Deleting a run must cascade-delete its trades."""
        from backtest_engine.writer import create_run, record_trade, delete_run
        from db.connection import get_session

        run_id = create_run(
            name="cascade-test",
            strategy_id=1,
            tickers=["X"],
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 1),
            config={},
        )
        assert run_id is not None

        record_trade(run_id, 1, {
            "ticker": "X", "symbol": "X260101C00100000", "direction": "LONG",
            "contracts": 1, "entry_price": 1.0,
            "entry_time": datetime(2026, 1, 1, tzinfo=timezone.utc),
        })

        delete_run(run_id)

        s = get_session()
        cnt = s.execute(text(
            "SELECT COUNT(*) FROM backtest_trades WHERE run_id = :id"
        ), {"id": run_id}).scalar()
        s.close()
        assert cnt == 0

    def test_list_runs_filters_by_strategy(self, db_guard):
        from backtest_engine.writer import create_run, list_runs, delete_run

        run_id = create_run(
            name="list-filter-test",
            strategy_id=1,
            tickers=["QQQ"],
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 1),
            config={},
        )
        try:
            runs = list_runs(strategy_id=1)
            assert any(r["id"] == run_id for r in runs)
            runs_other = list_runs(strategy_id=9999)
            assert not any(r["id"] == run_id for r in runs_other)
        finally:
            delete_run(run_id)
