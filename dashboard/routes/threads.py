"""Threads API — scanner thread status monitoring."""
from fastapi import APIRouter, HTTPException
from db.connection import get_session
from db.models import ThreadStatus

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
