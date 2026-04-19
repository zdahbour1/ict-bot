"""
Database writer — functions for the bot to write trade, thread, and error data.
All functions are no-op if DATABASE_URL is not configured.
"""
import logging
import traceback as tb
from datetime import datetime, timezone

from db.connection import get_session, db_available

log = logging.getLogger(__name__)


_db_checked = None

def _safe_db(func):
    """Decorator that catches DB errors and logs them without crashing the bot."""
    def wrapper(*args, **kwargs):
        global _db_checked
        if _db_checked is None:
            _db_checked = db_available()
            if _db_checked:
                log.info("Database connection verified — DB writes enabled")
            else:
                log.info("Database not available — DB writes disabled")
        if not _db_checked:
            return None
        try:
            return func(*args, **kwargs)
        except Exception as e:
            log.warning(f"DB write failed ({func.__name__}): {e}")
            return None
    return wrapper


@_safe_db
def _resolve_strategy_id(trade: dict) -> int:
    """Resolve the strategy_id to stamp on a new trade.

    Priority: trade['strategy_id'] > ACTIVE_STRATEGY setting > default strategy > 1.
    Cached for the process lifetime after the first successful lookup so every
    insert doesn't hit the DB for the same answer.
    """
    # Explicit win
    sid = trade.get("strategy_id")
    if sid is not None:
        try:
            return int(sid)
        except (TypeError, ValueError):
            pass

    global _CACHED_ACTIVE_STRATEGY_ID
    cached = globals().get("_CACHED_ACTIVE_STRATEGY_ID")
    if cached:
        return cached

    try:
        from db.strategy_writer import get_active_strategy_id, get_default_strategy_id
        resolved = get_active_strategy_id() or get_default_strategy_id() or 1
    except Exception:
        resolved = 1
    _CACHED_ACTIVE_STRATEGY_ID = resolved
    return resolved


# Process-cached active strategy_id — invalidated only on bot restart
# (matches the "change requires restart" contract in active_strategy_design.md).
_CACHED_ACTIVE_STRATEGY_ID: int | None = None


def invalidate_active_strategy_cache() -> None:
    """Testing hook — clear the cached active strategy id."""
    global _CACHED_ACTIVE_STRATEGY_ID
    _CACHED_ACTIVE_STRATEGY_ID = None


def _roadmap_fields(trade: dict) -> dict:
    """Extract the roadmap-schema optional fields from a trade dict.

    Only keys the caller actually provided are returned — the rest fall
    back to the DB defaults (OPT / 100 / SMART / USD). This keeps today's
    ICT flow unchanged while letting FOP / STK / futures callers pass
    their own values without a separate insert path.
    """
    out: dict = {}
    for key in ("sec_type", "multiplier", "exchange", "currency",
                "underlying", "strategy_config"):
        if key in trade and trade[key] is not None:
            out[key] = trade[key]
    return out


def insert_trade(trade: dict, account: str) -> int | None:
    """Insert a new trade row. Returns the DB id."""
    from db.models import Trade
    session = get_session()
    if not session:
        return None
    try:
        # Build enrichment JSONB
        entry_enrichment = {}
        for key in ("entry_indicators", "entry_greeks", "entry_stock_price", "entry_vix"):
            if key in trade:
                entry_enrichment[key] = trade[key]

        row = Trade(
            account=account,
            ticker=trade.get("ticker", "UNK"),
            symbol=trade["symbol"],
            direction=trade.get("direction", "LONG"),
            contracts_entered=trade["contracts"],
            contracts_open=trade["contracts"],
            entry_price=float(trade["entry_price"]),
            ib_fill_price=float(trade["entry_price"]),
            current_price=float(trade["entry_price"]),
            ib_order_id=trade.get("ib_order_id"),
            ib_perm_id=trade.get("ib_perm_id"),
            ib_con_id=trade.get("ib_con_id"),
            ib_tp_perm_id=trade.get("ib_tp_perm_id"),
            ib_sl_perm_id=trade.get("ib_sl_perm_id"),
            profit_target=float(trade.get("profit_target", 0)),
            stop_loss_level=float(trade.get("stop_loss", 0)),
            signal_type=trade.get("signal"),
            ict_entry=float(trade["ict_entry"]) if trade.get("ict_entry") else None,
            ict_sl=float(trade["ict_sl"]) if trade.get("ict_sl") else None,
            ict_tp=float(trade["ict_tp"]) if trade.get("ict_tp") else None,
            entry_time=trade.get("entry_time", datetime.now(timezone.utc)),
            entry_enrichment=_sanitize_for_json(entry_enrichment),
            strategy_id=_resolve_strategy_id(trade),
            # Roadmap schema extensions — caller may override, else DB defaults
            # (OPT / 100 / SMART / USD) fire and behavior is identical to before.
            **_roadmap_fields(trade),
        )
        session.add(row)
        session.commit()
        trade_id = row.id
        session.close()
        log.debug(f"DB: inserted trade {trade_id} for {trade.get('ticker')}")
        return trade_id
    except Exception as e:
        session.rollback()
        session.close()
        raise


