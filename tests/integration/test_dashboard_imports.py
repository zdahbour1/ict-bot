"""
Regression guard for missing-module errors in the API container.

Apr 19 bug: /api/backtests/{id} returned HTTP 500 with
  ModuleNotFoundError: No module named 'backtest_engine'
because dashboard/routes/backtest.py imports backtest_engine.writer
at request time but Dockerfile.api only COPY-ed db/ and dashboard/.

These tests exercise every dashboard route's imports end-to-end and
hit the HTTP surface so any similar gap surfaces during `pytest tests/`
instead of in production.

Runs against a real Postgres. Every route that needs dynamic data
gets a smoke request to force import-time resolution.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

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


@pytest.fixture(scope="module")
def client(db_guard):
    from fastapi.testclient import TestClient
    from dashboard.app import app
    return TestClient(app)


# ── Sanity: every router file is importable ───────────────

class TestRouterImports:
    """Importing each route module must succeed. Catches missing
    top-level dependencies at collection time."""

    def test_bot_importable(self):
        from dashboard.routes import bot  # noqa: F401

    def test_trades_importable(self):
        from dashboard.routes import trades  # noqa: F401

    def test_backtest_importable(self):
        from dashboard.routes import backtest  # noqa: F401

    def test_strategies_importable(self):
        from dashboard.routes import strategies  # noqa: F401

    def test_test_runs_importable(self):
        from dashboard.routes import test_runs  # noqa: F401

    def test_analytics_importable(self):
        from dashboard.routes import analytics  # noqa: F401

    def test_settings_importable(self):
        from dashboard.routes import settings  # noqa: F401


# ── The specific regression ───────────────────────────────

class TestBacktestDetailRegression:
    """Reproduces the Apr 19 500 error. Gets a backtest detail for a
    real run with trades. If the API doesn't have backtest_engine in
    its import path, this 500s with ModuleNotFoundError."""

    @pytest.fixture
    def seeded_run_with_trades(self, db_guard):
        """Create a minimal run with one trade so /backtests/{id} has
        something to fetch."""
        from backtest_engine.writer import (
            create_run, record_trade, delete_run,
        )

        run_id = create_run(
            name="dash-import-regression",
            strategy_id=1,
            tickers=["QQQ"],
            start_date=date(2026, 3, 1),
            end_date=date(2026, 3, 2),
            config={},
        )
        record_trade(run_id, 1, {
            "ticker": "QQQ", "symbol": "QQQ260301C00600000",
            "direction": "LONG", "contracts": 2,
            "entry_price": 2.0, "exit_price": 4.0,
            "pnl_pct": 1.0, "pnl_usd": 400.0,
            "entry_time": datetime(2026, 3, 1, 14, 30, tzinfo=timezone.utc),
            "exit_time": datetime(2026, 3, 1, 15, 15, tzinfo=timezone.utc),
            "hold_minutes": 45,
            "signal_type": "LONG_iFVG",
            "exit_reason": "TP", "exit_result": "WIN",
        })
        yield run_id
        delete_run(run_id)

    def test_get_backtest_returns_200(
        self, client, seeded_run_with_trades,
    ):
        run_id = seeded_run_with_trades
        r = client.get(f"/api/backtests/{run_id}")
        # The smoking gun: must NOT be 500. If this regresses,
        # Dockerfile.api is missing a dependency module again.
        assert r.status_code != 500, (
            f"500 from /api/backtests/{run_id}: {r.text[:200]}\n"
            f"Likely cause: a module imported by dashboard/routes/backtest.py "
            f"is missing from Dockerfile.api's COPY statements."
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert "run" in data
        assert data["trade_count"] >= 1

    def test_trades_pagination_endpoint_works(
        self, client, seeded_run_with_trades,
    ):
        """The new /trades paginated endpoint should also be reachable."""
        run_id = seeded_run_with_trades
        r = client.get(f"/api/backtests/{run_id}/trades?limit=5")
        assert r.status_code != 500, r.text
        assert r.status_code == 200
        data = r.json()
        assert "trades" in data
        assert "total" in data
        assert data["total"] >= 1

    def test_analytics_and_feature_analysis_too(
        self, client, seeded_run_with_trades,
    ):
        """These sibling endpoints use raw SQL and worked even when
        /backtests/{id} broke — lock both in so we notice if the
        reverse ever happens."""
        run_id = seeded_run_with_trades
        r = client.get(f"/api/backtests/{run_id}/analytics")
        assert r.status_code == 200, r.text
        r = client.get(f"/api/backtests/{run_id}/feature_analysis")
        assert r.status_code == 200, r.text


# ── Surface-level reachability ────────────────────────────

class TestKeyRoutesReachable:
    """Each route answers ≥1 GET without 500. Don't care about the
    specific response shape — just "does the import + handler resolve."""

    @pytest.mark.parametrize("path", [
        "/api/health",
        "/api/strategies",
        "/api/strategies/active",
        "/api/backtests?limit=1",
        "/api/backtests/strategies",
        "/api/tickers",
        "/api/settings",
        "/api/threads",
        "/api/summary",
        "/api/test-runs?limit=1",
        "/api/test-runs/summary?limit=1",
    ])
    def test_not_500(self, client, path):
        r = client.get(path)
        assert r.status_code < 500, (
            f"{path} returned {r.status_code}: {r.text[:200]}"
        )
