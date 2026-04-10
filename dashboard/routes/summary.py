"""Summary API — aggregated P&L, stats, and analytics."""
from datetime import datetime, timezone
from collections import defaultdict
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from sqlalchemy import func, case
from db.connection import get_session
from db.models import Trade

router = APIRouter(tags=["summary"])


@router.get("/summary")
def get_summary(date: Optional[str] = None):
    """Get P&L summary for today (or specified date YYYY-MM-DD)."""
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        q = session.query(Trade)

        if date:
            q = q.filter(func.date(Trade.entry_time) == date)
        else:
            # Default: today
            today = datetime.now(timezone.utc).date()
            q = q.filter(func.date(Trade.entry_time) == today)

        trades = q.all()

        open_trades = [t for t in trades if t.status == "open"]
        closed_trades = [t for t in trades if t.status == "closed"]
        errored_trades = [t for t in trades if t.status == "errored"]

        open_pnl = sum(float(t.pnl_usd or 0) for t in open_trades)
        closed_pnl = sum(float(t.pnl_usd or 0) for t in closed_trades)

        wins = sum(1 for t in closed_trades if t.exit_result == "WIN")
        losses = sum(1 for t in closed_trades if t.exit_result == "LOSS")
        scratches = sum(1 for t in closed_trades if t.exit_result == "SCRATCH")

        win_rate = round(wins / len(closed_trades) * 100, 1) if closed_trades else 0

        avg_win = 0
        avg_loss = 0
        win_pnls = [float(t.pnl_usd or 0) for t in closed_trades if t.exit_result == "WIN"]
        loss_pnls = [float(t.pnl_usd or 0) for t in closed_trades if t.exit_result == "LOSS"]
        if win_pnls:
            avg_win = round(sum(win_pnls) / len(win_pnls), 2)
        if loss_pnls:
            avg_loss = round(sum(loss_pnls) / len(loss_pnls), 2)

        session.close()
        return {
            "date": date or str(datetime.now(timezone.utc).date()),
            "total_trades": len(trades),
            "open_trades": len(open_trades),
            "closed_trades": len(closed_trades),
            "errored_trades": len(errored_trades),
            "open_pnl": round(open_pnl, 2),
            "closed_pnl": round(closed_pnl, 2),
            "total_pnl": round(open_pnl + closed_pnl, 2),
            "wins": wins, "losses": losses, "scratches": scratches,
            "win_rate": win_rate,
            "avg_win": avg_win, "avg_loss": avg_loss,
        }
    finally:
        session.close()


@router.get("/summary/by-ticker")
def get_summary_by_ticker():
    """P&L breakdown by ticker (all time)."""
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        trades = session.query(Trade).all()

        tickers = {}
        for t in trades:
            tk = t.ticker
            if tk not in tickers:
                tickers[tk] = {"total": 0, "wins": 0, "losses": 0, "pnl": 0}
            tickers[tk]["total"] += 1
            tickers[tk]["pnl"] += float(t.pnl_usd or 0)
            if t.exit_result == "WIN":
                tickers[tk]["wins"] += 1
            elif t.exit_result == "LOSS":
                tickers[tk]["losses"] += 1

        result = []
        for tk, data in sorted(tickers.items(), key=lambda x: x[1]["pnl"], reverse=True):
            closed = data["wins"] + data["losses"]
            result.append({
                "ticker": tk,
                "total_trades": data["total"],
                "wins": data["wins"], "losses": data["losses"],
                "total_pnl": round(data["pnl"], 2),
                "win_rate": round(data["wins"] / closed * 100, 1) if closed else 0,
            })

        session.close()
        return {"tickers": result}
    finally:
        session.close()