@_safe_db
def get_open_trades_from_db() -> list:
    """Get all open trades from DB. Used by exit_manager as source of truth.
    Returns list of trade dicts with all fields needed for monitoring."""
    from db.models import Trade
    session = get_session()
    if not session:
        return []
    try:
        rows = session.query(Trade).filter(Trade.status == "open").all()
        result = []
        for r in rows:
            result.append({
                "db_id": r.id,
                "ticker": r.ticker,
                "symbol": r.symbol,
                "contracts": r.contracts_open,
                "direction": r.direction or "LONG",
                "entry_price": float(r.entry_price) if r.entry_price else 0,
                "entry_time": r.entry_time,
                "current_price": float(r.current_price) if r.current_price else 0,
                "profit_target": float(r.profit_target) if r.profit_target else 0,
                "stop_loss": float(r.stop_loss_level) if r.stop_loss_level else 0,
                "ib_con_id": r.ib_con_id,
                "ib_order_id": r.ib_order_id,
                "ib_perm_id": r.ib_perm_id,
                "ib_tp_order_id": r.ib_tp_perm_id,
                "ib_sl_order_id": r.ib_sl_perm_id,
                "peak_pnl_pct": float(r.peak_pnl_pct) if r.peak_pnl_pct else 0,
                "dynamic_sl_pct": float(r.dynamic_sl_pct) if r.dynamic_sl_pct else -0.6,
                "signal": r.signal_type,
                "pnl_pct": float(r.pnl_pct) if r.pnl_pct else 0,
                "pnl_usd": float(r.pnl_usd) if r.pnl_usd else 0,
            })
        session.close()
        return result
    except Exception as e:
        session.close()
        raise


@_safe_db
def update_trade_price(trade_id: int, current_price: float, pnl_pct: float,
                       pnl_usd: float, peak_pnl_pct: float, dynamic_sl_pct: float):
    """Update live pricing for an open trade. Only updates if trade is still open.
    Uses GREATEST() to never downgrade peak_pnl_pct."""
    from sqlalchemy import text
    session = get_session()
    if not session:
        return
    try:
        session.execute(
            text("UPDATE trades SET current_price=:cp, pnl_pct=:pp, pnl_usd=:pu, "
                 "peak_pnl_pct = GREATEST(peak_pnl_pct, :peak), "
                 "dynamic_sl_pct=:dsl "
                 "WHERE id=:id AND status='open'"),
            {"cp": current_price, "pp": pnl_pct, "pu": pnl_usd,
             "peak": peak_pnl_pct, "dsl": dynamic_sl_pct, "id": trade_id}
        )
        session.commit()
        session.close()
    except Exception as e:
        session.rollback()
        session.close()
        raise


def _sanitize_for_json(obj):
    """Recursively convert datetime objects to ISO strings for JSONB storage."""
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    elif isinstance(obj, datetime):
        return obj.isoformat()
    elif hasattr(obj, '__float__'):
        try:
            return float(obj)
        except (ValueError, TypeError):
            return str(obj)
    return obj


