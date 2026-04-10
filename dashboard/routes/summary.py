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


@router.get("/analytics")
def get_analytics(date: Optional[str] = None):
    """Comprehensive daily analytics for charts."""
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        q = session.query(Trade)
        if date:
            q = q.filter(func.date(Trade.entry_time) == date)
        else:
            # Default to most recent trading day with data
            latest = session.query(func.max(func.date(Trade.entry_time))).scalar()
            if latest:
                q = q.filter(func.date(Trade.entry_time) == latest)
                date = str(latest)
            else:
                date = str(datetime.now(timezone.utc).date())

        trades = q.all()
        closed = [t for t in trades if t.status == "closed"]
        open_trades = [t for t in trades if t.status == "open"]

        # ── P&L by ticker ──
        ticker_pnl = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0, "losses": 0})
        for t in trades:
            tk = t.ticker
            ticker_pnl[tk]["pnl"] += float(t.pnl_usd or 0)
            ticker_pnl[tk]["trades"] += 1
            if t.exit_result == "WIN": ticker_pnl[tk]["wins"] += 1
            elif t.exit_result == "LOSS": ticker_pnl[tk]["losses"] += 1

        pnl_by_ticker = [{"ticker": tk, **data} for tk, data in
                         sorted(ticker_pnl.items(), key=lambda x: x[1]["pnl"], reverse=True)]

        # ── P&L by hour (exit time) ──
        pnl_by_exit_hour = defaultdict(lambda: {"pnl": 0, "count": 0})
        for t in closed:
            if t.exit_time:
                h = t.exit_time.hour
                pnl_by_exit_hour[h]["pnl"] += float(t.pnl_usd or 0)
                pnl_by_exit_hour[h]["count"] += 1
        exit_hour_data = [{"hour": f"{h}:00", "pnl": round(d["pnl"], 2), "trades": d["count"]}
                          for h, d in sorted(pnl_by_exit_hour.items())]

        # ── P&L by hour (entry time) ──
        pnl_by_entry_hour = defaultdict(lambda: {"pnl": 0, "count": 0})
        for t in trades:
            if t.entry_time:
                h = t.entry_time.hour
                pnl_by_entry_hour[h]["pnl"] += float(t.pnl_usd or 0)
                pnl_by_entry_hour[h]["count"] += 1
        entry_hour_data = [{"hour": f"{h}:00", "pnl": round(d["pnl"], 2), "trades": d["count"]}
                           for h, d in sorted(pnl_by_entry_hour.items())]

        # ── Risk capital by hour (premium paid for open trades) ──
        risk_by_hour = defaultdict(float)
        for t in trades:
            if t.entry_time:
                h = t.entry_time.hour
                risk_by_hour[h] += float(t.entry_price or 0) * 100 * t.contracts_entered
        risk_data = [{"hour": f"{h}:00", "capital": round(v, 2)}
                     for h, v in sorted(risk_by_hour.items())]

        # ── P&L by contract type ──
        calls_pnl = sum(float(t.pnl_usd or 0) for t in trades if t.direction == "LONG")
        puts_pnl = sum(float(t.pnl_usd or 0) for t in trades if t.direction == "SHORT")
        calls_count = sum(1 for t in trades if t.direction == "LONG")
        puts_count = sum(1 for t in trades if t.direction == "SHORT")

        # ── Contracts open by hour ──
        contracts_by_hour = defaultdict(int)
        for t in trades:
            if t.entry_time:
                h = t.entry_time.hour
                contracts_by_hour[h] += t.contracts_entered
        contracts_data = [{"hour": f"{h}:00", "contracts": v}
                          for h, v in sorted(contracts_by_hour.items())]

        # ── Best and worst trades ──
        best_trade = max(closed, key=lambda t: float(t.pnl_usd or 0)) if closed else None
        worst_trade = min(closed, key=lambda t: float(t.pnl_usd or 0)) if closed else None

        def trade_summary(t):
            if not t: return None
            return {
                "ticker": t.ticker, "direction": t.direction, "symbol": t.symbol,
                "entry_price": float(t.entry_price or 0),
                "exit_price": float(t.exit_price or 0),
                "pnl_usd": round(float(t.pnl_usd or 0), 2),
                "pnl_pct": round(float(t.pnl_pct or 0) * 100, 1),
                "exit_reason": t.exit_reason,
            }

        # ── Cumulative P&L timeline ──
        cum_pnl = []
        running = 0
        for t in sorted(closed, key=lambda x: x.exit_time or datetime.min.replace(tzinfo=timezone.utc)):
            if t.exit_time:
                running += float(t.pnl_usd or 0)
                cum_pnl.append({
                    "time": t.exit_time.strftime("%H:%M"),
                    "pnl": round(running, 2),
                    "ticker": t.ticker,
                })

        # ── Win/Loss by exit reason ──
        reason_stats = defaultdict(lambda: {"count": 0, "pnl": 0})
        for t in closed:
            r = t.exit_reason or "Unknown"
            reason_stats[r]["count"] += 1
            reason_stats[r]["pnl"] += float(t.pnl_usd or 0)
        exit_reasons = [{"reason": r, "count": d["count"], "pnl": round(d["pnl"], 2)}
                        for r, d in sorted(reason_stats.items(), key=lambda x: x[1]["count"], reverse=True)]

        # ── Average hold time ──
        hold_times = []
        for t in closed:
            if t.entry_time and t.exit_time:
                mins = (t.exit_time - t.entry_time).total_seconds() / 60
                hold_times.append(mins)
        avg_hold = round(sum(hold_times) / len(hold_times), 1) if hold_times else 0

        session.close()
        return {
            "pnl_by_ticker": pnl_by_ticker,
            "pnl_by_exit_hour": exit_hour_data,
            "pnl_by_entry_hour": entry_hour_data,
            "risk_by_hour": risk_data,
            "contract_type": {
                "calls": {"pnl": round(calls_pnl, 2), "count": calls_count},
                "puts": {"pnl": round(puts_pnl, 2), "count": puts_count},
            },
            "contracts_by_hour": contracts_data,
            "best_trade": trade_summary(best_trade),
            "worst_trade": trade_summary(worst_trade),
            "cumulative_pnl": cum_pnl,
            "exit_reasons": exit_reasons,
            "avg_hold_minutes": avg_hold,
            "total_trades": len(trades),
            "total_closed": len(closed),
            "total_open": len(open_trades),
            "date": date,
            "available_dates": _get_trading_dates(session),
        }
    finally:
        session.close()


def _get_trading_dates(session) -> list[str]:
    """Get list of dates that have trade data."""
    rows = session.query(func.date(Trade.entry_time)).distinct().order_by(func.date(Trade.entry_time).desc()).all()
    return [str(r[0]) for r in rows if r[0]]
