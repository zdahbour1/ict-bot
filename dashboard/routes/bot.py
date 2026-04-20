"""
Bot control API — start, stop, status via the host-side bot_manager sidecar.

The bot runs on the host machine (for IB TWS connectivity), not inside Docker.
The sidecar (bot_manager.py) on the host provides HTTP endpoints to manage it.
The API proxies these calls from the Docker-hosted frontend.
"""
import os
import logging
import httpx
from fastapi import APIRouter, HTTPException

log = logging.getLogger(__name__)

router = APIRouter(tags=["bot"])

# Sidecar runs on the host machine
SIDECAR_URL = os.getenv("BOT_SIDECAR_URL", "http://host.docker.internal:9000")


async def _sidecar_call(method: str, path: str, json_body: dict = None) -> dict:
    """Call the bot_manager sidecar on the host."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if method == "GET":
                resp = await client.get(f"{SIDECAR_URL}{path}")
            else:
                resp = await client.post(f"{SIDECAR_URL}{path}", json=json_body)
            data = resp.json()
            if resp.status_code >= 400:
                error_msg = data.get("error", f"Sidecar returned {resp.status_code}")
                raise HTTPException(resp.status_code, error_msg)
            return data
    except httpx.ConnectError:
        raise HTTPException(503, "Bot manager sidecar is not running. "
                            "Start it with: python start_dashboard.py")
    except httpx.TimeoutException:
        raise HTTPException(504, "Bot manager sidecar timed out")
    except Exception as e:
        raise HTTPException(502, f"Sidecar error: {str(e)}")


async def _sidecar_status_safe() -> dict | None:
    """Best-effort sidecar status. Returns None if the sidecar is
    unreachable or slow — we don't want a sidecar hiccup to break the
    status endpoint."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{SIDECAR_URL}/status")
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        return None
    return None


def _heal_stale_running(state, session) -> None:
    """Called when DB says running but the sidecar has no live process.
    The bot crashed ungracefully (TWS disconnect, network drop, kill -9,
    etc.) and nothing ran the graceful-shutdown DB update. Flip the row
    to stopped so the UI's Start/Stop button unsticks automatically."""
    from datetime import datetime, timezone
    import logging
    logging.getLogger(__name__).warning(
        f"Healing stale bot_state: DB=running pid={state.pid} but sidecar "
        f"reports no live process — flipping to stopped."
    )
    state.status = "stopped"
    state.pid = None
    state.ib_connected = False
    state.scans_active = False
    state.stopped_at = datetime.now(timezone.utc)
    state.last_error = (
        (state.last_error or "") +
        " | auto-healed: sidecar had no live process"
    )[-500:]
    session.commit()


@router.get("/bot/status")
async def bot_status():
    """Get bot status. Primary source is the DB `bot_state` row, but we
    cross-check with the sidecar and auto-heal if they disagree — the
    DB row can get stranded as 'running' after an ungraceful crash."""
    from db.connection import get_session, db_available
    if not db_available():
        return {"status": "unknown", "db": False}
    try:
        from db.models import BotState
        session = get_session()
        if not session:
            return {"status": "unknown", "db": False}
        state = session.query(BotState).filter(BotState.id == 1).first()
        if not state:
            session.close()
            return {"status": "stopped", "db": True}

        # Cross-check with sidecar. If DB says running but the sidecar
        # reports stopped, the bot died ungracefully — self-heal so the
        # UI's Start/Stop button doesn't get stuck.
        if state.status == "running":
            sc = await _sidecar_status_safe()
            if sc is not None and sc.get("status") == "stopped":
                _heal_stale_running(state, session)

        response = {
            "status": state.status,
            "scans_active": state.scans_active,
            "ib_connected": state.ib_connected,
            "account": state.account,
            "pid": state.pid,
            "total_tickers": state.total_tickers,
            "last_error": state.last_error,
            "started_at": state.started_at.isoformat() if state.started_at else None,
            "stopped_at": state.stopped_at.isoformat() if state.stopped_at else None,
            "db": True,
        }
        session.close()
        return response
    except Exception:
        return {"status": "unknown", "db": True}


@router.post("/bot/start")
async def start_bot():
    """Start the bot via the sidecar."""
    return await _sidecar_call("POST", "/start")


@router.post("/bot/stop")
async def stop_bot():
    """Stop the bot: set stop_requested in DB, also try sidecar."""
    # Set DB flag — bot checks this every 2 seconds
    try:
        from db.writer import set_stop_requested, add_system_log
        set_stop_requested(True)
        add_system_log("api", "info", "Stop requested via dashboard")
    except Exception:
        pass
    # Also try sidecar for immediate stop
    try:
        return await _sidecar_call("POST", "/stop")
    except Exception:
        return {"status": "stop_requested", "note": "DB flag set, bot will stop on next cycle"}


@router.post("/bot/pause-scans")
async def pause_scans():
    """Stop scanner threads via DB. Bot reads this every 2 seconds."""
    try:
        from db.writer import set_scans_active, add_system_log
        set_scans_active(False)
        add_system_log("api", "info", "Scans stopped via dashboard")
        return {"status": "scans_stopped"}
    except Exception as e:
        raise HTTPException(500, f"Failed to update scan state: {e}")


@router.post("/bot/resume-scans")
async def resume_scans():
    """Start scanner threads via DB. Bot reads this every 2 seconds."""
    try:
        from db.writer import set_scans_active, add_system_log
        set_scans_active(True)
        add_system_log("api", "info", "Scans started via dashboard")
        return {"status": "scans_started"}
    except Exception as e:
        raise HTTPException(500, f"Failed to update scan state: {e}")


@router.post("/bot/reconcile")
async def trigger_reconciliation():
    """Trigger manual reconciliation. Bot picks this up on next main loop cycle."""
    try:
        from db.writer import add_system_log
        from db.connection import get_session
        session = get_session()
        if session:
            # Use bot_state.last_error field as a signal channel
            session.execute(
                __import__('sqlalchemy').text(
                    "UPDATE bot_state SET last_error = 'RECONCILE_REQUESTED' WHERE id = 1"
                )
            )
            session.commit()
            session.close()
        add_system_log("api", "info", "Manual reconciliation requested via dashboard")
        return {"status": "reconcile_requested"}
    except Exception as e:
        raise HTTPException(500, f"Failed to trigger reconciliation: {e}")