@_safe_db
def lock_trade_for_close(trade_id: int):
    """
    Lock a trade record for closing using SELECT FOR UPDATE NOWAIT.
    Returns (session, trade_data) or (None, None).

    NOWAIT: If another thread already holds the lock, returns (None, None)
    immediately instead of blocking. The caller should skip this trade
    and move to the next one.

    The caller MUST call finalize_close() or release_trade_lock() when done.

    Flow:
    1. lock_trade_for_close()   ← acquires DB lock (or returns None if locked)
    2. [caller does IB work]    ← cancel brackets, sell, verify, enrich
    3. finalize_close()         ← updates DB, commits, releases lock
    OR release_trade_lock()     ← rollback if IB work failed
    """
    from sqlalchemy import text
    session = get_session()
    if not session:
        return None, None
    try:
        # NOWAIT: fail immediately if another process holds the lock
        row = session.execute(
            text("SELECT id, entry_price, contracts_entered, contracts_open, ticker, symbol, "
                 "ib_con_id, ib_order_id, ib_perm_id, ib_tp_perm_id, ib_sl_perm_id, direction "
                 "FROM trades WHERE id = :id AND status = 'open' FOR UPDATE NOWAIT"),
            {"id": trade_id}
        ).fetchone()

        if not row:
            session.close()
            log.info(f"DB: trade {trade_id} already closed — lock not acquired")
            return None, None

        trade_data = {
            "id": row[0], "entry_price": float(row[1]),
            "contracts_entered": int(row[2]), "contracts_open": int(row[3]),
            "ticker": row[4], "symbol": row[5],
            "ib_con_id": int(row[6]) if row[6] else None,
            "ib_order_id": int(row[7]) if row[7] else None,
            "ib_perm_id": int(row[8]) if row[8] else None,
            "ib_tp_order_id": int(row[9]) if row[9] else None,
            "ib_sl_order_id": int(row[10]) if row[10] else None,
            "direction": row[11] or "LONG",
        }
        log.debug(f"DB: locked trade {trade_id} for close")
        return session, trade_data
    except Exception as e:
        session.rollback()
        session.close()
        error_str = str(e)
        if "could not obtain lock" in error_str or "lock" in error_str.lower():
            log.info(f"DB: trade {trade_id} locked by another process — skipping")
            return None, None
        raise


