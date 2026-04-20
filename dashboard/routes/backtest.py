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

# Sort whitelists — map UI key -> SQL column. Prevents injection.
_RUNS_SORT_COLS = {
    "id": "r.id", "name": "r.name", "status": "r.status",
    "strategy": "s.name", "strategy_name": "s.name",
    "trades": "r.total_trades", "total_trades": "r.total_trades",
    "win_rate": "r.win_rate", "total_pnl": "r.total_pnl",
    "profit_factor": "r.profit_factor", "max_drawdown": "r.max_drawdown",
    "avg_hold_min": "r.avg_hold_min", "created_at": "r.created_at",
    "period": "r.start_date", "start_date": "r.start_date",
    "end_date": "r.end_date",
}

_TRADES_SORT_COLS = {
    "id": "id", "ticker": "ticker", "symbol": "symbol",
    "direction": "direction", "entry_price": "entry_price",
    "exit_price": "exit_price", "pnl_usd": "pnl_usd", "pnl_pct": "pnl_pct",
    "hold_minutes": "hold_minutes", "entry_time": "entry_time",
    "exit_time": "exit_time", "signal_type": "signal_type",
    "exit_reason": "exit_reason", "exit_result": "exit_result",
}


def _sort_clause(sort: Optional[str], direction: Optional[str],
                 whitelist: dict, default: str) -> str:
    """Safe ORDER BY builder. Falls back to default when key not in whitelist."""
    col = whitelist.get(sort or "", None)
    if col is None:
        return default
    dir_sql = "DESC" if (direction or "").lower() == "desc" else "ASC"
    return f"{col} {dir_sql} NULLS LAST"


@router.get("/backtests")
def list_backtests(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    strategy_id: Optional[int] = None,
    status: Optional[str] = None,
    sort: Optional[str] = None,
    direction: Optional[str] = Query(None, pattern="^(asc|desc)$"),
):
    """Paginated backtest runs. Server-side sort via ?sort=&direction=
    on any column in _RUNS_SORT_COLS. Default order: newest first."""
    session = get_session()
    if session is None:
        raise HTTPException(503, "Database not available")
    try:
        q = _RUNS_SELECT
        clauses, params = [], {"lim": limit, "off": offset}
        if strategy_id is not None:
            clauses.append("r.strategy_id = :sid")
            params["sid"] = strategy_id
        if status is not None:
            clauses.append("r.status = :status")
            params["status"] = status
        if clauses:
            q += "WHERE " + " AND ".join(clauses) + " "
        count_q = "SELECT COUNT(*) FROM backtest_runs r " + (
            "WHERE " + " AND ".join(clauses) + " " if clauses else ""
        )
        total = session.execute(text(count_q), params).scalar() or 0

        q += "ORDER BY " + _sort_clause(sort, direction, _RUNS_SORT_COLS,
                                         "r.created_at DESC")
        q += " LIMIT :lim OFFSET :off"
        rows = session.execute(text(q), params).fetchall()
        return {
            "runs": [_run_to_dict(r) for r in rows],
            "total": int(total),
            "limit": limit,
            "offset": offset,
            "sort": sort,
            "direction": direction,
        }
    finally:
        session.close()


@router.get("/backtests/strategies")
def list_strategies_for_backtest():
    """Strategies dropdown source for the Run Backtest dialog.
    Declared BEFORE /backtests/{run_id} so the literal 'strategies' path
    wins against the int parameter."""
    from db.strategy_writer import list_strategies
    return {"strategies": list_strategies(enabled_only=True)}


# ── Analytics routes ─────────────────────────────────────
# CRITICAL: these MUST be declared before /backtests/{run_id}/trades
# and /backtests/{run_id}/analytics. FastAPI matches routes by
# registration order; the string segment "analytics" would otherwise
# be interpreted as a run_id int and fail with 422.

