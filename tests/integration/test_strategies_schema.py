"""
Integration tests for the active-strategy foundation (ENH-024 rollout #1).

Covers:
- Schema shape: strategies table + FK columns on trades/tickers/settings
- ICT seeded as strategy_id=1, is_default=TRUE
- FK enforcement (invalid strategy_id rejected)
- Partial unique index: only one default strategy allowed
- Per-(key,strategy_id) setting uniqueness with global overlay
- Per-(symbol,strategy_id) ticker uniqueness
- Clone strategy is a single atomic transaction
- ACTIVE_STRATEGY resolution helper returns the right id

Runs against a real Postgres. Set:
    DATABASE_URL=postgresql://ict_bot:ict_bot_dev@localhost:5432/ict_bot
"""
from __future__ import annotations

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
        pytest.skip("Postgres not reachable — skipping strategies schema tests")


@pytest.fixture
def session(db_guard):
    from db.connection import get_session
    s = get_session()
    yield s
    s.close()


# ── Schema shape ─────────────────────────────────────────────

class TestSchemaShape:
    def test_strategies_table_exists(self, session):
        row = session.execute(text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name = 'strategies'"
        )).scalar()
        assert row == 1, "strategies table missing"

    def test_trades_has_strategy_id_column(self, session):
        row = session.execute(text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name = 'trades' AND column_name = 'strategy_id'"
        )).scalar()
        assert row == 1

    def test_tickers_has_strategy_id_column(self, session):
        row = session.execute(text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name = 'tickers' AND column_name = 'strategy_id'"
        )).scalar()
        assert row == 1

    def test_settings_has_strategy_id_column(self, session):
        row = session.execute(text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name = 'settings' AND column_name = 'strategy_id'"
        )).scalar()
        assert row == 1

    def test_settings_strategy_id_is_nullable(self, session):
        """Settings.strategy_id must be NULLable — NULL = global row."""
        row = session.execute(text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'settings' AND column_name = 'strategy_id'"
        )).scalar()
        assert row == "YES"

    def test_trades_strategy_id_is_not_nullable(self, session):
        row = session.execute(text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'trades' AND column_name = 'strategy_id'"
        )).scalar()
        assert row == "NO"


# ── Seed data ────────────────────────────────────────────────

class TestIctSeeded:
    def test_ict_row_exists(self, session):
        row = session.execute(text(
            "SELECT strategy_id, name, display_name, is_default, enabled "
            "FROM strategies WHERE name = 'ict'"
        )).fetchone()
        assert row is not None, "ICT strategy row not seeded"
        assert row[0] == 1, f"ICT should be strategy_id=1, got {row[0]}"
        assert row[3] is True, "ICT should be marked is_default"
        assert row[4] is True, "ICT should be enabled"

    def test_every_trade_has_ict_strategy_id(self, session):
        row = session.execute(text(
            "SELECT COUNT(*) FROM trades WHERE strategy_id IS NULL"
        )).scalar()
        assert row == 0, "some trades still have NULL strategy_id"

    def test_every_ticker_has_ict_strategy_id(self, session):
        row = session.execute(text(
            "SELECT COUNT(*) FROM tickers WHERE strategy_id IS NULL"
        )).scalar()
        assert row == 0

    def test_active_strategy_setting_present(self, session):
        row = session.execute(text(
            "SELECT value FROM settings "
            "WHERE key = 'ACTIVE_STRATEGY' AND strategy_id IS NULL"
        )).fetchone()
        assert row is not None
        assert row[0] == "ict"


# ── FK enforcement ──────────────────────────────────────────

class TestForeignKeyEnforcement:
    def test_invalid_strategy_id_on_insert_trade(self, session):
        """Inserting a trade with a non-existent strategy_id must fail."""
        with pytest.raises(IntegrityError):
            session.execute(text(
                "INSERT INTO trades "
                "  (account, ticker, symbol, direction, "
                "   contracts_entered, contracts_open, "
                "   entry_price, profit_target, stop_loss_level, "
                "   entry_time, status, strategy_id) "
                "VALUES ('DU0-TEST', 'FK_TEST', 'FK_TEST260101C00100000', 'LONG', "
                "        2, 2, 1.0, 2.0, 0.4, NOW(), 'open', 999999)"
            ))
            session.commit()
        session.rollback()

    def test_only_one_default_strategy(self, session):
        """Partial unique index must reject a second is_default=TRUE row."""
        # Create a second strategy marked default — should fail
        with pytest.raises(IntegrityError):
            session.execute(text(
                "INSERT INTO strategies (name, display_name, class_path, is_default) "
                "VALUES ('dup-default-test', 'dup', 'x.y', TRUE)"
            ))
            session.commit()
        session.rollback()


# ── Uniqueness: ticker per strategy ──────────────────────────

