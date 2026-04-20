"""
Integration tests for db/settings_loader.py — strategy-scoped settings
with global fallback, plus strategy-scoped ticker loading.

Covers ENH-024 rollouts #2 and #3 of active_strategy_design.md.
"""
from __future__ import annotations

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


# ── Active-strategy resolution ───────────────────────────

class TestActiveStrategyResolution:
    def test_get_active_strategy_id_returns_ict_by_default(self, db_guard):
        """ACTIVE_STRATEGY='ict' (as seeded by rollout #1) should resolve."""
        from db.settings_loader import get_active_strategy_id
        sid = get_active_strategy_id()
        assert sid == 1, f"expected ict (strategy_id=1), got {sid}"

    def test_get_default_strategy_id_returns_ict(self, db_guard):
        from db.settings_loader import get_default_strategy_id
        assert get_default_strategy_id() == 1

    def test_resolve_strategy_id_prefers_active(self, db_guard):
        from db.settings_loader import resolve_strategy_id
        assert resolve_strategy_id() == 1


# ── Strategy-scoped settings ─────────────────────────────

class TestStrategyScopedSettings:
    def test_load_for_ict_includes_both_globals_and_scoped(self, db_guard):
        """ICT has PROFIT_TARGET (strategy-scoped) + IB_HOST (global).
        Both must appear in the resolved dict."""
        from db.settings_loader import load_settings_from_db
        settings = load_settings_from_db(strategy_id=1)
        assert settings is not None
        # A canonical strategy-scoped key:
        assert "PROFIT_TARGET" in settings
        # Active strategy is a global key:
        assert "ACTIVE_STRATEGY" in settings

    def test_scoped_overrides_global(self, db_guard):
        """If the same key has both a global row and a strategy-scoped
        row, the strategy-scoped one wins in the returned dict."""
        from db.connection import get_session
        from db.settings_loader import load_settings_from_db

        session = get_session()
        # Inject a duplicate: TEST_OVERLAY_KEY as both global and ICT-scoped
        session.execute(text(
            "INSERT INTO settings (category, key, value, data_type, description, strategy_id) "
            "VALUES ('test', 'TEST_OVERLAY_KEY', 'global_val', 'string', 'overlay test', NULL) "
            "ON CONFLICT (key, strategy_id) DO NOTHING"
        ))
        session.execute(text(
            "INSERT INTO settings (category, key, value, data_type, description, strategy_id) "
            "VALUES ('test', 'TEST_OVERLAY_KEY', 'scoped_val', 'string', 'overlay test', 1) "
            "ON CONFLICT (key, strategy_id) DO NOTHING"
        ))
        session.commit()
        session.close()

        try:
            settings = load_settings_from_db(strategy_id=1)
            assert settings is not None
            assert settings["TEST_OVERLAY_KEY"] == "scoped_val", (
                "strategy-scoped value must override global "
                f"(got {settings.get('TEST_OVERLAY_KEY')})"
            )
        finally:
            session = get_session()
            session.execute(text(
                "DELETE FROM settings WHERE key = 'TEST_OVERLAY_KEY'"
            ))
            session.commit()
            session.close()

    def test_different_strategies_see_different_scoped_values(self, db_guard):
        """ICT and ORB can have distinct values for the same setting key."""
        from db.settings_loader import load_settings_from_db

        ict_settings = load_settings_from_db(strategy_id=1)
        # ORB strategy_id varies by Postgres sequence state — look it up
        from db.connection import get_session
        s = get_session()
        orb_sid = s.execute(text(
            "SELECT strategy_id FROM strategies WHERE name = 'orb'"
        )).scalar()
        s.close()
        assert orb_sid is not None

        orb_settings = load_settings_from_db(strategy_id=orb_sid)
        assert orb_settings is not None

        # Both should have PROFIT_TARGET (ORB's own + ICT's own, independent)
        assert "PROFIT_TARGET" in ict_settings
        assert "PROFIT_TARGET" in orb_settings

        # And ORB has ORB-specific keys that ICT does not
        assert "ORB_RANGE_MINUTES" in orb_settings
        assert "ORB_RANGE_MINUTES" not in ict_settings

    def test_data_type_casting(self, db_guard):
        """Int/float/bool data_type values are cast to native Python types."""
        from db.settings_loader import load_settings_from_db
        settings = load_settings_from_db(strategy_id=1)
        # PROFIT_TARGET is stored as 'float' → returns a float
        assert isinstance(settings["PROFIT_TARGET"], float)


# ── Strategy-scoped tickers ──────────────────────────────

class TestStrategyScopedTickers:
    def test_load_tickers_for_ict(self, db_guard):
        from db.settings_loader import load_tickers_from_db
        tickers = load_tickers_from_db(strategy_id=1)
        assert tickers is not None
        # We know ICT has >=20 tickers (from earlier setup)
        assert len(tickers) > 0
        # Every row is a dict with the canonical fields
        sample = tickers[0]
        for key in ("symbol", "contracts", "sec_type", "multiplier", "exchange", "currency"):
            assert key in sample, f"ticker row missing '{key}': {sample}"

    def test_fop_tickers_excluded_from_ict_active_list(self, db_guard):
        """FOP tickers (seeded inactive on the futures-options branch)
        must not appear in the active list for ICT since is_active=FALSE."""
        from db.settings_loader import load_tickers_from_db
        tickers = load_tickers_from_db(strategy_id=1)
        assert tickers is not None
        fop_symbols = {"MNQ", "NQ", "MES", "ES", "GC", "CL"}
        active_symbols = {t["symbol"] for t in tickers}
        leaked = fop_symbols & active_symbols
        assert not leaked, f"inactive FOP tickers leaked into active list: {leaked}"

    def test_load_ticker_symbols_helper(self, db_guard):
        from db.settings_loader import load_active_ticker_symbols
        symbols = load_active_ticker_symbols(strategy_id=1)
        assert isinstance(symbols, list)
        assert all(isinstance(s, str) for s in symbols)

    def test_resolve_strategy_used_when_none_passed(self, db_guard):
        """Omitting strategy_id uses the resolved active/default strategy."""
        from db.settings_loader import load_tickers_from_db
        # No arg → resolves to ict (strategy_id=1)
        t1 = load_tickers_from_db()
        t2 = load_tickers_from_db(strategy_id=1)
        assert t1 is not None and t2 is not None
        assert {x["symbol"] for x in t1} == {x["symbol"] for x in t2}


# ── Back-compat: old signatures still work ───────────────

class TestBackwardsCompat:
    def test_zero_arg_call_still_works(self, db_guard):
        """Callers that don't pass strategy_id still function."""
        from db.settings_loader import (
            load_settings_from_db, load_tickers_from_db,
            load_active_ticker_symbols, load_contracts_per_ticker,
            get_setting,
        )
        assert load_settings_from_db() is not None
        assert load_tickers_from_db() is not None
        assert load_active_ticker_symbols() is not None
        assert load_contracts_per_ticker() is not None
        # get_setting resolves through the overlay
        assert get_setting("ACTIVE_STRATEGY") == "ict"