def finalize_close(session, trade_id: int, exit_price: float, result: str,
                   reason: str, exit_enrichment: dict = None) -> bool:
    """
    Complete the close: update DB record and commit (releases lock).
    Must be called after lock_trade_for_close() with the same session.
    """
    import json as json_mod
    from sqlalchemy import text
    try:
        row = session.execute(
            text("SELECT entry_price, contracts_entered FROM trades WHERE id = :id"),
            {"id": trade_id}
        ).fetchone()
        entry_price = float(row[0]) if row else 0
        contracts = int(row[1]) if row else 0

        pnl_pct = (exit_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
        pnl_usd = (exit_price - entry_price) * 100 * contracts

        safe_enrichment = _sanitize_for_json(exit_enrichment or {})

        session.execute(
            text("UPDATE trades SET exit_price=:ep, current_price=:ep, "
                 "pnl_pct=:pp, pnl_usd=:pu, exit_time=NOW(), "
                 "status='closed', exit_reason=:rn, exit_result=:er, "
                 "contracts_open=0, contracts_closed=:cc, "
                 "exit_enrichment=CAST(:ee AS jsonb) "
                 "WHERE id=:id"),
            {"ep": exit_price, "pp": pnl_pct, "pu": pnl_usd,
             "rn": reason, "er": result, "cc": contracts,
             "ee": json_mod.dumps(safe_enrichment), "id": trade_id}
        )
        session.commit()
        session.close()
        log.info(f"DB: closed trade {trade_id} — {result} ({reason}) P&L=${pnl_usd:.2f}")
        return True
    except Exception as e:
        session.rollback()
        session.close()
        log.error(f"DB: finalize_close failed for trade {trade_id}: {e}")
        return False


def release_trade_lock(session):
    """Rollback and release the lock without closing the trade.
    Use when IB close operation failed and we want to retry later."""
    if session:
        try:
            session.rollback()
            session.close()
        except Exception:
            pass


@_safe_db
def close_trade(trade_id: int, exit_price: float, result: str, reason: str,
                exit_enrichment: dict = None) -> bool:
    """
    Simple close: lock, update, commit in one call.
    Use this for reconciliation or cases where IB work is already done.
    For the full atomic flow (lock → IB close → update), use
    lock_trade_for_close() + finalize_close() instead.
    """
    session, trade_data = lock_trade_for_close(trade_id)
    if not session:
        return False
    return finalize_close(session, trade_id, exit_price, result, reason, exit_enrichment)


@_safe_db
def record_partial_close(trade_id: int, contracts: int, close_price: float,
                         pnl_pct: float, pnl_usd: float, reason: str,
                         ib_order_id: int = None, ib_fill_price: float = None):
    """Record a partial close event and update the trade's contract counts."""
    from db.models import Trade, TradeClose
    session = get_session()
    if not session:
        return
    try:
        close = TradeClose(
            trade_id=trade_id,
            contracts=contracts,
            close_price=close_price,
            pnl_pct=pnl_pct,
            pnl_usd=pnl_usd,
            reason=reason,
            ib_order_id=ib_order_id,
            ib_fill_price=ib_fill_price,
        )
        session.add(close)

        trade = session.query(Trade).filter(Trade.id == trade_id).first()
        if trade:
            trade.contracts_open = max(0, trade.contracts_open - contracts)
            trade.contracts_closed += contracts
            if trade.contracts_open == 0:
                trade.status = "closed"
                trade.exit_time = datetime.now(timezone.utc)
                trade.exit_price = close_price

        session.commit()
        session.close()
    except Exception as e:
        session.rollback()
        session.close()
        raise


@_safe_db
def mark_trade_errored(trade_id: int, error_message: str):
    """Mark a trade as errored."""
    from db.models import Trade
    session = get_session()
    if not session:
        return
    try:
        session.query(Trade).filter(Trade.id == trade_id).update({
            "status": "errored",
            "error_message": error_message,
        })
        session.commit()
        session.close()
    except Exception as e:
        session.rollback()
        session.close()
        raise


@_safe_db
def update_thread_status(thread_name: str, ticker: str = None, status: str = "idle",
                         message: str = None, scans_today: int = None,
                         trades_today: int = None, alerts_today: int = None,
                         error_count: int = None,
                         pid: int = None, thread_id: int = None):
    """Upsert thread status row."""
    import os, threading as _threading
    from db.models import ThreadStatus
    session = get_session()
    if not session:
        return
    # Auto-detect pid and thread_id if not provided
    if pid is None:
        pid = os.getpid()
    if thread_id is None:
        thread_id = _threading.get_ident()
    try:
        existing = session.query(ThreadStatus).filter(
            ThreadStatus.thread_name == thread_name
        ).first()

        if existing:
            existing.status = status
            existing.pid = pid
            existing.thread_id = thread_id
            if ticker:
                existing.ticker = ticker
            if message is not None:
                existing.last_message = message
            if status == "scanning":
                existing.last_scan_time = datetime.now(timezone.utc)
            if scans_today is not None:
                existing.scans_today = scans_today
            if trades_today is not None:
                existing.trades_today = trades_today
            if alerts_today is not None:
                existing.alerts_today = alerts_today
            if error_count is not None:
                existing.error_count = error_count
        else:
            row = ThreadStatus(
                thread_name=thread_name,
                ticker=ticker,
                status=status,
                pid=pid,
                thread_id=thread_id,
                last_message=message,
                last_scan_time=datetime.now(timezone.utc) if status == "scanning" else None,
                scans_today=scans_today or 0,
                trades_today=trades_today or 0,
                alerts_today=alerts_today or 0,
                error_count=error_count or 0,
            )
            session.add(row)

        session.commit()
        session.close()
    except Exception as e:
        session.rollback()
        session.close()
        raise


@_safe_db
def update_bot_state(status: str, account: str = None, pid: int = None,
                     total_tickers: int = None):
    """Update the bot_state singleton."""
    from db.models import BotState
    session = get_session()
    if not session:
        return
    try:
        state = session.query(BotState).filter(BotState.id == 1).first()
        if state:
            state.status = status
            if account:
                state.account = account
            if pid:
                state.pid = pid
            if total_tickers is not None:
                state.total_tickers = total_tickers
            if status == "running":
                state.started_at = datetime.now(timezone.utc)
            elif status == "stopped":
                state.stopped_at = datetime.now(timezone.utc)
        else:
            state = BotState(
                id=1, status=status, account=account, pid=pid,
                total_tickers=total_tickers or 0,
            )
            session.add(state)
        session.commit()
        session.close()
    except Exception as e:
        session.rollback()
        session.close()
        raise


@_safe_db
def log_error(thread_name: str = None, ticker: str = None, trade_id: int = None,
              error_type: str = "unknown", message: str = "", trace: str = None):
    """Insert an error log row."""
    from db.models import Error
    session = get_session()
    if not session:
        return
    try:
        row = Error(
            thread_name=thread_name,
            ticker=ticker,
            trade_id=trade_id,
            error_type=error_type,
            message=message[:2000] if message else "",
            traceback=trace[:5000] if trace else None,
        )
        session.add(row)
        session.commit()
        session.close()
    except Exception as e:
        session.rollback()
        session.close()
        raise


@_safe_db
def check_pending_commands() -> list:
    """Fetch all pending trade commands from the UI. Returns list of dicts."""
    from db.models import TradeCommand
    session = get_session()
    if not session:
        return []
    try:
        commands = session.query(TradeCommand).filter(
            TradeCommand.status == "pending"
        ).order_by(TradeCommand.created_at).all()

        result = []
        for cmd in commands:
            cmd.status = "executing"
            result.append({
                "id": cmd.id,
                "trade_id": cmd.trade_id,
                "command": cmd.command,
                "contracts": cmd.contracts,
            })
        session.commit()
        session.close()
        return result
    except Exception as e:
        session.rollback()
        session.close()
        raise


@_safe_db
def complete_command(command_id: int, error: str = None):
    """Mark a command as executed or failed."""
    from db.models import TradeCommand
    session = get_session()
    if not session:
        return
    try:
        session.query(TradeCommand).filter(TradeCommand.id == command_id).update({
            "status": "failed" if error else "executed",
            "error": error,
            "executed_at": datetime.now(timezone.utc),
        })
        session.commit()
        session.close()
    except Exception as e:
        session.rollback()
        session.close()
        raise


# ══════════════════════════════════════════════════════════
# System State Management (DB as single source of truth)
# ══════════════════════════════════════════════════════════

@_safe_db
def get_bot_state() -> dict | None:
    """Read current bot state from DB."""
    from db.models import BotState
    session = get_session()
    if not session:
        return None
    try:
        state = session.query(BotState).filter(BotState.id == 1).first()
        if not state:
            return None
        result = {
            "status": state.status,
            "scans_active": state.scans_active,
            "stop_requested": state.stop_requested,
            "ib_connected": state.ib_connected,
            "pid": state.pid,
            "account": state.account,
            "total_tickers": state.total_tickers,
            "last_error": state.last_error,
        }
        session.close()
        return result
    except Exception as e:
        session.close()
        return None


@_safe_db
def set_scans_active(active: bool):
    """Set scan state in DB."""
    from db.models import BotState
    session = get_session()
    if not session:
        return
    try:
        session.query(BotState).filter(BotState.id == 1).update({"scans_active": active})
        session.commit()
        session.close()
        log.info(f"DB: scans_active = {active}")
    except Exception as e:
        session.rollback()
        session.close()
        raise


@_safe_db
def set_stop_requested(requested: bool):
    """Set stop request in DB."""
    from db.models import BotState
    session = get_session()
    if not session:
        return
    try:
        session.query(BotState).filter(BotState.id == 1).update({"stop_requested": requested})
        session.commit()
        session.close()
    except Exception as e:
        session.rollback()
        session.close()
        raise


@_safe_db
def set_ib_connected(connected: bool):
    """Set IB connection state in DB."""
    from db.models import BotState
    session = get_session()
    if not session:
        return
    try:
        session.query(BotState).filter(BotState.id == 1).update({"ib_connected": connected})
        session.commit()
        session.close()
    except Exception as e:
        session.rollback()
        session.close()
        raise


@_safe_db
def set_bot_error(error_msg: str | None):
    """Set or clear the last error in DB."""
    from db.models import BotState
    session = get_session()
    if not session:
        return
    try:
        session.query(BotState).filter(BotState.id == 1).update({"last_error": error_msg})
        session.commit()
        session.close()
    except Exception as e:
        session.rollback()
        session.close()
        raise


@_safe_db
def add_system_log(component: str, level: str, message: str, details: dict = None):
    """Add an entry to the system_log table."""
    from db.models import SystemLog
    session = get_session()
    if not session:
        return
    try:
        row = SystemLog(
            component=component,
            level=level,
            message=message[:2000],
            details=details or {},
        )
        session.add(row)
        session.commit()
        session.close()
    except Exception as e:
        session.rollback()
        session.close()
        raise
