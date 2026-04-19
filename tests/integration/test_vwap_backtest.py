"""
Integration tests for VWAP end-to-end via the backtest runner.
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


class TestVwapEnabled:
    def test_vwap_row_enabled(self, session):
        row = session.execute(text(
            "SELECT enabled, class_path FROM strategies WHERE name = 'vwap_revert'"
        )).fetchone()
        assert row is not None
        assert row[0] is True
        assert row[1] == "strategy.vwap_strategy.VWAPStrategy"

    def test_vwap_settings_seeded(self, session):
        cnt = session.execute(text(
            "SELECT COUNT(*) FROM settings WHERE strategy_id = "
            "  (SELECT strategy_id FROM strategies WHERE name = 'vwap_revert')"
        )).scalar()
        assert cnt >= 8, f"expected >= 8 VWAP settings, found {cnt}"

    def test_vwap_visible_in_strategies_api(self, db_guard):
        from db.strategy_writer import list_strategies
        names = {s["name"] for s in list_strategies(enabled_only=True)}
        assert {"ict", "orb", "vwap_revert"}.issubset(names)


class TestVwapClassPath:
    def test_vwap_class_importable(self):
        import importlib
        module = importlib.import_module("strategy.vwap_strategy")
        cls = getattr(module, "VWAPStrategy")
        instance = cls()
        assert instance.name == "vwap_revert"
        assert hasattr(instance, "detect")
        assert callable(instance.configure)

    def test_vwap_registered(self):
        from strategy.base_strategy import StrategyRegistry
        got = StrategyRegistry.get("vwap_revert")
        assert got is not None
        inst = StrategyRegistry.instantiate("vwap_revert")
        assert inst is not None
        assert inst.name == "vwap_revert"
