"""
Integration tests for db/roadmap_schema.sql — forward-compatible columns
for futures options + multi-security-type support + per-trade strategy
config snapshots + pre-seeded strategy rows.

See docs/roadmap_schema_extensions.md for the principle.

Runs against a real Postgres. Set:
    DATABASE_URL=postgresql://ict_bot:ict_bot_dev@localhost:5432/ict_bot
"""
from __future__ import annotations

from datetime import datetime, timezone

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
        pytest.skip("Postgres not reachable — skipping roadmap schema tests")


@pytest.fixture
def session(db_guard):
    from db.connection import get_session
    s = get_session()
    yield s
    s.close()


# ── Schema shape ─────────────────────────────────────────

class TestSchemaShape:
    """Every new column exists on the right table with the right
    nullability and default."""

    EXPECTED_DEFAULTS = {
        "sec_type": "'OPT'::character varying",
        "multiplier": "100",
        "exchange": "'SMART'::character varying",
        "currency": "'USD'::character varying",
    }

    @pytest.mark.parametrize("table", ["trades", "backtest_trades", "tickers"])
    @pytest.mark.parametrize("column,is_nullable", [
        ("sec_type", "NO"),
        ("multiplier", "NO"),
        ("exchange", "NO"),
        ("currency", "NO"),
    ])
    def test_column_exists_not_null(self, session, table, column, is_nullable):
        row = session.execute(text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ), {"t": table, "c": column}).fetchone()
        assert row is not None, f"{table}.{column} missing"
        assert row[0] == is_nullable

    @pytest.mark.parametrize("table", ["trades", "backtest_trades"])
    def test_underlying_nullable(self, session, table):
        row = session.execute(text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = 'underlying'"
        ), {"t": table}).fetchone()
        assert row is not None
        assert row[0] == "YES"

    @pytest.mark.parametrize("table", ["trades", "backtest_trades"])
    def test_strategy_config_jsonb_not_null(self, session, table):
        row = session.execute(text(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = 'strategy_config'"
        ), {"t": table}).fetchone()
        assert row is not None
        assert row[0] == "jsonb"
        assert row[1] == "NO"


# ── Pre-seeded strategies ────────────────────────────────

class TestStrategySeeds:
    def test_all_four_strategies_present(self, session):
        rows = session.execute(text(
            "SELECT name, enabled, is_default FROM strategies "
            "WHERE name IN ('ict', 'orb', 'vwap_revert', 'delta_neutral') "
            "ORDER BY name"
        )).fetchall()
        names = {r[0] for r in rows}
        assert names == {"ict", "orb", "vwap_revert", "delta_neutral"}

    def test_ict_is_always_enabled(self, session):
        """ICT must stay enabled regardless of what other strategies get
        flipped on in subsequent feature branches (orb, vwap, etc.).
        Originally this test asserted 'only ict' but as new strategies
        come online the invariant shifts to 'ict is always present'."""
        enabled = session.execute(text(
            "SELECT name FROM strategies WHERE enabled = TRUE ORDER BY name"
        )).fetchall()
        names = {r[0] for r in enabled}
        assert "ict" in names, f"ict must be enabled; got {names}"

    def test_only_ict_is_default(self, session):
        cnt = session.execute(text(
            "SELECT COUNT(*) FROM strategies WHERE is_default = TRUE"
        )).scalar()
        assert cnt == 1

    def test_class_paths_point_to_real_modules(self, session):
        """The class_path column should at least be the dotted-path format
        we expect (sanity check, not an import check — ORB exists on this
        branch, VWAP/delta-neutral don't yet)."""
        paths = session.execute(text(
            "SELECT name, class_path FROM strategies WHERE name <> 'ict'"
        )).fetchall()
        for name, path in paths:
            assert "." in path, f"{name}: class_path '{path}' not dotted"
            assert path.startswith("strategy."), f"{name}: should start with 'strategy.'"


# ── Default values on new trade rows ─────────────────────

