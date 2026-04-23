"""DB writer for backtest_runs + backtest_trades.

Same single-source-of-truth pattern as db/writer.py: every state
transition is a single commit; caller never touches sessions.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import text

from db.connection import get_session
from backtest_engine.metrics import BacktestSummary

log = logging.getLogger(__name__)


def _sanitize_json(obj):
    """Recursively strip non-JSON-serializable values (numpy, pd.Timestamp, ...)."""
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    if isinstance(obj, float):
        # NaN / inf can't serialize
        if obj != obj or obj in (float("inf"), float("-inf")):
            return None
        return obj
    if isinstance(obj, (list, tuple)):
        return [_sanitize_json(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _sanitize_json(v) for k, v in obj.items()}
    # Fall back to string coercion for everything else
    try:
        return str(obj)
    except Exception:
        return None


def create_run(
    *,
    name: str,
    strategy_id: int,
    tickers: Sequence[str],
    start_date: date,
    end_date: date,
    config: dict,
) -> Optional[int]:
    """Insert a backtest_runs row in 'pending' status. Returns the new id."""
    session = get_session()
    if session is None:
        return None
    try:
        row = session.execute(
            text(
                "INSERT INTO backtest_runs "
                "  (name, status, strategy_id, tickers, start_date, end_date, config) "
                "VALUES (:name, 'pending', :sid, :tickers, :start, :end, CAST(:config AS jsonb)) "
                "RETURNING id"
            ),
            {
                "name": name,
                "sid": strategy_id,
                "tickers": list(tickers),
                "start": start_date,
                "end": end_date,
                "config": json.dumps(_sanitize_json(config)),
            },
        )
        run_id = int(row.scalar())
        session.commit()
        return run_id
    except Exception as e:
        session.rollback()
        log.error(f"create_run failed: {e}", exc_info=True)
        return None
    finally:
        session.close()


def mark_run_started(run_id: int) -> None:
    session = get_session()
    if session is None:
        return
    try:
        session.execute(
            text(
                "UPDATE backtest_runs "
                "SET status = 'running', started_at = NOW() "
                "WHERE id = :id"
            ),
            {"id": run_id},
        )
        session.commit()
    finally:
        session.close()


def mark_run_failed(run_id: int, error: str) -> None:
    session = get_session()
    if session is None:
        return
    try:
        session.execute(
            text(
                "UPDATE backtest_runs "
                "SET status = 'failed', error_message = :err, "
                "    completed_at = NOW() "
                "WHERE id = :id"
            ),
            {"id": run_id, "err": error[:4000]},
        )
        session.commit()
    finally:
        session.close()


def finalize_run(run_id: int, summary: BacktestSummary) -> None:
    """Write summary stats + flip to completed."""
    session = get_session()
    if session is None:
        return
    try:
        session.execute(
            text(
                "UPDATE backtest_runs SET "
                "  status = 'completed', "
                "  total_trades = :total, wins = :wins, losses = :losses, "
                "  scratches = :scratches, total_pnl = :total_pnl, "
                "  win_rate = :win_rate, avg_win = :avg_win, avg_loss = :avg_loss, "
                "  max_drawdown = :max_dd, sharpe_ratio = :sharpe, "
                "  profit_factor = :pf, avg_hold_min = :avg_hold, "
                "  max_win_streak = :wstreak, max_loss_streak = :lstreak, "
                "  completed_at = NOW(), "
                "  duration_sec = EXTRACT(EPOCH FROM (NOW() - started_at)) "
                "WHERE id = :id"
            ),
            {
                "id": run_id,
                "total": summary.total_trades,
                "wins": summary.wins,
                "losses": summary.losses,
                "scratches": summary.scratches,
                "total_pnl": summary.total_pnl,
                "win_rate": summary.win_rate,
                "avg_win": summary.avg_win,
                "avg_loss": summary.avg_loss,
                "max_dd": summary.max_drawdown,
                "sharpe": summary.sharpe_ratio,
                "pf": summary.profit_factor,
                "avg_hold": summary.avg_hold_min,
                "wstreak": summary.max_win_streak,
                "lstreak": summary.max_loss_streak,
            },
        )
        session.commit()
    finally:
        session.close()


def record_trade(run_id: int, strategy_id: int, trade: dict) -> Optional[int]:
    """Insert one backtest_trades row. Pass a dict shaped like a live trade
    dict; unknown keys are ignored."""
    session = get_session()
    if session is None:
        return None
    try:
        row = session.execute(
            text(
                "INSERT INTO backtest_trades ( "
                "  run_id, strategy_id, "
                "  ticker, symbol, direction, contracts, "
                "  entry_price, exit_price, pnl_pct, pnl_usd, peak_pnl_pct, "
                "  slippage_paid, commission, "
                "  entry_time, exit_time, hold_minutes, "
                "  signal_type, entry_bar_idx, "
                "  exit_reason, exit_result, "
                "  tp_level, sl_level, dynamic_sl_pct, tp_trailed, rolled, "
                "  entry_indicators, exit_indicators, entry_context, signal_details "
                ") VALUES ( "
                "  :run, :sid, "
                "  :ticker, :symbol, :direction, :contracts, "
                "  :entry, :exit, :pnl_pct, :pnl_usd, :peak, "
                "  :slip, :comm, "
                "  :etime, :xtime, :hold, "
                "  :sig, :idx, "
                "  :reason, :result, "
                "  :tp, :sl, :dsl, :tptrail, :rolled, "
                "  CAST(:ei AS jsonb), CAST(:xi AS jsonb), "
                "  CAST(:ec AS jsonb), CAST(:sd AS jsonb) "
                ") RETURNING id"
            ),
            {
                "run": run_id,
                "sid": strategy_id,
                "ticker": trade.get("ticker"),
                "symbol": trade.get("symbol"),
                "direction": trade.get("direction", "LONG"),
                "contracts": int(trade.get("contracts", 2)),
                "entry": float(trade["entry_price"]),
                "exit": float(trade["exit_price"]) if trade.get("exit_price") else None,
                "pnl_pct": float(trade.get("pnl_pct", 0) or 0),
                "pnl_usd": float(trade.get("pnl_usd", 0) or 0),
                "peak": float(trade.get("peak_pnl_pct", 0) or 0),
                "slip": float(trade.get("slippage_paid", 0) or 0),
                "comm": float(trade.get("commission", 0) or 0),
                "etime": trade.get("entry_time"),
                "xtime": trade.get("exit_time"),
                "hold": trade.get("hold_minutes"),
                "sig": trade.get("signal_type"),
                "idx": trade.get("entry_bar_idx"),
                "reason": trade.get("exit_reason"),
                "result": trade.get("exit_result"),
                "tp": trade.get("tp_level"),
                "sl": trade.get("sl_level"),
                "dsl": trade.get("dynamic_sl_pct"),
                "tptrail": bool(trade.get("tp_trailed", False)),
                "rolled": bool(trade.get("rolled", False)),
                "ei": json.dumps(_sanitize_json(trade.get("entry_indicators", {}))),
                "xi": json.dumps(_sanitize_json(trade.get("exit_indicators", {}))),
                "ec": json.dumps(_sanitize_json(trade.get("entry_context", {}))),
                "sd": json.dumps(_sanitize_json(trade.get("signal_details", {}))),
            },
        )
        tid = int(row.scalar())
        session.commit()
        return tid
    except Exception as e:
        session.rollback()
        log.error(f"record_trade failed: {e}", exc_info=True)
        return None
    finally:
        session.close()


def record_multi_leg_trade(
    run_id: int, strategy_id: int,
    envelope: dict, legs: list[dict],
) -> Optional[int]:
    """Insert one backtest_trades row + N backtest_trade_legs rows.

    envelope: dict with trade-level fields (run_id/strategy_id are
        passed separately). Expected keys: ticker, entry_time, exit_time,
        signal_type, entry_indicators, exit_indicators, exit_reason,
        exit_result, hold_minutes, plus pre-aggregated pnl_usd and
        pnl_pct. The envelope ALSO needs a `symbol` + `direction` +
        `entry_price` + `exit_price` for back-compat (use the first
        leg's values — the Backtest UI reads these for the trade row).
    legs: list of dicts each with:
        symbol, sec_type, underlying, strike, right, expiry, multiplier,
        direction, contracts, entry_price, exit_price, entry_time,
        exit_time, leg_role, pnl_usd.
    """
    session = get_session()
    if session is None:
        return None
    try:
        first = legs[0] if legs else {}
        envelope = {**envelope}  # copy, don't mutate caller
        envelope.setdefault("symbol", first.get("symbol"))
        envelope.setdefault("direction", first.get("direction", "LONG"))
        envelope.setdefault("contracts", first.get("contracts", 1))
        envelope.setdefault("entry_price", float(first.get("entry_price", 0)))
        if "exit_price" not in envelope:
            envelope["exit_price"] = (float(first.get("exit_price"))
                                       if first.get("exit_price") is not None else None)

        trade_id = record_trade(run_id, strategy_id, envelope)
        if trade_id is None:
            return None
        # Stamp n_legs + insert each leg
        session.execute(
            text("UPDATE backtest_trades SET n_legs = :n WHERE id = :id"),
            {"n": len(legs), "id": trade_id},
        )
        for i, leg in enumerate(legs):
            per_leg_pnl = leg.get("pnl_usd")
            if per_leg_pnl is None and leg.get("exit_price") is not None:
                # Compute per-leg realised P&L if the caller didn't pre-supply
                sign = 1 if leg.get("direction", "LONG") == "LONG" else -1
                per_leg_pnl = (
                    (float(leg["exit_price"]) - float(leg["entry_price"]))
                    * int(leg["contracts"])
                    * int(leg.get("multiplier", 100))
                    * sign
                )
            session.execute(
                text(
                    'INSERT INTO backtest_trade_legs ('
                    '  backtest_trade_id, leg_index, leg_role, '
                    '  sec_type, symbol, underlying, strike, "right", '
                    '  expiry, multiplier, direction, contracts, '
                    '  entry_price, exit_price, entry_time, exit_time, pnl_usd'
                    ') VALUES ('
                    '  :tid, :idx, :role, '
                    '  :sec, :sym, :under, :strike, :right, '
                    '  :exp, :mult, :dir, :qty, '
                    '  :epx, :xpx, :et, :xt, :pnl'
                    ')'
                ),
                {
                    "tid": trade_id, "idx": leg.get("leg_index", i),
                    "role": leg.get("leg_role"),
                    "sec": leg.get("sec_type", "OPT"),
                    "sym": leg["symbol"],
                    "under": leg.get("underlying"),
                    "strike": leg.get("strike"),
                    "right": leg.get("right"),
                    "exp": leg.get("expiry"),
                    "mult": int(leg.get("multiplier", 100)),
                    "dir": leg.get("direction", "LONG"),
                    "qty": int(leg["contracts"]),
                    "epx": float(leg["entry_price"]),
                    "xpx": float(leg["exit_price"]) if leg.get("exit_price") is not None else None,
                    "et": leg.get("entry_time") or envelope.get("entry_time"),
                    "xt": leg.get("exit_time") or envelope.get("exit_time"),
                    "pnl": float(per_leg_pnl) if per_leg_pnl is not None else None,
                },
            )
        session.commit()
        return trade_id
    except Exception as e:
        session.rollback()
        log.error(f"record_multi_leg_trade failed: {e}", exc_info=True)
        return None
    finally:
        session.close()


def list_runs(limit: int = 50, strategy_id: Optional[int] = None,
              status: Optional[str] = None) -> list[dict]:
    session = get_session()
    if session is None:
        return []
    try:
        q = (
            "SELECT id, name, status, strategy_id, tickers, start_date, end_date, "
            "  total_trades, wins, losses, scratches, total_pnl, win_rate, "
            "  avg_win, avg_loss, max_drawdown, sharpe_ratio, profit_factor, "
            "  avg_hold_min, duration_sec, started_at, completed_at, created_at, "
            "  error_message "
            "FROM backtest_runs "
        )
        clauses = []
        params: dict = {"lim": limit}
        if strategy_id is not None:
            clauses.append("strategy_id = :sid")
            params["sid"] = strategy_id
        if status is not None:
            clauses.append("status = :status")
            params["status"] = status
        if clauses:
            q += "WHERE " + " AND ".join(clauses) + " "
        q += "ORDER BY created_at DESC LIMIT :lim"
        rows = session.execute(text(q), params).fetchall()
        return [
            {
                "id": r[0], "name": r[1], "status": r[2],
                "strategy_id": r[3], "tickers": list(r[4] or []),
                "start_date": r[5].isoformat() if r[5] else None,
                "end_date": r[6].isoformat() if r[6] else None,
                "total_trades": r[7], "wins": r[8], "losses": r[9],
                "scratches": r[10],
                "total_pnl": float(r[11]) if r[11] is not None else 0.0,
                "win_rate": float(r[12]) if r[12] is not None else 0.0,
                "avg_win": float(r[13]) if r[13] is not None else 0.0,
                "avg_loss": float(r[14]) if r[14] is not None else 0.0,
                "max_drawdown": float(r[15]) if r[15] is not None else 0.0,
                "sharpe_ratio": float(r[16]) if r[16] is not None else None,
                "profit_factor": float(r[17]) if r[17] is not None else None,
                "avg_hold_min": float(r[18]) if r[18] is not None else 0.0,
                "duration_sec": float(r[19]) if r[19] is not None else None,
                "started_at": r[20].isoformat() if r[20] else None,
                "completed_at": r[21].isoformat() if r[21] else None,
                "created_at": r[22].isoformat() if r[22] else None,
                "error_message": r[23],
            }
            for r in rows
        ]
    finally:
        session.close()


def get_run_trades(run_id: int) -> list[dict]:
    session = get_session()
    if session is None:
        return []
    try:
        rows = session.execute(
            text(
                "SELECT id, ticker, symbol, direction, contracts, "
                "  entry_price, exit_price, pnl_pct, pnl_usd, peak_pnl_pct, "
                "  entry_time, exit_time, hold_minutes, "
                "  signal_type, exit_reason, exit_result, "
                "  tp_trailed, rolled, "
                "  entry_indicators, exit_indicators, entry_context, signal_details "
                "FROM backtest_trades WHERE run_id = :id "
                "ORDER BY entry_time"
            ),
            {"id": run_id},
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
                "entry_indicators": r[18] or {},
                "exit_indicators": r[19] or {},
                "entry_context": r[20] or {},
                "signal_details": r[21] or {},
            }
            for r in rows
        ]
    finally:
        session.close()


def delete_run(run_id: int) -> bool:
    session = get_session()
    if session is None:
        return False
    try:
        session.execute(text("DELETE FROM backtest_runs WHERE id = :id"),
                        {"id": run_id})
        session.commit()
        return True
    finally:
        session.close()
