"""Test Runs API — history of pytest executions.

Powers the Tests tab in the dashboard: list runs, drill into one run's
individual test results, expose a pass/fail trend timeseries, and
launch new test runs by proxying to the bot_manager sidecar.
"""
import os
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Body

from db.connection import get_session
from db.models import TestRun, TestResult

router = APIRouter(tags=["test-runs"])

# Same sidecar the bot control routes talk to
SIDECAR_URL = os.getenv("BOT_SIDECAR_URL", "http://host.docker.internal:9000")
ALLOWED_SUITES = {"unit", "concurrency", "integration", "all"}


@router.post("/test-runs/launch")
async def launch_test_run(payload: dict = Body(default={})):
    """Kick off a pytest subprocess on the host via bot_manager.

    Body: {"suite": "unit"|"concurrency"|"integration"|"all"}
    Returns 202 with the sidecar's acknowledgement. A row appears in
    test_runs as the run progresses (poll /test-runs/summary to detect it).
    """
    suite = (payload.get("suite") or "unit").strip()
    if suite not in ALLOWED_SUITES:
        raise HTTPException(400, f"suite must be one of {sorted(ALLOWED_SUITES)}")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{SIDECAR_URL}/run-tests",
                json={"suite": suite, "triggered_by": "dashboard"},
            )
            data = resp.json()
            if resp.status_code >= 400:
                raise HTTPException(resp.status_code, data.get("error", "sidecar error"))
            return data
    except httpx.ConnectError:
        raise HTTPException(
            503,
            "Bot manager sidecar is not running. Start it with: python bot_manager.py"
        )
    except httpx.TimeoutException:
        raise HTTPException(504, "Sidecar timed out starting pytest")


def _run_to_dict(r: TestRun) -> dict:
    return {
        "id": r.id,
        "git_branch": r.git_branch,
        "git_sha": r.git_sha,
        "suite": r.suite,
        "total": r.total,
        "passed": r.passed,
        "failed": r.failed,
        "skipped": r.skipped,
        "errors": r.errors,
        "duration_sec": float(r.duration_sec) if r.duration_sec is not None else 0.0,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "triggered_by": r.triggered_by,
        "python_version": r.python_version,
        "platform": r.platform,
        "exit_status": r.exit_status,
        "summary": r.summary,
    }


@router.get("/test-runs")
def list_test_runs(
    limit: int = Query(50, ge=1, le=500),
    suite: Optional[str] = None,
    branch: Optional[str] = None,
    status: Optional[str] = None,
):
    """List recent test runs, most recent first."""
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        q = session.query(TestRun)
        if suite:
            q = q.filter(TestRun.suite == suite)
        if branch:
            q = q.filter(TestRun.git_branch == branch)
        if status:
            q = q.filter(TestRun.exit_status == status)
        runs = q.order_by(TestRun.started_at.desc()).limit(limit).all()
        return {"runs": [_run_to_dict(r) for r in runs], "total": len(runs)}
    finally:
        session.close()


@router.get("/test-runs/summary")
def test_runs_summary(limit: int = Query(30, ge=1, le=200)):
    """Pass/fail trend over the most recent N runs (oldest → newest for charts)."""
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        runs = (session.query(TestRun)
                .order_by(TestRun.started_at.desc())
                .limit(limit).all())
        # Most recent first from DB → reverse for chronological charting
        runs = list(reversed(runs))
        trend = [
            {
                "id": r.id,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "total": r.total, "passed": r.passed, "failed": r.failed,
                "duration_sec": float(r.duration_sec) if r.duration_sec is not None else 0.0,
                "exit_status": r.exit_status,
                "git_sha": r.git_sha,
            }
            for r in runs
        ]
        # Latest run summary card
        latest = trend[-1] if trend else None
        return {"trend": trend, "latest": latest, "count": len(trend)}
    finally:
        session.close()


@router.get("/test-runs/{run_id}")
def get_test_run(run_id: int):
    """One run + its full per-test result list."""
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        run = session.get(TestRun, run_id)
        if run is None:
            raise HTTPException(404, f"test_run {run_id} not found")
        results = (session.query(TestResult)
                   .filter(TestResult.run_id == run_id)
                   .order_by(TestResult.outcome.desc(), TestResult.nodeid)
                   .all())
        return {
            "run": _run_to_dict(run),
            "results": [
                {
                    "id": t.id,
                    "nodeid": t.nodeid,
                    "module": t.module,
                    "test_class": t.test_class,
                    "test_name": t.test_name,
                    "outcome": t.outcome,
                    "duration_sec": float(t.duration_sec) if t.duration_sec is not None else 0.0,
                    "error_message": t.error_message,
                    "traceback": t.traceback,
                }
                for t in results
            ],
        }
    finally:
        session.close()


@router.delete("/test-runs/{run_id}")
def delete_test_run(run_id: int):
    """Delete a run (cascade deletes its results)."""
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        run = session.get(TestRun, run_id)
        if run is None:
            raise HTTPException(404, f"test_run {run_id} not found")
        session.delete(run)
        session.commit()
        return {"deleted": run_id}
    finally:
        session.close()