@router.get("/backtests/analytics/trades")
def backtest_analytics_trades(
    strategy: Optional[str] = None,
    ticker: Optional[str] = None,
    run_id: Optional[int] = None,
    outcome: Optional[str] = None,
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    status: Optional[str] = "completed",
    sort: Optional[str] = None,
    direction: Optional[str] = Query(None, pattern="^(asc|desc)$"),
):
    """Cross-run trade drill-down with server-side sort/filter.

    Powers the click-to-drill from charts/tables on the Analytics panel.
    Filters AND-joined. Server-side sort via ?sort=&direction=."""
    session = get_session()
    if session is None:
        raise HTTPException(503, "Database not available")
    try:
        where = ["1=1"]
        params: dict = {"lim": limit, "off": offset}
        if status:
            where.append("r.status = :status")
            params["status"] = status
        if strategy:
            where.append("s.name = :strategy")
            params["strategy"] = strategy
        if ticker:
            where.append("t.ticker = :ticker")
            params["ticker"] = ticker
        if run_id is not None:
            where.append("t.run_id = :rid")
            params["rid"] = run_id
        if outcome:
            where.append("t.exit_result = :outcome")
            params["outcome"] = outcome
        wh = " AND ".join(where)

        analytics_sort_cols = {k: f"t.{v}" for k, v in _TRADES_SORT_COLS.items()}
        analytics_sort_cols["strategy"] = "s.name"
        order_by = _sort_clause(sort, direction, analytics_sort_cols,
                                 "t.pnl_usd DESC")

        rows = session.execute(text(
            f"SELECT t.id, t.run_id, t.ticker, t.symbol, t.direction, "
            f"  t.entry_price, t.exit_price, t.pnl_usd, t.pnl_pct, "
            f"  t.entry_time, t.exit_time, t.hold_minutes, "
            f"  t.signal_type, t.exit_reason, t.exit_result, s.name AS strategy "
            f"FROM backtest_trades t "
            f"JOIN backtest_runs r ON r.id = t.run_id "
            f"JOIN strategies s ON s.strategy_id = r.strategy_id "
            f"WHERE {wh} "
            f"ORDER BY {order_by} "
            f"LIMIT :lim OFFSET :off"
        ), params).fetchall()

        total_row = session.execute(text(
            f"SELECT COUNT(*) FROM backtest_trades t "
            f"JOIN backtest_runs r ON r.id = t.run_id "
            f"JOIN strategies s ON s.strategy_id = r.strategy_id "
            f"WHERE {wh}"
        ), params).fetchone()
        total = int(total_row[0] or 0) if total_row else 0

        trades = [
            {
                "id": r[0], "run_id": r[1], "ticker": r[2], "symbol": r[3],
                "direction": r[4],
                "entry_price": float(r[5]) if r[5] is not None else None,
                "exit_price": float(r[6]) if r[6] is not None else None,
                "pnl_usd": float(r[7] or 0),
                "pnl_pct": float(r[8] or 0),
                "entry_time": r[9].isoformat() if r[9] else None,
                "exit_time": r[10].isoformat() if r[10] else None,
                "hold_minutes": float(r[11]) if r[11] is not None else None,
                "signal_type": r[12], "exit_reason": r[13],
                "exit_result": r[14], "strategy": r[15],
            }
            for r in rows
        ]
        return {"trades": trades, "total": total, "returned": len(trades)}
    finally:
        session.close()


@router.get("/backtests/{run_id}")
def get_backtest(
    run_id: int,
    include_trades: bool = Query(False, description="Include trades inline (capped at `trade_limit`). "
                                                     "Prefer /backtests/{id}/trades with pagination for "
                                                     "large runs."),
    trade_limit: int = Query(100, ge=1, le=2000),
):
    """Run + its config. Returns trade_count but NOT the full trade list by
    default — historical backtests can have 100s of trades each ~1.5KB
    with indicator enrichment. Fetch trades via /backtests/{id}/trades
    with pagination.

    For small runs or backward-compat callers that expected the inline
    'trades' array, pass ?include_trades=true (capped at trade_limit)."""
    session = get_session()
    if session is None:
        raise HTTPException(503, "Database not available")
    try:
        row = session.execute(
            text(_RUNS_SELECT + "WHERE r.id = :id"), {"id": run_id}
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"backtest {run_id} not found")

        cfg_row = session.execute(
            text("SELECT config, notes FROM backtest_runs WHERE id = :id"),
            {"id": run_id},
        ).fetchone()
        trade_count = session.execute(
            text("SELECT COUNT(*) FROM backtest_trades WHERE run_id = :id"),
            {"id": run_id},
        ).scalar() or 0

        run = _run_to_dict(row)
        run["config"] = cfg_row[0] if cfg_row else {}
        run["notes"] = cfg_row[1] if cfg_row else None

        result = {
            "run": run,
            "trade_count": int(trade_count),
            "trades": [],   # always present so old callers don't KeyError
        }
        if include_trades:
            result["trades"] = _fetch_trades_page(
                session, run_id, limit=trade_limit, offset=0,
                outcome=None,
            )
        return result
    finally:
        session.close()


