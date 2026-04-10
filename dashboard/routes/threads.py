"""Threads API — scanner thread status monitoring + error logs."""
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from db.connection import get_session
from db.models import ThreadStatus, Error

router = APIRouter(tags=["threads"])


@router.get("/threads")
def list_threads():
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        threads = session.query(ThreadStatus).order_by(ThreadStatus.thread_name).all()
        result = [
            {
                "id": t.id, "thread_name": t.thread_name, "ticker": t.ticker,
                "status": t.status,
                "pid": t.pid, "thread_id": t.thread_id,
                "last_scan_time": t.last_scan_time.isoformat() if t.last_scan_time else None,
                "last_message": t.last_message,
                "scans_today": t.scans_today, "trades_today": t.trades_today,
                "alerts_today": t.alerts_today, "error_count": t.error_count,
                "updated_at": t.updated_at.isoformat() if t.updated_at else None,
            }
            for t in threads
        ]
        session.close()
        return {"threads": result, "total": len(result)}
    finally:
        session.close()


@router.get("/errors")
def list_errors(
    ticker: Optional[str] = None,
    thread_name: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
):
    """Get error logs, most recent first."""
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        q = session.query(Error)
        if ticker:
            q = q.filter(Error.ticker == ticker.upper())
        if thread_name:
            q = q.filter(Error.thread_name == thread_name)
        errors = q.order_by(Error.created_at.desc()).limit(limit).all()
        result = [
            {
                "id": e.id,
                "thread_name": e.thread_name,
                "ticker": e.ticker,
                "trade_id": e.trade_id,
                "error_type": e.error_type,
                "message": e.message,
                "traceback": e.traceback,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in errors
        ]
        session.close()
        return {"errors": result, "total": len(result)}
    finally:
        session.close()


@router.get("/system-log")
def get_system_log(
    component: Optional[str] = None,
    level: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
):
    """Get system log entries, most recent first."""
    from db.models import SystemLog
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        q = session.query(SystemLog)
        if component:
            q = q.filter(SystemLog.component == component)
        if level:
            q = q.filter(SystemLog.level == level)
        logs = q.order_by(SystemLog.created_at.desc()).limit(limit).all()
        result = [
            {
                "id": l.id,
                "component": l.component,
                "level": l.level,
                "message": l.message,
                "details": l.details or {},
                "created_at": l.created_at.isoformat() if l.created_at else None,
            }
            for l in logs
        ]
        session.close()
        return {"logs": result, "total": len(result)}
    finally:
        session.close()
