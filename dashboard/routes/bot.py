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


async def _sidecar_call(method: str, path: str) -> dict:
    """Call the bot_manager sidecar on the host."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if method == "GET":
                resp = await client.get(f"{SIDECAR_URL}{path}")
            else:
                resp = await client.post(f"{SIDECAR_URL}{path}")
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(503, "Bot manager sidecar is not running. "
                            "Start it with: python start_dashboard.py")
    except httpx.TimeoutException:
        raise HTTPException(504, "Bot manager sidecar timed out")
    except Exception as e:
        raise HTTPException(502, f"Sidecar error: {str(e)}")


@router.get("/bot/status")
async def bot_status():
    """Get bot status from the sidecar."""
    try:
        result = await _sidecar_call("GET", "/status")
        # Enrich with DB info
        from db.connection import db_available
        result["db"] = db_available()
        # Get account and ticker count from DB
        try:
            from db.connection import get_session
            from db.models import BotState
            session = get_session()
            if session:
                state = session.query(BotState).filter(BotState.id == 1).first()
                if state:
                    result["account"] = state.account
                    result["total_tickers"] = state.total_tickers
                    result["started_at"] = state.started_at.isoformat() if state.started_at else result.get("started_at")
                session.close()
        except Exception:
            pass
        return result
    except HTTPException:
        raise
    except Exception:
        # Sidecar not reachable — fall back to DB state
        from db.connection import get_session, db_available
        if not db_available():
            return {"status": "unknown", "db": False, "sidecar": False}
        try:
            from db.models import BotState
            session = get_session()
            if session:
                state = session.query(BotState).filter(BotState.id == 1).first()
                session.close()
                if state:
                    return {
                        "status": state.status,
                        "account": state.account,
                        "total_tickers": state.total_tickers,
                        "db": True,
                        "sidecar": False,
                    }
        except Exception:
            pass
        return {"status": "unknown", "db": True, "sidecar": False}


@router.post("/bot/start")
async def start_bot():
    """Start the bot via the sidecar."""
    return await _sidecar_call("POST", "/start")


@router.post("/bot/stop")
async def stop_bot():
    """Stop the bot via the sidecar."""
    return await _sidecar_call("POST", "/stop")


@router.post("/bot/pause-scans")
async def pause_scans():
    """Pause scanner threads. Exit manager continues monitoring open trades."""
    return await _sidecar_call("POST", "/pause-scans")


@router.post("/bot/resume-scans")
async def resume_scans():
    """Resume scanner threads."""
    return await _sidecar_call("POST", "/resume-scans")
