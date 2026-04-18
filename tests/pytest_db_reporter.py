"""
Pytest plugin — writes each run + individual test results to the
test_runs / test_results DB tables so the dashboard can show history.

Activation: set PYTEST_DB_REPORT=1 in the env (or ICT_PYTEST_DB_REPORT=1).
Without it, the plugin is a no-op so contributors who don't have the
DB container running aren't forced to depend on it.

Connection: uses db.connection.get_session() — same pool as the app.

Failure handling: if the DB is unreachable we log a warning and keep
going. Reporting to the DB must never break the test suite.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest


def _enabled() -> bool:
    for key in ("PYTEST_DB_REPORT", "ICT_PYTEST_DB_REPORT"):
        val = os.environ.get(key, "").strip().lower()
        if val in ("1", "true", "yes", "on"):
            return True
    return False


def _git_info() -> tuple[str | None, str | None]:
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode().strip()
    except Exception:
        branch = None
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode().strip()
    except Exception:
        sha = None
    return branch, sha


def _split_nodeid(nodeid: str) -> tuple[str, str | None, str | None]:
    """'tests/unit/test_foo.py::TestBar::test_baz' →
        ('tests/unit/test_foo.py', 'TestBar', 'test_baz')
    """
    parts = nodeid.split("::")
    module = parts[0]
    if len(parts) == 3:
        return module, parts[1], parts[2]
    if len(parts) == 2:
        return module, None, parts[1]
    return module, None, None


class DBReporter:
    """Pytest plugin that persists results to Postgres."""

    def __init__(self):
        self.enabled = _enabled()
        self.run_id: int | None = None
        self._start_ts: float = 0.0
        self._session = None
        # Per-test outcome scratch: nodeid → {"outcome", "duration", "error", "tb"}
        self._results: dict = {}

    # ── Lifecycle ────────────────────────────────────────────
    def pytest_sessionstart(self, session):
        if not self.enabled:
            return
        try:
            # Import here so pytest collection still works without the project on sys.path
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
            from db.connection import get_session
            from db.models import TestRun

            self._session = get_session()
            if self._session is None:
                self.enabled = False
                print("[pytest-db] DB not available — skipping test-run reporting")
                return

            branch, sha = _git_info()
            suite = os.environ.get("PYTEST_SUITE", "unit")
            triggered = os.environ.get("PYTEST_TRIGGERED_BY", "manual")

            run = TestRun(
                git_branch=branch,
                git_sha=sha,
                suite=suite,
                triggered_by=triggered,
                python_version=platform.python_version(),
                platform=f"{platform.system()}-{platform.machine()}",
                started_at=datetime.now(timezone.utc),
                exit_status="pending",
            )
            self._session.add(run)
            self._session.commit()
            self.run_id = run.id
            self._start_ts = time.time()
        except Exception as e:
            self.enabled = False
            print(f"[pytest-db] Could not create test_run row: {e}")

    def pytest_runtest_logreport(self, report):
        """Pytest fires this 3x per test (setup/call/teardown). We collapse
        those into one logical outcome per nodeid."""
        if not self.enabled:
            return
        nid = report.nodeid

        prev = self._results.get(nid, {
            "outcome": "passed",
            "duration": 0.0,
            "error": None,
            "tb": None,
        })
        # Accumulate total time across phases
        prev["duration"] += float(report.duration or 0)

        if report.outcome == "failed":
            prev["outcome"] = "failed"
            longrepr = getattr(report, "longrepr", None)
            if longrepr is not None:
                tb_text = str(longrepr)
                prev["tb"] = tb_text[:4000]  # guard against massive tracebacks
                # First non-blank line is usually the assertion/error
                for line in tb_text.splitlines():
                    s = line.strip()
                    if s:
                        prev["error"] = s[:500]
                        break
        elif report.outcome == "skipped" and prev["outcome"] == "passed":
            prev["outcome"] = "skipped"
            if hasattr(report, "longrepr") and report.longrepr:
                prev["error"] = str(report.longrepr)[:500]

        self._results[nid] = prev

    def pytest_sessionfinish(self, session, exitstatus):
        if not self.enabled or self.run_id is None:
            return
        try:
            from db.models import TestRun, TestResult

            # Bulk-insert individual results
            rows = []
            passed = failed = skipped = errors = 0
            for nid, data in self._results.items():
                module, cls, name = _split_nodeid(nid)
                outcome = data["outcome"]
                if outcome == "passed":
                    passed += 1
                elif outcome == "failed":
                    failed += 1
                elif outcome == "skipped":
                    skipped += 1
                else:
                    errors += 1

                rows.append(TestResult(
                    run_id=self.run_id,
                    nodeid=nid[:1000],
                    module=module[:200] if module else None,
                    test_class=cls[:100] if cls else None,
                    test_name=name[:200] if name else None,
                    outcome=outcome,
                    duration_sec=round(data["duration"], 4),
                    error_message=data.get("error"),
                    traceback=data.get("tb"),
                ))
            self._session.bulk_save_objects(rows)

            # Update the run row with aggregates
            run = self._session.get(TestRun, self.run_id)
            if run:
                run.total = len(self._results)
                run.passed = passed
                run.failed = failed
                run.skipped = skipped
                run.errors = errors
                run.duration_sec = round(time.time() - self._start_ts, 3)
                run.finished_at = datetime.now(timezone.utc)
                if errors:
                    run.exit_status = "error"
                elif failed:
                    run.exit_status = "failed"
                else:
                    run.exit_status = "passed"
                run.summary = (f"{passed} passed, {failed} failed, "
                               f"{skipped} skipped in {run.duration_sec}s")
            self._session.commit()
        except Exception as e:
            print(f"[pytest-db] Failed to record results: {e}")
            try:
                self._session.rollback()
            except Exception:
                pass
        finally:
            try:
                self._session.close()
            except Exception:
                pass


_plugin: DBReporter | None = None


def pytest_configure(config):
    global _plugin
    _plugin = DBReporter()
    config.pluginmanager.register(_plugin, name="ict-db-reporter")


def pytest_unconfigure(config):
    global _plugin
    if _plugin is not None:
        config.pluginmanager.unregister(_plugin)
        _plugin = None