class TestTickerUniquenessPerStrategy:
    def test_same_symbol_different_strategy_allowed(self, session):
        """QQQ can exist under ICT AND under another strategy."""
        # Create a throwaway strategy
        new_id = session.execute(text(
            "INSERT INTO strategies (name, display_name, class_path) "
            "VALUES ('ticker-uniq-test', 'test', 'x.y') "
            "RETURNING strategy_id"
        )).scalar()

        try:
            # QQQ under strategy_id=1 already exists; insert QQQ under the new one
            session.execute(text(
                "INSERT INTO tickers (symbol, strategy_id) "
                "VALUES ('QQQ', :sid)"
            ), {"sid": new_id})
            session.commit()

            # Both rows should be visible
            count = session.execute(text(
                "SELECT COUNT(*) FROM tickers WHERE symbol = 'QQQ'"
            )).scalar()
            assert count >= 2
        finally:
            # Cleanup
            session.execute(text(
                "DELETE FROM tickers WHERE strategy_id = :sid"
            ), {"sid": new_id})
            session.execute(text(
                "DELETE FROM strategies WHERE strategy_id = :sid"
            ), {"sid": new_id})
            session.commit()

    def test_same_symbol_same_strategy_rejected(self, session):
        """QQQ cannot be duplicated within the SAME strategy."""
        with pytest.raises(IntegrityError):
            session.execute(text(
                "INSERT INTO tickers (symbol, strategy_id) VALUES ('QQQ', 1)"
            ))
            session.commit()
        session.rollback()


# ── Settings overlay ─────────────────────────────────────────

class TestSettingsOverlay:
    def test_strategy_override_coexists_with_global(self, session):
        """A strategy-scoped PROFIT_TARGET and a global PROFIT_TARGET
        can both exist simultaneously (one row per strategy_id)."""
        # ICT should have PROFIT_TARGET with strategy_id=1 already
        row = session.execute(text(
            "SELECT value FROM settings "
            "WHERE key = 'PROFIT_TARGET' AND strategy_id = 1"
        )).fetchone()
        assert row is not None, "ICT PROFIT_TARGET not classified during migration"

        # Insert a global PROFIT_TARGET (strategy_id IS NULL) — should be allowed
        session.execute(text(
            "INSERT INTO settings "
            "  (category, key, value, data_type, description, strategy_id) "
            "VALUES ('strategy', 'PROFIT_TARGET', '0.99', 'float', 'global fallback', NULL) "
            "ON CONFLICT (key, strategy_id) DO NOTHING"
        ))
        session.commit()

        try:
            count = session.execute(text(
                "SELECT COUNT(*) FROM settings WHERE key = 'PROFIT_TARGET'"
            )).scalar()
            # One for ICT, one global — at minimum 2
            assert count >= 2
        finally:
            session.execute(text(
                "DELETE FROM settings "
                "WHERE key = 'PROFIT_TARGET' AND strategy_id IS NULL"
            ))
            session.commit()


# ── strategy_writer helpers ──────────────────────────────────

