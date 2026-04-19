"""Backtest API — CRUD for backtest_runs + launch endpoint.

Runs spawn as a subprocess on the host via bot_manager (same sidecar
pattern as /run-tests). The engine writes rows incrementally, so the UI
polls /api/backtests for status updates.
"""
from __future__ import annotations

import os
from datetime import date
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Body

from db.connection import get_session
from sqlalchemy import text

router = APIRouter(tags=["backtests"])

SIDECAR_URL = os.getenv("BOT_SIDECAR_URL", "http://host.docker.internal:9000")


# ── Helpers ──────────────────────────────────────────────

def _run_to_dict(row) -> dict:
    """Convert a backtest_runs row tuple to a dict."""
    return {
        "id": row[0], "name": row[1], "status": row[2],
        "strategy_id": row[3],
        "strategy_name": row[4] if len(row) > 4 else None,
        "tickers": list(row[5] or []) if len(row) > 5 else [],
        "start_date": row[6].isoformat() if len(row) > 6 and row[6] else None,
        "end_date": row[7].isoformat() if len(row) > 7 and row[7] else None,
        "total_trades": row[8] if len(row) > 8 else 0,
        "wins": row[9] if len(row) > 9 else 0,
        "losses": row[10] if len(row) > 10 else 0,
        "scratches": row[11] if len(row) > 11 else 0,
        "total_pnl": float(row[12]) if len(row) > 12 and row[12] is not None else 0.0,
        "win_rate": float(row[13]) if len(row) > 13 and row[13] is not None else 0.0,
        "avg_win": float(row[14]) if len(row) > 14 and row[14] is not None else 0.0,
        "avg_loss": float(row[15]) if len(row) > 15 and row[15] is not None else 0.0,
        "max_drawdown": float(row[16]) if len(row) > 16 and row[16] is not None else 0.0,
        "sharpe_ratio": float(row[17]) if len(row) > 17 and row[17] is not None else None,
        "profit_factor": float(row[18]) if len(row) > 18 and row[18] is not None else None,
        "avg_hold_min": float(row[19]) if len(row) > 19 and row[19] is not None else 0.0,
        "duration_sec": float(row[20]) if len(row) > 20 and row[20] is not None else None,
        "started_at": row[21].isoformat() if len(row) > 21 and row[21] else None,
        "completed_at": row[22].isoformat() if len(row) > 22 and row[22] else None,
        "created_at": row[23].isoformat() if len(row) > 23 and row[23] else None,
        "error_message": row[24] if len(row) > 24 else None,
    }


_RUNS_SELECT = (
    "SELECT r.id, r.name, r.status, r.strategy_id, s.name AS strategy_name, "
    "  r.tickers, r.start_date, r.end_date, "
    "  r.total_trades, r.wins, r.losses, r.scratches, r.total_pnl, r.win_rate, "
    "  r.avg_win, r.avg_loss, r.max_drawdown, r.sharpe_ratio, r.profit_factor, "
    "  r.avg_hold_min, r.duration_sec, r.started_at, r.completed_at, r.created_at, "
    "  r.error_message "
    "FROM backtest_runs r JOIN strategies s ON s.strategy_id = r.strategy_id "
)


# ── Routes ───────────────────────────────────────────────

@router.get("/backtests")
def list_backtests(
    limit: int = Query(50, ge=1, le=500),
    strategy_id: Optional[int] = None,
    status: Optional[str] = None,
):
    """Most recent backtest runs first."""
    session = get_session()
    if session is None:
        raise HTTPException(503, "Database not available")
    try:
        q = _RUNS_SELECT
        clauses, params = [], {"lim": limit}
        if strategy_id is not None:
            clauses.append("r.strategy_id = :sid")
            params["sid"] = strategy_id
        if status is not None:
            clauses.append("r.status = :status")
            params["status"] = status
        if clauses:
            q += "WHERE " + " AND ".join(clauses) + " "
        q += "ORDER BY r.created_at DESC LIMIT :lim"
        rows = session.execute(text(q), params).fetchall()
        return {"runs": [_run_to_dict(r) for r in rows], "total": len(rows)}
    finally:
        session.close()


@router.get("/backtests/strategies")
def list_strategies_for_backtest():
    """Strategies dropdown source for the Run Backtest dialog.
    Declared BEFORE /backtests/{run_id} so the literal 'strategies' path
    wins against the int parameter."""
    from db.strategy_writer import list_strategies
    return {"strategies": list_strategies(enabled_only=True)}


@router.get("/backtests/{run_id}")
def get_backtest(run_id: int):
    """Run + its config + trades (drill-down)."""
    session = get_session()
    if session is None:
        raise HTTPException(503, "Database not available")
    try:
        row = session.execute(
            text(_RUNS_SELECT + "WHERE r.id = :id"), {"id": run_id}
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"backtest {run_id} not found")

        # Fetch config separately (not in the base SELECT)
        cfg_row = session.execute(
            text("SELECT config, notes FROM backtest_runs WHERE id = :id"),
            {"id": run_id},
        ).fetchone()

        run = _run_to_dict(row)
        run["config"] = cfg_row[0] if cfg_row else {}
        run["notes"] = cfg_row[1] if cfg_row else None

        # Trades
        from backtest_engine.writer import get_run_trades
        trades = get_run_trades(run_id)
        return {"run": run, "trades": trades, "trade_count": len(trades)}
    finally:
        session.close()


