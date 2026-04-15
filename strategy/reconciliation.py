"""
IB ↔ DB Reconciliation — Two-pass sync between IB positions and database.

Pass 1 (DB → IB): Every open trade in DB must exist on IB.
    If not → mark closed in DB (bracket/SL likely fired while bot was down).

Pass 2 (IB → DB): Every IB position must have a record in DB.
    If not → adopt into DB (trade entered but DB write failed/timed out).

In both passes, the DATABASE is the record we update to match IB reality.
IB is the source of truth for what positions actually exist.
"""
import logging
from datetime import datetime
import pytz
import config

log = logging.getLogger(__name__)
PT = pytz.timezone("America/Los_Angeles")


def startup_reconciliation_direct(client, exit_manager):
    """
    Run on MAIN THREAD after IB connects, before main loop starts.
    Calls IB directly (not via worker queue) because queue isn't running yet.
    """
    log.info("=" * 50)
    log.info("Running reconciliation (direct mode)...")
    try:
        ib_positions = client._ib_get_positions_raw()
    except Exception as e:
        log.error(f"Reconciliation ABORTED — can't get IB positions: {e}")
        _log_to_db("error", f"Aborted (direct): {e}")
        return
    _reconcile(client, exit_manager, ib_positions)


def periodic_reconciliation(client, exit_manager):
    """
    Run via worker queue during normal operation.
    Full two-pass reconciliation — same logic as startup.
    """
    try:
        ib_positions = client.get_ib_positions_raw()
    except Exception as e:
        log.debug(f"Periodic reconciliation skipped — IB positions unavailable: {e}")
        return
    _reconcile(client, exit_manager, ib_positions)


def _reconcile(client, exit_manager, ib_positions):
    """
    Core two-pass reconciliation.

    Pass 1: For each open trade in DB → verify it exists on IB.
    Pass 2: For each IB position → verify it exists in DB.
    """
    # ── Build IB position lookup by conId ──
    ib_by_con_id = {}
    for p in ib_positions:
        con_id = p.get("conId")
        qty = p.get("qty", 0)
        if con_id and abs(qty) > 0:
            ib_by_con_id[con_id] = p

    # ── Get all open trades from DATABASE (not just in-memory) ──
    db_open_trades = _get_db_open_trades()

    log.info(f"[RECONCILE] IB positions: {len(ib_by_con_id)} | DB open trades: {len(db_open_trades)}")

    # ── SAFETY CHECK: IB returns 0 but DB has trades → likely connection issue ──
    if len(ib_by_con_id) == 0 and len(db_open_trades) > 0:
        log.warning(f"[RECONCILE] SAFETY: IB returned 0 positions but DB has "
                    f"{len(db_open_trades)} open trades. ABORTING.")
        _log_to_db("warn", f"Aborted: IB=0 positions, DB={len(db_open_trades)} trades")
        return

    # ══════════════════════════════════════════════════════════
    # PASS 1: DB → IB (close DB trades that no longer exist on IB)
    # ══════════════════════════════════════════════════════════
    closed_count = 0
    for db_trade in db_open_trades:
        db_id = db_trade["id"]
        con_id = db_trade.get("ib_con_id")
        ticker = db_trade.get("ticker", "UNK")
        symbol = db_trade.get("symbol", "")

        if not con_id:
            log.warning(f"[RECONCILE] DB trade {db_id} ({ticker} {symbol}) has no conId — "
                        f"cannot verify against IB. Skipping.")
            continue

        if con_id in ib_by_con_id:
            # Position exists on IB — all good
            continue

        # Position NOT on IB → close in DB
        log.info(f"[RECONCILE] PASS 1: {ticker} {symbol} (conId={con_id}, db_id={db_id}) "
                 f"— NOT on IB → closing in DB")

        exit_price = _get_exit_price(client, ticker, db_trade)
        entry_price = db_trade.get("entry_price", 0)
        pnl_pct = (exit_price - entry_price) / entry_price if entry_price > 0 else 0
        result = "WIN" if pnl_pct > 0.001 else "LOSS" if pnl_pct < -0.001 else "SCRATCH"

        try:
            from db.writer import close_trade
            close_trade(db_id, exit_price, result, "CLOSED ON IB (RECONCILE)", {})
            closed_count += 1
            log.info(f"[RECONCILE] Closed db_id={db_id} {ticker}: {result} P&L={pnl_pct:+.1%}")
        except Exception as e:
            log.error(f"[RECONCILE] Failed to close db_id={db_id}: {e}")

        # Also remove from exit_manager in-memory if present
        _remove_from_exit_manager(exit_manager, con_id, symbol)

    # ══════════════════════════════════════════════════════════
    # PASS 2: IB → DB (adopt IB positions that have no DB record)
    # ══════════════════════════════════════════════════════════
    # Refresh DB open trades after Pass 1 closures
    db_open_con_ids = _get_db_open_con_ids()
    bot_con_ids = {t.get("ib_con_id") for t in exit_manager.open_trades if t.get("ib_con_id")}

    adopted_count = 0
    for con_id, pos in ib_by_con_id.items():
        # Skip if already in DB
        if con_id in db_open_con_ids:
            continue
        # Skip if already in exit_manager memory
        if con_id in bot_con_ids:
            continue

        ticker = pos.get("ticker", "UNK")
        sym = pos.get("symbol", "").strip()
        qty = int(pos["qty"])  # Preserve sign — negative means naked short on IB
        avg_cost = pos.get("avg_cost", 0)
        right = pos.get("right", "C")
        direction = "SHORT" if right == "P" else "LONG"

        log.info(f"[RECONCILE] PASS 2: {ticker} {sym} (conId={con_id}) "
                 f"— on IB but NOT in DB → adopting")

        trade = {
            "ticker": ticker,
            "symbol": sym,
            "contracts": qty,
            "entry_price": avg_cost,
            "profit_target": round(avg_cost * (1 + config.PROFIT_TARGET), 2),
            "stop_loss": round(avg_cost * (1 - config.STOP_LOSS), 2),
            "entry_time": datetime.now(PT),
            "direction": direction,
            "ib_con_id": con_id,
            "_adopted": True,
            "peak_pnl_pct": 0.0,
            "dynamic_sl_pct": -config.STOP_LOSS,
        }

        # Add to exit manager (writes to DB via add_trade)
        exit_manager.add_trade(trade)

        # Verify DB record was created
        if not trade.get("db_id"):
            log.error(f"[RECONCILE] Adopted {ticker} {sym} but NO db_id — retrying...")
            try:
                from db.writer import insert_trade
                db_id = insert_trade(trade, config.IB_ACCOUNT or "unknown")
                if db_id:
                    trade["db_id"] = db_id
            except Exception as e:
                log.error(f"[RECONCILE] DB retry failed for {ticker} {sym}: {e}")

        adopted_count += 1
        log.info(f"[RECONCILE] Adopted {ticker} {sym} (conId={con_id}): "
                 f"{qty}x @ ${avg_cost:.2f} {direction} db_id={trade.get('db_id', 'MISSING')}")

    # ── Summary ──
    total_ib = len(ib_by_con_id)
    total_db = len(db_open_trades)
    log.info(f"[RECONCILE] Done: {closed_count} closed, {adopted_count} adopted, "
             f"{total_ib - adopted_count} matched. "
             f"(IB={total_ib}, DB was={total_db}, DB now={total_db - closed_count + adopted_count})")
    _log_to_db("info", f"Reconciliation: closed={closed_count}, adopted={adopted_count}, "
               f"IB={total_ib}, DB={total_db}")
    log.info("=" * 50)

    exit_manager._save_trades()