def _fetch_trades_page(
    session,
    run_id: int,
    *,
    limit: int,
    offset: int,
    outcome: Optional[str],
    sort: Optional[str] = None,
    direction: Optional[str] = None,
) -> list[dict]:
    """Shared pagination helper used by both the inline-include path
    and the dedicated /trades endpoint."""
    clauses = ["run_id = :rid"]
    params: dict = {"rid": run_id, "lim": limit, "off": offset}
    if outcome:
        clauses.append("exit_result = :out")
        params["out"] = outcome

    order_by = _sort_clause(sort, direction, _TRADES_SORT_COLS,
                            "entry_time ASC")
    rows = session.execute(
        text(
            "SELECT id, ticker, symbol, direction, contracts, "
            "  entry_price, exit_price, pnl_pct, pnl_usd, peak_pnl_pct, "
            "  entry_time, exit_time, hold_minutes, "
            "  signal_type, exit_reason, exit_result, "
            "  tp_trailed, rolled "
            "FROM backtest_trades "
            f"WHERE {' AND '.join(clauses)} "
            f"ORDER BY {order_by} "
            "LIMIT :lim OFFSET :off"
        ),
        params,
    ).fetchall()

    return [
        {
            "id": r[0], "ticker": r[1], "symbol": r[2], "direction": r[3],
            "contracts": r[4],
            "entry_price": float(r[5]) if r[5] is not None else None,
            "exit_price": float(r[6]) if r[6] is not None else None,
            "pnl_pct": float(r[7]) if r[7] is not None else 0.0,
            "pnl_usd": float(r[8]) if r[8] is not None else 0.0,
            "peak_pnl_pct": float(r[9]) if r[9] is not None else 0.0,
            "entry_time": r[10].isoformat() if r[10] else None,
            "exit_time": r[11].isoformat() if r[11] else None,
            "hold_minutes": float(r[12]) if r[12] is not None else None,
            "signal_type": r[13], "exit_reason": r[14], "exit_result": r[15],
            "tp_trailed": bool(r[16]) if r[16] is not None else False,
            "rolled": bool(r[17]) if r[17] is not None else False,
        }
        for r in rows
    ]