@router.get("/backtests/{run_id}/analytics")
def backtest_analytics(run_id: int):
    """Aggregated views for the charts — P&L by ticker, exit reason
    distribution, signal-type win rates, cumulative-P&L curve, hold-time
    histogram. Everything the UI needs for the drill-down tab."""
    session = get_session()
    if session is None:
        raise HTTPException(503, "Database not available")
    try:
        # Sanity — run exists
        exists = session.execute(
            text("SELECT 1 FROM backtest_runs WHERE id = :id"),
            {"id": run_id},
        ).fetchone()
        if not exists:
            raise HTTPException(404, f"backtest {run_id} not found")

        # P&L by ticker
        pnl_by_ticker = session.execute(text(
            "SELECT ticker, COUNT(*), SUM(pnl_usd), "
            "       SUM(CASE WHEN exit_result='WIN' THEN 1 ELSE 0 END) "
            "FROM backtest_trades WHERE run_id = :id "
            "GROUP BY ticker ORDER BY SUM(pnl_usd) DESC"
        ), {"id": run_id}).fetchall()

        # Exit reason distribution
        by_reason = session.execute(text(
            "SELECT exit_reason, COUNT(*), SUM(pnl_usd) "
            "FROM backtest_trades WHERE run_id = :id "
            "GROUP BY exit_reason ORDER BY COUNT(*) DESC"
        ), {"id": run_id}).fetchall()

        # Signal-type win rate
        by_signal = session.execute(text(
            "SELECT signal_type, COUNT(*), "
            "  SUM(CASE WHEN exit_result='WIN' THEN 1 ELSE 0 END), "
            "  SUM(pnl_usd) "
            "FROM backtest_trades WHERE run_id = :id "
            "GROUP BY signal_type ORDER BY COUNT(*) DESC"
        ), {"id": run_id}).fetchall()

        # Cumulative P&L curve (ordered by exit_time)
        curve = session.execute(text(
            "SELECT exit_time, pnl_usd FROM backtest_trades "
            "WHERE run_id = :id AND exit_time IS NOT NULL "
            "ORDER BY exit_time"
        ), {"id": run_id}).fetchall()
        cum = []
        running = 0.0
        for exit_time, pnl in curve:
            running += float(pnl or 0)
            cum.append({
                "t": exit_time.isoformat() if exit_time else None,
                "cum_pnl": round(running, 2),
            })

        # Hold-time histogram (10-minute buckets capped at 180)
        hist = session.execute(text(
            "SELECT FLOOR(hold_minutes / 10) * 10 AS bucket, COUNT(*) "
            "FROM backtest_trades WHERE run_id = :id AND hold_minutes IS NOT NULL "
            "GROUP BY bucket ORDER BY bucket"
        ), {"id": run_id}).fetchall()

        # Day-of-week breakdown
        by_dow = session.execute(text(
            "SELECT TO_CHAR(entry_time, 'Dy') AS dow, "
            "  COUNT(*), SUM(pnl_usd), "
            "  SUM(CASE WHEN exit_result='WIN' THEN 1 ELSE 0 END) "
            "FROM backtest_trades WHERE run_id = :id "
            "GROUP BY dow "
            "ORDER BY MIN(entry_time)"
        ), {"id": run_id}).fetchall()

        return {
            "pnl_by_ticker": [
                {"ticker": r[0], "trades": r[1],
                 "pnl": float(r[2] or 0), "wins": r[3]} for r in pnl_by_ticker
            ],
            "by_reason": [
                {"reason": r[0] or "—", "count": r[1], "pnl": float(r[2] or 0)}
                for r in by_reason
            ],
            "by_signal": [
                {"signal": r[0] or "—", "count": r[1], "wins": r[2],
                 "pnl": float(r[3] or 0)}
                for r in by_signal
            ],
            "cum_pnl": cum,
            "hold_time_hist": [
                {"bucket_min": int(r[0] or 0), "count": r[1]} for r in hist
            ],
            "by_day_of_week": [
                {"dow": r[0], "count": r[1],
                 "pnl": float(r[2] or 0), "wins": r[3]}
                for r in by_dow
            ],
        }
    finally:
        session.close()