class TestStrategyWriter:
    def test_get_active_strategy_id_returns_ict(self):
        from db.strategy_writer import get_active_strategy_id
        assert get_active_strategy_id() == 1

    def test_get_default_strategy_id_returns_ict(self):
        from db.strategy_writer import get_default_strategy_id
        assert get_default_strategy_id() == 1

    def test_list_strategies_includes_ict(self):
        from db.strategy_writer import list_strategies
        rows = list_strategies()
        assert any(r["name"] == "ict" for r in rows)

    def test_clone_strategy_from_ict(self):
        """Cloning ICT must produce a new strategy with copied tickers + settings."""
        from db.strategy_writer import create_strategy_from_source
        from db.connection import get_session

        new_id = create_strategy_from_source(
            new_name="clone-test-strategy",
            display_name="Clone Test",
            class_path="strategy.ict_strategy.ICTStrategy",
            source_strategy_id=1,
            description="integration test",
        )
        assert new_id is not None and new_id != 1

        session = get_session()
        try:
            # New strategy has the same ticker count as ICT
            ict_tickers = session.execute(text(
                "SELECT COUNT(*) FROM tickers WHERE strategy_id = 1"
            )).scalar()
            new_tickers = session.execute(text(
                "SELECT COUNT(*) FROM tickers WHERE strategy_id = :nid"
            ), {"nid": new_id}).scalar()
            assert new_tickers == ict_tickers > 0

            # New strategy has the same strategy-scoped settings as ICT
            ict_settings = session.execute(text(
                "SELECT COUNT(*) FROM settings WHERE strategy_id = 1"
            )).scalar()
            new_settings = session.execute(text(
                "SELECT COUNT(*) FROM settings WHERE strategy_id = :nid"
            ), {"nid": new_id}).scalar()
            assert new_settings == ict_settings > 0
        finally:
            # Cleanup (CASCADE removes children via FKs? No — strategies has no
            # ON DELETE CASCADE by design, so we must clean up manually)
            session.execute(text(
                "DELETE FROM tickers WHERE strategy_id = :nid"
            ), {"nid": new_id})
            session.execute(text(
                "DELETE FROM settings WHERE strategy_id = :nid"
            ), {"nid": new_id})
            session.execute(text(
                "DELETE FROM strategies WHERE strategy_id = :nid"
            ), {"nid": new_id})
            session.commit()
            session.close()

    def test_clone_from_nonexistent_source_returns_none(self):
        from db.strategy_writer import create_strategy_from_source
        result = create_strategy_from_source(
            new_name="should-not-exist",
            display_name="nope",
            class_path="x.y",
            source_strategy_id=999999,
        )
        assert result is None

    def test_set_active_strategy_updates_setting(self):
        """Switching the active strategy updates the global ACTIVE_STRATEGY row
        without creating a duplicate."""
        from db.strategy_writer import set_active_strategy
        from db.connection import get_session

        # Flip to a throwaway strategy, then back
        session = get_session()
        new_id = session.execute(text(
            "INSERT INTO strategies (name, display_name, class_path) "
            "VALUES ('active-swap-test', 'swap test', 'x.y') "
            "RETURNING strategy_id"
        )).scalar()
        session.commit()

        try:
            assert set_active_strategy("active-swap-test") is True

            count = session.execute(text(
                "SELECT COUNT(*) FROM settings WHERE key = 'ACTIVE_STRATEGY'"
            )).scalar()
            assert count == 1, "ACTIVE_STRATEGY should be a singleton"

            val = session.execute(text(
                "SELECT value FROM settings WHERE key = 'ACTIVE_STRATEGY'"
            )).scalar()
            assert val == "active-swap-test"

            # Flip back
            assert set_active_strategy("ict") is True
            val = session.execute(text(
                "SELECT value FROM settings WHERE key = 'ACTIVE_STRATEGY'"
            )).scalar()
            assert val == "ict"
        finally:
            session.execute(text(
                "DELETE FROM strategies WHERE strategy_id = :nid"
            ), {"nid": new_id})
            session.commit()
            session.close()

    def test_insert_trade_auto_stamps_active_strategy(self):
        """insert_trade() must populate strategy_id from the active strategy
        when the caller doesn't provide one."""
        from db.writer import insert_trade, invalidate_active_strategy_cache
        from db.connection import get_session
        from datetime import datetime, timezone

        invalidate_active_strategy_cache()

        session = get_session()
        try:
            trade_id = insert_trade({
                "ticker": "AUTOSID",
                "symbol": "AUTOSID260415C00100000",
                "direction": "LONG",
                "contracts": 2,
                "entry_price": 1.0,
                "profit_target": 2.0,
                "stop_loss": 0.4,
                "entry_time": datetime.now(timezone.utc),
            }, account="DU0-TEST")
            assert trade_id is not None

            row = session.execute(text(
                "SELECT strategy_id FROM trades WHERE id = :id"
            ), {"id": trade_id}).fetchone()
            assert row[0] == 1, f"auto-stamped strategy_id should be 1 (ICT), got {row[0]}"
        finally:
            session.execute(text(
                "DELETE FROM trades WHERE ticker = 'AUTOSID'"
            ))
            session.commit()
            session.close()

    def test_insert_trade_honors_explicit_strategy_id(self):
        """If the caller passes strategy_id on the trade dict, insert_trade uses it."""
        from db.writer import insert_trade, invalidate_active_strategy_cache
        from db.connection import get_session
        from datetime import datetime, timezone

        invalidate_active_strategy_cache()

        session = get_session()
        # Create a throwaway strategy so we can stamp against it
        new_id = session.execute(text(
            "INSERT INTO strategies (name, display_name, class_path) "
            "VALUES ('explicit-sid-test', 'explicit', 'x.y') "
            "RETURNING strategy_id"
        )).scalar()
        session.commit()
        session.close()

        try:
            trade_id = insert_trade({
                "ticker": "EXPLSID",
                "symbol": "EXPLSID260415C00100000",
                "direction": "LONG",
                "contracts": 2,
                "entry_price": 1.0,
                "profit_target": 2.0,
                "stop_loss": 0.4,
                "entry_time": datetime.now(timezone.utc),
                "strategy_id": new_id,
            }, account="DU0-TEST")
            assert trade_id is not None

            session = get_session()
            row = session.execute(text(
                "SELECT strategy_id FROM trades WHERE id = :id"
            ), {"id": trade_id}).fetchone()
            assert row[0] == new_id
        finally:
            session = get_session()
            session.execute(text("DELETE FROM trades WHERE ticker = 'EXPLSID'"))
            session.execute(text(
                "DELETE FROM strategies WHERE strategy_id = :nid"
            ), {"nid": new_id})
            session.commit()
            session.close()

    def test_set_active_refuses_disabled_strategy(self):
        """set_active_strategy must reject a strategy that is not enabled."""
        from db.strategy_writer import set_active_strategy
        from db.connection import get_session

        session = get_session()
        new_id = session.execute(text(
            "INSERT INTO strategies (name, display_name, class_path, enabled) "
            "VALUES ('disabled-strat-test', 'd', 'x.y', FALSE) "
            "RETURNING strategy_id"
        )).scalar()
        session.commit()
        session.close()

        try:
            assert set_active_strategy("disabled-strat-test") is False
        finally:
            session = get_session()
            session.execute(text(
                "DELETE FROM strategies WHERE strategy_id = :nid"
            ), {"nid": new_id})
            session.commit()
            session.close()
