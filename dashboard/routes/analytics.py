"""
Analytics API — uses PostgreSQL views for all chart data.
Supports date range filtering and drill-down into individual trades.
Leverages PostgreSQL analytics: window functions, percentiles, aggregates.
"""
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from sqlalchemy import text
from db.connection import get_session

router = APIRouter(tags=["analytics"])


def _rows_to_dicts(result) -> list[dict]:
    """Convert SQLAlchemy result rows to list of dicts."""
    columns = result.keys()
    return [dict(zip(columns, row)) for row in result.fetchall()]


def _serialize(rows: list[dict]) -> list[dict]:
    """Make all values JSON-serializable."""
    result = []
    for row in rows:
        clean = {}
        for k, v in row.items():
            if hasattr(v, 'isoformat'):
                clean[k] = v.isoformat()
            elif v is None:
                clean[k] = None
            else:
                try:
                    clean[k] = float(v) if isinstance(v, (int, float)) or (hasattr(v, '__float__')) else str(v)
                except (ValueError, TypeError):
                    clean[k] = str(v)
        result.append(clean)
    return result


@router.get("/analytics")
def get_analytics(
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    """
    Comprehensive analytics using PostgreSQL views.
    Defaults to most recent trading day if no dates specified.
    """
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        conn = session.connection()

        # Get available trading dates
        dates_result = conn.execute(text(
            "SELECT DISTINCT trade_date FROM v_trades_analytics ORDER BY trade_date DESC"
        ))
        available_dates = [str(r[0]) for r in dates_result.fetchall()]

        if not available_dates:
            return {"error": "No trade data available", "available_dates": []}

        # Default to most recent day
        if not start:
            start = available_dates[0]
        if not end:
            end = start

        date_filter = "trade_date BETWEEN :start AND :end"
        params = {"start": start, "end": end}

        # ── P&L by ticker (with PostgreSQL rank) ──
        pnl_by_ticker = _serialize(_rows_to_dicts(conn.execute(text(f"""
            SELECT ticker,
                   SUM(total_trades)::int AS trades,
                   SUM(wins)::int AS wins,
                   SUM(losses)::int AS losses,
                   ROUND(SUM(total_pnl)::numeric, 2) AS pnl,
                   ROUND(AVG(avg_hold_min)::numeric, 1) AS avg_hold,
                   RANK() OVER (ORDER BY SUM(total_pnl) DESC) AS rank
            FROM v_pnl_by_ticker WHERE {date_filter}
            GROUP BY ticker ORDER BY pnl DESC
        """), params)))

        # ── P&L by exit hour ──
        pnl_by_exit_hour = _serialize(_rows_to_dicts(conn.execute(text(f"""
            SELECT hour, SUM(trades)::int AS trades, ROUND(SUM(pnl)::numeric, 2) AS pnl
            FROM v_pnl_by_exit_hour WHERE {date_filter}
            GROUP BY hour ORDER BY hour
        """), params)))

        # ── P&L by entry hour ──
        pnl_by_entry_hour = _serialize(_rows_to_dicts(conn.execute(text(f"""
            SELECT hour, SUM(trades)::int AS trades, ROUND(SUM(pnl)::numeric, 2) AS pnl
            FROM v_pnl_by_entry_hour WHERE {date_filter}
            GROUP BY hour ORDER BY hour
        """), params)))

        # ── Risk capital by hour ──
        risk_by_hour = _serialize(_rows_to_dicts(conn.execute(text(f"""
            SELECT hour, ROUND(SUM(capital)::numeric, 2) AS capital, SUM(contracts)::int AS contracts
            FROM v_risk_by_hour WHERE {date_filter}
            GROUP BY hour ORDER BY hour
        """), params)))

        # ── Contracts by hour ──
        contracts_by_hour = _serialize(_rows_to_dicts(conn.execute(text(f"""
            SELECT hour, SUM(contracts)::int AS contracts
            FROM v_contracts_by_hour WHERE {date_filter}
            GROUP BY hour ORDER BY hour
        """), params)))

        # ── P&L by contract type ──
        contract_type = _serialize(_rows_to_dicts(conn.execute(text(f"""
            SELECT contract_type, SUM(trades)::int AS trades, ROUND(SUM(pnl)::numeric, 2) AS pnl,
                   SUM(wins)::int AS wins, SUM(losses)::int AS losses
            FROM v_pnl_by_contract_type WHERE {date_filter}
            GROUP BY contract_type
        """), params)))

        # ── Exit reasons ──
        exit_reasons = _serialize(_rows_to_dicts(conn.execute(text(f"""
            SELECT exit_reason AS reason, SUM(trades)::int AS count, ROUND(SUM(pnl)::numeric, 2) AS pnl
            FROM v_pnl_by_exit_reason WHERE {date_filter}
            GROUP BY exit_reason ORDER BY count DESC
        """), params)))

        # ── Best and worst trades (PostgreSQL window functions) ──
        extremes = _rows_to_dicts(conn.execute(text(f"""
            SELECT * FROM (
                SELECT ticker, direction, symbol, entry_price, exit_price,
                       ROUND(pnl_usd::numeric, 2) AS pnl_usd,
                       ROUND((pnl_pct * 100)::numeric, 1) AS pnl_pct,
                       exit_reason, hold_minutes,
                       entry_time_pt, exit_time_pt,
                       ROW_NUMBER() OVER (ORDER BY pnl_usd DESC) AS best_rank,
                       ROW_NUMBER() OVER (ORDER BY pnl_usd ASC) AS worst_rank
                FROM v_trades_analytics
                WHERE {date_filter} AND status = 'closed'
            ) ranked WHERE best_rank = 1 OR worst_rank = 1
        """), params))
        best_trade = None
        worst_trade = None
        for r in extremes:
            clean = _serialize([r])[0]
            if r.get("best_rank") == 1:
                best_trade = clean
            if r.get("worst_rank") == 1:
                worst_trade = clean

        # ── Cumulative P&L (running sum via window function) ──
        cumulative = _serialize(_rows_to_dicts(conn.execute(text(f"""
            SELECT
                TO_CHAR(exit_time_pt, 'HH24:MI') AS time,
                ticker,
                ROUND(pnl_usd::numeric, 2) AS trade_pnl,
                ROUND(SUM(pnl_usd) OVER (ORDER BY exit_time_pt)::numeric, 2) AS cumulative_pnl
            FROM v_trades_analytics
            WHERE {date_filter} AND status = 'closed' AND exit_time_pt IS NOT NULL
            ORDER BY exit_time_pt
        """), params)))

        # ── Summary stats (PostgreSQL aggregates + percentiles) ──
        stats = _rows_to_dicts(conn.execute(text(f"""
            SELECT
                COUNT(*)::int AS total_trades,
                COUNT(*) FILTER (WHERE status = 'closed')::int AS total_closed,
                COUNT(*) FILTER (WHERE status = 'open')::int AS total_open,
                ROUND(COALESCE(SUM(pnl_usd), 0)::numeric, 2) AS total_pnl,
                ROUND(AVG(hold_minutes) FILTER (WHERE status = 'closed')::numeric, 1) AS avg_hold,
                ROUND(COALESCE(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pnl_usd) FILTER (WHERE status = 'closed'), 0)::numeric, 2) AS median_pnl,
                ROUND(COALESCE(STDDEV(pnl_usd) FILTER (WHERE status = 'closed'), 0)::numeric, 2) AS pnl_stddev,
                ROUND(COALESCE(MAX(pnl_usd) FILTER (WHERE status = 'closed'), 0)::numeric, 2) AS max_win,
                ROUND(COALESCE(MIN(pnl_usd) FILTER (WHERE status = 'closed'), 0)::numeric, 2) AS max_loss,
                ROUND(SUM(risk_capital)::numeric, 2) AS total_risk_capital,
                COUNT(DISTINCT ticker)::int AS unique_tickers
            FROM v_trades_analytics
            WHERE {date_filter}
        """), params))
        summary = _serialize(stats)[0] if stats else {}

        # ── Streaks (consecutive wins/losses via PostgreSQL) ──
        streaks = _rows_to_dicts(conn.execute(text(f"""
            WITH numbered AS (
                SELECT exit_result,
                       ROW_NUMBER() OVER (ORDER BY exit_time_pt) AS rn,
                       ROW_NUMBER() OVER (PARTITION BY exit_result ORDER BY exit_time_pt) AS grp
                FROM v_trades_analytics
                WHERE {date_filter} AND status = 'closed' AND exit_result IN ('WIN', 'LOSS')
            ),
            streak_groups AS (
                SELECT exit_result, COUNT(*) AS streak_len
                FROM numbered
                GROUP BY exit_result, rn - grp
            )
            SELECT
                COALESCE(MAX(streak_len) FILTER (WHERE exit_result = 'WIN'), 0)::int AS max_win_streak,
                COALESCE(MAX(streak_len) FILTER (WHERE exit_result = 'LOSS'), 0)::int AS max_loss_streak
            FROM streak_groups
        """), params))
        streak_data = _serialize(streaks)[0] if streaks else {"max_win_streak": 0, "max_loss_streak": 0}

        # ── P&L by day of week ──
        pnl_by_day_of_week = _serialize(_rows_to_dicts(conn.execute(text(f"""
            SELECT day_name, day_num,
                   SUM(trades)::int AS trades,
                   SUM(wins)::int AS wins,
                   SUM(losses)::int AS losses,
                   ROUND(SUM(total_pnl)::numeric, 2) AS pnl,
                   ROUND(AVG(win_rate)::numeric, 1) AS win_rate
            FROM v_pnl_by_day_of_week WHERE {date_filter}
            GROUP BY day_name, day_num ORDER BY day_num
        """), params)))

        # ── P&L by signal type ──
        pnl_by_signal_type = _serialize(_rows_to_dicts(conn.execute(text(f"""
            SELECT signal_type,
                   SUM(trades)::int AS trades,
                   SUM(wins)::int AS wins,
                   SUM(losses)::int AS losses,
                   ROUND(SUM(total_pnl)::numeric, 2) AS pnl,
                   ROUND(AVG(win_rate)::numeric, 1) AS win_rate
            FROM v_pnl_by_signal_type WHERE {date_filter}
            GROUP BY signal_type ORDER BY trades DESC
        """), params)))

        # ── Hold time distribution (bucketed into 5-min intervals) ──
        hold_time_dist = _serialize(_rows_to_dicts(conn.execute(text(f"""
            SELECT
                FLOOR(hold_minutes / 5) * 5 AS bucket,
                COUNT(*)::int AS trades,
                ROUND(SUM(pnl_usd)::numeric, 2) AS pnl,
                COUNT(*) FILTER (WHERE exit_result = 'WIN')::int AS wins
            FROM v_trades_analytics
            WHERE {date_filter} AND status = 'closed' AND hold_minutes IS NOT NULL
            GROUP BY bucket ORDER BY bucket
        """), params)))

        session.close()
        return {
            "start": start,
            "end": end,
            "available_dates": available_dates,
            "summary": summary,
            "streaks": streak_data,
            "pnl_by_ticker": pnl_by_ticker,
            "pnl_by_exit_hour": pnl_by_exit_hour,
            "pnl_by_entry_hour": pnl_by_entry_hour,
            "risk_by_hour": risk_by_hour,
            "contracts_by_hour": contracts_by_hour,
            "contract_type": contract_type,
            "exit_reasons": exit_reasons,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            "cumulative_pnl": cumulative,
            "pnl_by_day_of_week": pnl_by_day_of_week,
            "pnl_by_signal_type": pnl_by_signal_type,
            "hold_time_dist": hold_time_dist,
        }
    finally:
        session.close()


@router.get("/analytics/drilldown")
def drilldown(
    start: Optional[str] = None,
    end: Optional[str] = None,
    ticker: Optional[str] = None,
    hour: Optional[int] = None,
    hour_type: Optional[str] = "entry",  # "entry" or "exit"
    contract_type: Optional[str] = None,  # "Call" or "Put"
    exit_reason: Optional[str] = None,
    day_of_week: Optional[int] = None,  # 1=Mon, 7=Sun (ISO day)
    signal_type: Optional[str] = None,
):
    """Drill down into individual trades for a specific chart bar."""
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        conn = session.connection()

        if not start:
            r = conn.execute(text("SELECT MAX(trade_date) FROM v_trades_analytics")).scalar()
            start = str(r) if r else str(datetime.now(timezone.utc).date())
        if not end:
            end = start

        conditions = ["trade_date BETWEEN :start AND :end"]
        params: dict = {"start": start, "end": end}

        if ticker:
            conditions.append("ticker = :ticker")
            params["ticker"] = ticker
        if hour is not None:
            if hour_type == "exit":
                conditions.append("exit_hour = :hour")
            else:
                conditions.append("entry_hour = :hour")
            params["hour"] = hour
        if contract_type:
            conditions.append("contract_type = :contract_type")
            params["contract_type"] = contract_type
        if exit_reason:
            conditions.append("exit_reason = :exit_reason")
            params["exit_reason"] = exit_reason
        if day_of_week is not None:
            conditions.append("EXTRACT(ISODOW FROM entry_time_pt)::int = :day_of_week")
            params["day_of_week"] = day_of_week
        if signal_type:
            conditions.append("COALESCE(signal_type, 'unknown') = :signal_type")
            params["signal_type"] = signal_type

        where = " AND ".join(conditions)

        trades = _serialize(_rows_to_dicts(conn.execute(text(f"""
            SELECT id, ticker, symbol, direction, contract_type, status,
                   contracts_entered, entry_price, exit_price, current_price,
                   ROUND((pnl_pct * 100)::numeric, 1) AS pnl_pct,
                   ROUND(pnl_usd::numeric, 2) AS pnl_usd,
                   exit_reason, exit_result,
                   TO_CHAR(entry_time_pt, 'HH24:MI') AS entry_time,
                   TO_CHAR(exit_time_pt, 'HH24:MI') AS exit_time,
                   ROUND(hold_minutes::numeric, 1) AS hold_min,
                   ROUND(risk_capital::numeric, 2) AS risk_capital
            FROM v_trades_analytics
            WHERE {where}
            ORDER BY entry_time_pt
        """), params)))

        session.close()
        return {"trades": trades, "count": len(trades), "filters": params}
    finally:
        session.close()
