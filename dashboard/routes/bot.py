"""Bot control API — start, stop, status."""
import os
import signal
import subprocess
import sys
from fastapi import APIRouter, HTTPException
from db.connection import get_session
from db.models import BotState

router = APIRouter(tags=["bot"])


@router.get("/bot/status")
def bot_status():
    session = get_session()
    if not session:
        return {"status": "unknown", "db": False}
    try:
        state = session.query(BotState).filter(BotState.id == 1).first()
        if not state:
            return {"status": "stopped", "db": True}
        result = {
            "status": state.status,
            "account": state.account,
            "pid": state.pid,
            "total_tickers": state.total_tickers,
            "started_at": state.started_at.isoformat() if state.started_at else None,
            "stopped_at": state.stopped_at.isoformat() if state.stopped_at else None,
            "db": True,
        }
        # Check if PID is actually running
        if state.pid and state.status == "running":
            try:
                os.kill(state.pid, 0)  # signal 0 = check if process exists
            except OSError:
                result["status"] = "crashed"
                state.status = "stopped"
                session.commit()
        session.close()
        return result
    finally:
        session.close()


@router.post("/bot/start")
def start_bot():
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        state = session.query(BotState).filter(BotState.id == 1).first()
        if state and state.status == "running":
            # Verify PID is actually running
            try:
                os.kill(state.pid, 0)
                raise HTTPException(409, "Bot is already running")
            except OSError:
                pass  # PID not running, proceed to start

        # Start bot as subprocess
        bot_dir = os.path.join(os.path.dirname(__file__), "..", "..")
        main_py = os.path.join(bot_dir, "main.py")
        proc = subprocess.Popen(
            [sys.executable, main_py],
            cwd=bot_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        session.close()
        return {"status": "starting", "pid": proc.pid}
    finally:
        session.close()


@router.post("/bot/stop")
def stop_bot():
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        state = session.query(BotState).filter(BotState.id == 1).first()
        if not state or state.status != "running":
            raise HTTPException(400, "Bot is not running")

        pid = state.pid
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                state.status = "stopping"
                session.commit()
                return {"status": "stopping", "pid": pid}
            except OSError as e:
                state.status = "stopped"
                session.commit()
                return {"status": "already_stopped", "error": str(e)}
        raise HTTPException(400, "No PID recorded for running bot")
    finally:
        session.close()