class TestInsertTradeDefaults:
    """Inserts via the normal insert_trade path must land with the right
    defaults so existing code keeps behaving identically."""

    def test_defaults_applied_when_not_specified(self, db_guard):
        from db.writer import insert_trade, invalidate_active_strategy_cache
        from db.connection import get_session

        invalidate_active_strategy_cache()
        session = get_session()
        try:
            trade_id = insert_trade({
                "ticker": "RSCHKDEF",
                "symbol": "RSCHKDEF260415C00100000",
                "direction": "LONG",
                "contracts": 2,
                "entry_price": 1.0,
                "profit_target": 2.0,
                "stop_loss": 0.4,
                "entry_time": datetime.now(timezone.utc),
            }, account="DU0-TEST")
            assert trade_id is not None

            row = session.execute(text(
                "SELECT sec_type, multiplier, exchange, currency, "
                "       underlying, strategy_config "
                "FROM trades WHERE id = :id"
            ), {"id": trade_id}).fetchone()
            assert row[0] == "OPT"
            assert row[1] == 100
            assert row[2] == "SMART"
            assert row[3] == "USD"
            assert row[4] is None
            assert row[5] == {}
        finally:
            session.execute(text("DELETE FROM trades WHERE ticker = 'RSCHKDEF'"))
            session.commit()
            session.close()

    def test_caller_can_override_defaults(self, db_guard):
        """FOP / futures callers pass explicit sec_type etc. and round-trip."""
        from db.writer import insert_trade, invalidate_active_strategy_cache
        from db.connection import get_session

        invalidate_active_strategy_cache()
        session = get_session()
        try:
            trade_id = insert_trade({
                "ticker": "MNQ",
                "symbol": "MNQ  260617C00025000",
                "direction": "LONG",
                "contracts": 1,
                "entry_price": 50.0,
                "profit_target": 100.0,
                "stop_loss": 25.0,
                "entry_time": datetime.now(timezone.utc),
                # FOP-specific fields
                "sec_type": "FOP",
                "multiplier": 20,
                "exchange": "CME",
                "currency": "USD",
                "underlying": "MNQ",
                "strategy_config": {"profit_target": 1.0, "stop_loss": 0.5,
                                    "range_minutes": 15},
            }, account="DU0-TEST")
            assert trade_id is not None

            row = session.execute(text(
                "SELECT sec_type, multiplier, exchange, currency, underlying, "
                "       strategy_config "
                "FROM trades WHERE id = :id"
            ), {"id": trade_id}).fetchone()
            assert row[0] == "FOP"
            assert row[1] == 20
            assert row[2] == "CME"
            assert row[3] == "USD"
            assert row[4] == "MNQ"
            assert row[5]["profit_target"] == 1.0
            assert row[5]["range_minutes"] == 15
        finally:
            session.execute(text("DELETE FROM trades WHERE ticker = 'MNQ'"))
            session.commit()
            session.close()


# ── Existing tickers backfill ────────────────────────────

class TestTickerBackfill:
    def test_all_existing_tickers_opt_default(self, session):
        """Every pre-existing ticker must be OPT / 100 / SMART / USD after
        the migration (the implicit assumption made explicit)."""
        row = session.execute(text(
            "SELECT COUNT(*) FROM tickers "
            "WHERE sec_type <> 'OPT' OR multiplier <> 100 "
            "  OR exchange <> 'SMART' OR currency <> 'USD'"
        )).scalar()
        assert row == 0, (
            f"{row} tickers have non-default values after migration — "
            "existing rows should match the implicit equity-options defaults"
        )


# ── Backtest_trades round-trip ───────────────────────────

class TestBacktestTradesNewColumns:
    def test_backtest_trade_roundtrip_with_fop(self, db_guard):
        from backtest_engine.writer import create_run, record_trade, delete_run
        from db.connection import get_session
        from datetime import date

        run_id = create_run(
            name="roadmap-fop-test",
            strategy_id=1,
            tickers=["MNQ"],
            start_date=date(2026, 3, 1),
            end_date=date(2026, 3, 1),
            config={},
        )
        assert run_id is not None

        try:
            # Use the direct DB path since backtest_engine.writer.record_trade
            # doesn't know about the roadmap fields yet (that's fine — ICT
            # backtests don't need them. But future-options backtests will
            # extend it or populate directly.)
            session = get_session()
            session.execute(text(
                "INSERT INTO backtest_trades ( "
                "  run_id, strategy_id, ticker, direction, contracts, "
                "  entry_price, entry_time, "
                "  sec_type, multiplier, exchange, currency, underlying, "
                "  strategy_config "
                ") VALUES ( "
                "  :rid, 1, 'MNQ', 'LONG', 1, 50.0, NOW(), "
                "  'FOP', 20, 'CME', 'USD', 'MNQ', "
                "  CAST(:cfg AS jsonb) "
                ")"
            ), {"rid": run_id, "cfg": '{"base_interval": "15m"}'})
            session.commit()

            row = session.execute(text(
                "SELECT sec_type, multiplier, exchange, underlying, strategy_config "
                "FROM backtest_trades WHERE run_id = :rid"
            ), {"rid": run_id}).fetchone()
            session.close()

            assert row[0] == "FOP"
            assert row[1] == 20
            assert row[2] == "CME"
            assert row[3] == "MNQ"
            assert row[4]["base_interval"] == "15m"
        finally:
            delete_run(run_id)
