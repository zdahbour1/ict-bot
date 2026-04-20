"""
Integration tests for dashboard/routes/strategies.py — the Strategies
tab's API surface.
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


@pytest.fixture(scope="module")
def client(db_guard):
    from fastapi.testclient import TestClient
    from dashboard.app import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_active_strategy(db_guard):
    """Every test in this file assumes ACTIVE_STRATEGY='ict' as the
    baseline. Earlier tests (possibly in other files) can leave the
    row at another value if they fail mid-flight — reset before each
    test to keep the suite deterministic."""
    from db.strategy_writer import set_active_strategy
    set_active_strategy("ict")
    yield
    set_active_strategy("ict")


# ── GET /strategies ─────────────────────────────────────

class TestListStrategies:
    def test_lists_all_strategies(self, client):
        r = client.get("/api/strategies")
        assert r.status_code == 200
        data = r.json()
        names = {s["name"] for s in data["strategies"]}
        # The four we've seeded (possibly plus any earlier test rows)
        assert {"ict", "orb", "vwap_revert", "delta_neutral"}.issubset(names)
        assert data["active"] == "ict"

    def test_is_active_flag_correct(self, client):
        r = client.get("/api/strategies")
        data = r.json()
        active_rows = [s for s in data["strategies"] if s["is_active"]]
        assert len(active_rows) == 1
        assert active_rows[0]["name"] == "ict"

    def test_shape_of_each_row(self, client):
        r = client.get("/api/strategies")
        for s in r.json()["strategies"]:
            for key in ("strategy_id", "name", "display_name", "class_path",
                        "enabled", "is_default", "is_active"):
                assert key in s


# ── GET /strategies/active ──────────────────────────────

class TestGetActive:
    def test_returns_ict(self, client):
        r = client.get("/api/strategies/active")
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "ict"
        assert data["is_active"] is True


# ── POST /strategies/{id}/activate ──────────────────────

class TestActivate:
    def _lookup_id(self, client, name: str) -> int:
        r = client.get("/api/strategies")
        row = next(s for s in r.json()["strategies"] if s["name"] == name)
        return row["strategy_id"]

    def test_switch_to_orb_and_back(self, client, db_guard):
        orb_id = self._lookup_id(client, "orb")
        ict_id = self._lookup_id(client, "ict")

        # Activate ORB
        r = client.post(f"/api/strategies/{orb_id}/activate")
        assert r.status_code == 200, r.text
        assert r.json()["activated"] == "orb"

        # Verify state reflects it
        r = client.get("/api/strategies/active")
        assert r.json()["name"] == "orb"

        # Flip back to ICT
        r = client.post(f"/api/strategies/{ict_id}/activate")
        assert r.status_code == 200
        r = client.get("/api/strategies/active")
        assert r.json()["name"] == "ict"

    def test_cannot_activate_disabled(self, client):
        """delta_neutral is seeded enabled=FALSE — should refuse."""
        dn_id = self._lookup_id(client, "delta_neutral")
        r = client.post(f"/api/strategies/{dn_id}/activate")
        assert r.status_code == 400
        assert "disabled" in r.json()["detail"].lower()

    def test_404_on_missing(self, client):
        r = client.post("/api/strategies/999999/activate")
        assert r.status_code == 404


# ── POST /strategies/{id}/enable|disable ────────────────

class TestEnableDisable:
    def _lookup_id(self, client, name: str) -> int:
        r = client.get("/api/strategies")
        row = next(s for s in r.json()["strategies"] if s["name"] == name)
        return row["strategy_id"]

    def test_toggle_delta_neutral(self, client, db_guard):
        dn_id = self._lookup_id(client, "delta_neutral")

        # Enable
        r = client.post(f"/api/strategies/{dn_id}/enable")
        assert r.status_code == 200
        assert r.json()["enabled"] is True

        # Disable again
        r = client.post(f"/api/strategies/{dn_id}/disable")
        assert r.status_code == 200
        assert r.json()["enabled"] is False

    def test_cannot_disable_active_strategy(self, client):
        ict_id = self._lookup_id(client, "ict")
        r = client.post(f"/api/strategies/{ict_id}/disable")
        assert r.status_code == 400
        assert "active" in r.json()["detail"].lower()