# ── Helper functions ──────────────────────────────────────────

def _get_db_open_trades() -> list:
    """Get all open trades from DATABASE (not in-memory)."""
    try:
        from db.connection import get_session
        from sqlalchemy import text
        session = get_session()
        if not session:
            return []
        rows = session.execute(
            text("SELECT id, ticker, symbol, ib_con_id, entry_price, direction "
                 "FROM trades WHERE status='open'")
        ).fetchall()
        session.close()
        return [
            {"id": r[0], "ticker": r[1], "symbol": r[2],
             "ib_con_id": int(r[3]) if r[3] else None,
             "entry_price": float(r[4]) if r[4] else 0,
             "direction": r[5]}
            for r in rows
        ]
    except Exception as e:
        log.error(f"[RECONCILE] Failed to query DB open trades: {e}")
        return []


def _get_db_open_con_ids() -> set:
    """Get set of ib_con_id values for all open trades in DB."""
    try:
        from db.connection import get_session
        from sqlalchemy import text
        session = get_session()
        if not session:
            return set()
        rows = session.execute(
            text("SELECT ib_con_id FROM trades WHERE status='open' AND ib_con_id IS NOT NULL")
        ).fetchall()
        session.close()
        return {int(r[0]) for r in rows if r[0]}
    except Exception as e:
        log.error(f"[RECONCILE] Failed to query DB con_ids: {e}")
        return set()


def _get_exit_price(client, ticker, db_trade) -> float:
    """Try to find exit price from IB fills, fall back to entry price."""
    exit_price = db_trade.get("entry_price", 0)
    try:
        fill = client.check_recent_fills(ticker)
        if fill and fill.get("price"):
            exit_price = fill["price"]
            log.info(f"[RECONCILE] Found IB fill for {ticker}: ${exit_price:.2f}")
    except Exception:
        pass
    return exit_price


def _remove_from_exit_manager(exit_manager, con_id, symbol):
    """Remove a trade from exit_manager's in-memory list."""
    sym_clean = symbol.replace(" ", "")
    with exit_manager._lock:
        for trade in list(exit_manager.open_trades):
            t_con = trade.get("ib_con_id")
            t_sym = trade.get("symbol", "").replace(" ", "")
            if (t_con and t_con == con_id) or t_sym == sym_clean:
                exit_manager.open_trades.remove(trade)
                log.info(f"[RECONCILE] Removed from exit_manager memory: {symbol}")
                break


def _log_to_db(level, message):
    """Best-effort log to system_log table."""
    try:
        from db.writer import add_system_log
        add_system_log("reconciliation", level, message)
    except Exception:
        pass