@router.get("/backtests/{run_id}/feature_analysis")
def backtest_feature_analysis(run_id: int):
    """Data-science layer: correlate entry_indicators with outcomes.

    For each numeric feature found in entry_indicators, compute:
      - mean value for WIN trades vs LOSS trades
      - quartile-bucketed win rate (so we can see e.g. "trades with RSI
        in Q1 win 60% vs overall 45%")
    This is the foundation for strategy-optimization analytics.
    """
    session = get_session()
    if session is None:
        raise HTTPException(503, "Database not available")
    try:
        # Pull all trades with their indicators + outcome
        rows = session.execute(text(
            "SELECT pnl_usd, exit_result, entry_indicators "
            "FROM backtest_trades WHERE run_id = :id"
        ), {"id": run_id}).fetchall()

        if not rows:
            return {"features": [], "total_trades": 0}

        # Collect all numeric feature keys
        feature_values: dict[str, list[tuple[float, str]]] = {}
        for pnl_usd, exit_result, indicators in rows:
            if not isinstance(indicators, dict):
                continue
            for key, val in indicators.items():
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    feature_values.setdefault(key, []).append(
                        (float(val), exit_result or ("WIN" if pnl_usd > 0 else
                                                     "LOSS" if pnl_usd < 0 else "SCRATCH"))
                    )

        features = []
        for key, pairs in feature_values.items():
            wins = [v for v, r in pairs if r == "WIN"]
            losses = [v for v, r in pairs if r == "LOSS"]
            if not wins and not losses:
                continue
            win_mean = sum(wins) / len(wins) if wins else None
            loss_mean = sum(losses) / len(losses) if losses else None

            # Quartile bucket win rate
            sorted_vals = sorted(v for v, _ in pairs)
            n = len(sorted_vals)
            if n >= 8:
                q1 = sorted_vals[n // 4]
                q2 = sorted_vals[n // 2]
                q3 = sorted_vals[3 * n // 4]
                buckets = [
                    {"label": f"≤ {q1:.3g}",        "lo": None, "hi": q1},
                    {"label": f"{q1:.3g} – {q2:.3g}", "lo": q1,   "hi": q2},
                    {"label": f"{q2:.3g} – {q3:.3g}", "lo": q2,   "hi": q3},
                    {"label": f"> {q3:.3g}",        "lo": q3,   "hi": None},
                ]
                for b in buckets:
                    in_b = [r for v, r in pairs
                            if (b["lo"] is None or v > b["lo"])
                            and (b["hi"] is None or v <= b["hi"])]
                    decided = sum(1 for r in in_b if r in ("WIN", "LOSS"))
                    wins_b = sum(1 for r in in_b if r == "WIN")
                    b["count"] = len(in_b)
                    b["win_rate"] = round(100.0 * wins_b / decided, 1) if decided else 0.0
                    # Remove the numeric lo/hi before returning (display-only)
                    b.pop("lo", None)
                    b.pop("hi", None)
            else:
                buckets = []

            features.append({
                "feature": key,
                "n_total": len(pairs),
                "n_wins": len(wins),
                "n_losses": len(losses),
                "win_mean": round(win_mean, 4) if win_mean is not None else None,
                "loss_mean": round(loss_mean, 4) if loss_mean is not None else None,
                "edge": round(win_mean - loss_mean, 4)
                        if win_mean is not None and loss_mean is not None else None,
                "quartile_win_rates": buckets,
            })

        # Sort by absolute edge (biggest win-vs-loss mean gap) so the
        # most informative features show up first in the UI
        features.sort(key=lambda f: abs(f.get("edge") or 0), reverse=True)

        return {"features": features, "total_trades": len(rows)}
    finally:
        session.close()


@router.delete("/backtests/{run_id}")
def delete_backtest(run_id: int):
    from backtest_engine.writer import delete_run
    if not delete_run(run_id):
        raise HTTPException(500, "delete failed")
    return {"deleted": run_id}


# ── Launch ───────────────────────────────────────────────

ALLOWED_STATUSES = {"pending", "running", "completed", "failed"}


@router.post("/backtests/launch")
async def launch_backtest(payload: dict = Body(default={})):
    """Kick off a backtest on the host via bot_manager.

    Body: {
        "name": "optional",
        "strategy": "ict",               # name from strategies table
        "tickers": ["QQQ", "SPY"],
        "start_date": "2026-03-01",
        "end_date":   "2026-04-01",
        "config": { "profit_target": 1.0, ... }  # optional override
    }

    Returns 202 with the sidecar's ack. The engine writes rows to
    backtest_runs as it progresses; poll /backtests?limit=1 to detect
    the new run and then GET /backtests/{id} for details.
    """
    # Basic validation — full validation happens sidecar/engine-side
    tickers = payload.get("tickers")
    if not tickers or not isinstance(tickers, list):
        raise HTTPException(400, "tickers (list) is required")
    if not payload.get("start_date") or not payload.get("end_date"):
        raise HTTPException(400, "start_date and end_date are required (YYYY-MM-DD)")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{SIDECAR_URL}/run-backtest",
                json={
                    "name": payload.get("name"),
                    "strategy": payload.get("strategy", "ict"),
                    "tickers": tickers,
                    "start_date": payload["start_date"],
                    "end_date": payload["end_date"],
                    "config": payload.get("config", {}),
                },
            )
            data = resp.json()
            if resp.status_code >= 400:
                raise HTTPException(resp.status_code, data.get("error", "sidecar error"))
            return data
    except httpx.ConnectError:
        raise HTTPException(
            503,
            "Bot manager sidecar not running. Start it: python bot_manager.py"
        )
    except httpx.TimeoutException:
        raise HTTPException(504, "Sidecar timed out starting backtest")


