"""
Integration test for bot_state auto-heal — regression guard against the
"button stuck on Stop Bot" bug observed Apr 19.

Reproduces the failure mode:
  1. DB bot_state row says status='running' with a PID
  2. Sidecar has no matching process
  3. /api/bot/status must detect the drift and flip the DB to 'stopped'
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

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
def stale_running_row(db_guard):
    """Force bot_state into the broken state: running + pid set, but no
    real process. Restore the original values after the test."""
    from db.connection import get_session

    session = get_session()
    # Capture current state to restore
    original = session.execute(text(
        "SELECT status, pid, ib_connected, scans_active, stopped_at "
        "FROM bot_state WHERE id = 1"
    )).fetchone()

    # Put it into the stuck state
    session.execute(text(
        "UPDATE bot_state SET status='running', pid=99999, "
        "ib_connected=TRUE, scans_active=FALSE "
        "WHERE id = 1"
    ))
    session.commit()
    session.close()

    yield

    # Restore
    session = get_session()
    if original is not None:
        session.execute(text(
            "UPDATE bot_state SET status=:s, pid=:p, "
            "ib_connected=:ib, scans_active=:sa, stopped_at=:st "
            "WHERE id = 1"
        ), {
            "s": original[0], "p": original[1], "ib": original[2],
            "sa": original[3], "st": original[4],
        })
    else:
        session.execute(text("UPDATE bot_state SET status='stopped', pid=NULL"))
    session.commit()
    session.close()


@pytest.fixture
def client(db_guard):
    from fastapi.testclient import TestClient
    from dashboard.app import app
    return TestClient(app)


class TestBotStatusHeal:
    def test_heals_stale_running_when_sidecar_reports_stopped(
        self, client, stale_running_row
    ):
        """The critical regression: DB says running, sidecar says stopped
        → /api/bot/status must flip the DB to stopped so the UI button
        unsticks."""
        # Patch the sidecar status lookup to pretend sidecar says stopped
        with patch("dashboard.routes.bot._sidecar_status_safe",
                   new=AsyncMock(return_value={"status": "stopped", "pid": None})):
            r = client.get("/api/bot/status")

        assert r.status_code == 200
        data = r.json()
        # The endpoint heals the row in-flight, so the response should
        # already show 'stopped' (it reads from the same session after
        # the UPDATE commits).
        assert data["status"] == "stopped", (
            f"expected auto-heal to 'stopped', got {data['status']}"
        )

        # And the DB row should be healed, verifiable directly
        from db.connection import get_session
        session = get_session()
        row = session.execute(text(
            "SELECT status, pid, ib_connected FROM bot_state WHERE id = 1"
        )).fetchone()
        session.close()
        assert row[0] == "stopped"
        assert row[1] is None
        assert row[2] is False

    def test_does_not_heal_when_sidecar_agrees_running(
        self, client, stale_running_row
    ):
        """If both DB and sidecar say running, we trust it — no heal."""
        with patch("dashboard.routes.bot._sidecar_status_safe",
                   new=AsyncMock(return_value={"status": "running", "pid": 99999})):
            r = client.get("/api/bot/status")

        assert r.status_code == 200
        # State stays 'running'
        assert r.json()["status"] == "running"

    def test_does_not_heal_when_sidecar_unreachable(
        self, client, stale_running_row
    ):
        """Sidecar unreachable → don't touch the DB (might just be a
        transient network/process issue; treat conservatively)."""
        with patch("dashboard.routes.bot._sidecar_status_safe",
                   new=AsyncMock(return_value=None)):
            r = client.get("/api/bot/status")

        assert r.status_code == 200
        # State preserved as running — we don't flip based on sidecar silence
        assert r.json()["status"] == "running"
