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


@router.get("/bot/status")
async def bot_status():
    """Get bot status from DB (single source of truth)."""
    from db.connection import get_session, db_available
    if not db_available():
        return {"status": "unknown", "db": False}
    try:
        from db.models import BotState
        session = get_session()
        if not session:
            return {"status": "unknown", "db": False}
        state = session.query(BotState).filter(BotState.id == 1).first()
        session.close()
        if not state:
            return {"status": "stopped", "db": True}
        return {
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