@router.get("/backtests/{run_id}/trades")
def get_backtest_trades(
    run_id: int,
    limit: int = Query(100, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    outcome: Optional[str] = Query(None, description="Filter by WIN / LOSS / SCRATCH"),
    sort: Optional[str] = None,
    direction: Optional[str] = Query(None, pattern="^(asc|desc)$"),
):
    """Paginated trades for a run. Slimmer response than the inline
    detail — omits the big JSONB enrichment columns (entry_indicators,
    entry_context, signal_details). Fetch a single trade's full
    enrichment via /backtests/{id}/trades/{trade_id}."""
    session = get_session()
    if session is None:
        raise HTTPException(503, "Database not available")
    try:
        exists = session.execute(
            text("SELECT 1 FROM backtest_runs WHERE id = :id"), {"id": run_id}
        ).fetchone()
        if not exists:
            raise HTTPException(404, f"backtest {run_id} not found")

        total = session.execute(
            text(
                "SELECT COUNT(*) FROM backtest_trades WHERE run_id = :id"
                + (" AND exit_result = :out" if outcome else "")
            ),
            {"id": run_id, **({"out": outcome} if outcome else {})},
        ).scalar() or 0

        trades = _fetch_trades_page(
            session, run_id, limit=limit, offset=offset, outcome=outcome,
            sort=sort, direction=direction,
        )
        return {
            "trades": trades,
            "total": int(total),
            "limit": limit,
            "offset": offset,
            "outcome": outcome,
            "sort": sort,
            "direction": direction,
        }
    finally:
        session.close()


@router.get("/backtests/{run_id}/trades/{trade_id}")
def get_backtest_trade_detail(run_id: int, trade_id: int):
    """Full detail for a single trade including the JSONB enrichment
    (entry_indicators, exit_indicators, entry_context, signal_details).
    This is what the UI fetches on "expand row"."""
    session = get_session()
    if session is None:
        raise HTTPException(503, "Database not available")
    try:
        row = session.execute(
            text(
                "SELECT id, run_id, ticker, symbol, direction, contracts, "
                "  entry_price, exit_price, pnl_pct, pnl_usd, peak_pnl_pct, "
                "  slippage_paid, commission, "
                "  entry_time, exit_time, hold_minutes, "
                "  signal_type, exit_reason, exit_result, "
                "  tp_level, sl_level, dynamic_sl_pct, tp_trailed, rolled, "
                "  entry_indicators, exit_indicators, entry_context, signal_details, "
                "  sec_type, multiplier, exchange, currency, underlying, strategy_config "
                "FROM backtest_trades WHERE id = :tid AND run_id = :rid"
            ),
            {"tid": trade_id, "rid": run_id},
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"trade {trade_id} not found in run {run_id}")
        return {
            "id": row[0], "run_id": row[1],
            "ticker": row[2], "symbol": row[3], "direction": row[4],
            "contracts": row[5],
            "entry_price": float(row[6]) if row[6] is not None else None,
            "exit_price": float(row[7]) if row[7] is not None else None,
            "pnl_pct": float(row[8]) if row[8] is not None else 0.0,
            "pnl_usd": float(row[9]) if row[9] is not None else 0.0,
            "peak_pnl_pct": float(row[10]) if row[10] is not None else 0.0,
            "slippage_paid": float(row[11]) if row[11] is not None else 0.0,
            "commission": float(row[12]) if row[12] is not None else 0.0,
            "entry_time": row[13].isoformat() if row[13] else None,
            "exit_time": row[14].isoformat() if row[14] else None,
            "hold_minutes": float(row[15]) if row[15] is not None else None,
            "signal_type": row[16], "exit_reason": row[17], "exit_result": row[18],
            "tp_level": float(row[19]) if row[19] is not None else None,
            "sl_level": float(row[20]) if row[20] is not None else None,
            "dynamic_sl_pct": float(row[21]) if row[21] is not None else None,
            "tp_trailed": bool(row[22]) if row[22] is not None else False,
            "rolled": bool(row[23]) if row[23] is not None else False,
            "entry_indicators": row[24] or {},
            "exit_indicators": row[25] or {},
            "entry_context": row[26] or {},
            "signal_details": row[27] or {},
            "sec_type": row[28],
            "multiplier": row[29],
            "exchange": row[30],
            "currency": row[31],
            "underlying": row[32],
            "strategy_config": row[33] or {},
        }
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


@router.get("/backtests/analytics/cross_run")
def backtest_cross_run_analytics(
    strategy_id: Optional[int] = None,
    status: Optional[str] = "completed",
    limit_runs: int = Query(500, ge=1, le=2000),
):
    """Cross-run analytics — aggregate backtest_trades across many runs.

    Returns four rollups:
      - by_ticker_strategy: (ticker, strategy_name) → trades, pnl, win_rate, runs
      - by_strategy:        strategy → trades, pnl, win_rate, runs
      - by_ticker:          ticker → trades, pnl, win_rate, strategies
      - top_runs:           top/bottom runs by total P&L (quick config comparison)
    Used by the Analytics sub-tab on the Backtest page to slice/dice
    the entire corpus of backtests.
    """
    session = get_session()
    if session is None:
        raise HTTPException(503, "Database not available")
    try:
        # Identify qualifying runs
        where = ["1=1"]
        params: dict = {"lim_runs": limit_runs}
        if strategy_id is not None:
            where.append("r.strategy_id = :sid")
            params["sid"] = strategy_id
        if status:
            where.append("r.status = :status")
            params["status"] = status
        wh = " AND ".join(where)

        run_rows = session.execute(text(
            f"SELECT r.id, r.strategy_id, s.name "
            f"FROM backtest_runs r JOIN strategies s ON s.strategy_id = r.strategy_id "
            f"WHERE {wh} "
            f"ORDER BY r.created_at DESC LIMIT :lim_runs"
        ), params).fetchall()

        if not run_rows:
            return {
                "by_ticker_strategy": [], "by_strategy": [],
                "by_ticker": [], "top_runs": [], "bottom_runs": [],
                "run_count": 0, "trade_count": 0,
            }

        run_ids = [r[0] for r in run_rows]
        strategy_by_run = {r[0]: r[2] for r in run_rows}

        # Pull aggregated trades. Group DB-side to keep payload small.
        by_ts_rows = session.execute(text(
            "SELECT t.ticker, s.name AS strategy, "
            "  COUNT(*) AS trades, "
            "  SUM(t.pnl_usd) AS pnl, "
            "  SUM(CASE WHEN t.exit_result='WIN' THEN 1 ELSE 0 END) AS wins, "
            "  SUM(CASE WHEN t.exit_result IN ('WIN','LOSS') THEN 1 ELSE 0 END) AS decided, "
            "  COUNT(DISTINCT t.run_id) AS runs "
            "FROM backtest_trades t "
            "JOIN backtest_runs r ON r.id = t.run_id "
            "JOIN strategies s ON s.strategy_id = r.strategy_id "
            "WHERE t.run_id = ANY(:rids) "
            "GROUP BY t.ticker, s.name"
        ), {"rids": run_ids}).fetchall()

        by_ticker_strategy = [
            {
                "ticker": r[0], "strategy": r[1],
                "trades": int(r[2] or 0),
                "pnl": round(float(r[3] or 0), 2),
                "wins": int(r[4] or 0),
                "decided": int(r[5] or 0),
                "win_rate": round(100.0 * (r[4] or 0) / r[5], 1) if r[5] else 0.0,
                "runs": int(r[6] or 0),
            }
            for r in by_ts_rows
        ]
        by_ticker_strategy.sort(key=lambda x: x["pnl"], reverse=True)

        # Rollups from the detailed rows (in-Python to avoid 3 more queries)
        from collections import defaultdict
        strat_agg = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0, "decided": 0, "runs": set()})
        ticker_agg = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0, "decided": 0, "strategies": set()})
        for r in by_ts_rows:
            ticker, strat = r[0], r[1]
            trades, pnl, wins, decided, runs = r[2], r[3], r[4], r[5], r[6]
            s = strat_agg[strat]
            s["trades"] += int(trades or 0); s["pnl"] += float(pnl or 0)
            s["wins"] += int(wins or 0); s["decided"] += int(decided or 0)
            s["runs"].add(strat)  # placeholder; real count below
            t = ticker_agg[ticker]
            t["trades"] += int(trades or 0); t["pnl"] += float(pnl or 0)
            t["wins"] += int(wins or 0); t["decided"] += int(decided or 0)
            t["strategies"].add(strat)

        # Real per-strategy run counts
        runs_per_strat = defaultdict(int)
        for rid, sid, sname in run_rows:
            runs_per_strat[sname] += 1

        by_strategy = [
            {
                "strategy": k,
                "trades": v["trades"],
                "pnl": round(v["pnl"], 2),
                "wins": v["wins"],
                "decided": v["decided"],
                "win_rate": round(100.0 * v["wins"] / v["decided"], 1) if v["decided"] else 0.0,
                "runs": runs_per_strat.get(k, 0),
            }
            for k, v in strat_agg.items()
        ]
        by_strategy.sort(key=lambda x: x["pnl"], reverse=True)

        by_ticker = [
            {
                "ticker": k,
                "trades": v["trades"],
                "pnl": round(v["pnl"], 2),
                "wins": v["wins"],
                "decided": v["decided"],
                "win_rate": round(100.0 * v["wins"] / v["decided"], 1) if v["decided"] else 0.0,
                "strategies": sorted(v["strategies"]),
            }
            for k, v in ticker_agg.items()
        ]
        by_ticker.sort(key=lambda x: x["pnl"], reverse=True)

        # Top/bottom runs by P&L
        run_summary_rows = session.execute(text(
            "SELECT r.id, r.name, s.name, r.tickers, r.total_trades, "
            "  r.total_pnl, r.win_rate, r.profit_factor, r.max_drawdown, r.created_at "
            "FROM backtest_runs r JOIN strategies s ON s.strategy_id = r.strategy_id "
            "WHERE r.id = ANY(:rids) AND r.total_trades > 0 "
            "ORDER BY r.total_pnl DESC"
        ), {"rids": run_ids}).fetchall()

        def _run_row_to_dict(r):
            return {
                "id": r[0], "name": r[1], "strategy": r[2],
                "tickers": list(r[3] or []),
                "trades": int(r[4] or 0),
                "pnl": round(float(r[5] or 0), 2),
                "win_rate": round(float(r[6] or 0), 1),
                "profit_factor": float(r[7]) if r[7] is not None else None,
                "max_drawdown": round(float(r[8] or 0), 2),
                "created_at": r[9].isoformat() if r[9] else None,
            }

        top_runs = [_run_row_to_dict(r) for r in run_summary_rows[:20]]
        bottom_runs = [_run_row_to_dict(r) for r in run_summary_rows[-20:][::-1]]

        trade_count = sum(x["trades"] for x in by_ticker_strategy)

        return {
            "by_ticker_strategy": by_ticker_strategy,
            "by_strategy": by_strategy,
            "by_ticker": by_ticker,
            "top_runs": top_runs,
            "bottom_runs": bottom_runs,
            "run_count": len(run_rows),
            "trade_count": trade_count,
        }
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


