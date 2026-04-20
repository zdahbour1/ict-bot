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


# Module-level singleton — state (suspect_orders dict) must persist
# across reconcile cycles. See docs/orphan_bracket_detector.md.
_orphan_detector_instance = None


def _get_orphan_detector():
    """Lazy-init and return the shared OrphanBracketDetector.

    Reads ORPHAN_AUTO_CANCEL from settings (default True) so the
    feature can be flipped to detection-only mode without a redeploy.
    """
    global _orphan_detector_instance
    if _orphan_detector_instance is None:
        from strategy.orphan_detector import OrphanBracketDetector
        auto = True
        try:
            from db.connection import get_session
            from sqlalchemy import text
            session = get_session()
            if session is not None:
                row = session.execute(
                    text("SELECT value FROM settings "
                         "WHERE key='ORPHAN_AUTO_CANCEL' AND strategy_id IS NULL"),
                ).fetchone()
                session.close()
                if row and row[0] is not None:
                    auto = str(row[0]).lower() in ("true", "1", "yes")
        except Exception:
            pass
        _orphan_detector_instance = OrphanBracketDetector(auto_cancel=auto)
        log.info(
            f"[ORPHAN] Detector initialized (auto_cancel={auto}, "
            f"grace={_orphan_detector_instance.grace_period_sec:.0f}s)"
        )
    return _orphan_detector_instance


def startup_reconciliation_direct(client, exit_manager):
    """
    Run on MAIN THREAD after IB connects, before main loop starts.
    Calls IB directly (not via worker queue) because queue isn't running yet.
    """
    log.info("=" * 50)
    log.info("Running reconciliation (direct mode)...")
    _update_thread("running", "Startup reconciliation...")
    try:
        ib_positions = client._ib_get_positions_raw()
    except Exception as e:
        log.error(f"Reconciliation ABORTED — can't get IB positions: {e}")
        _log_to_db("error", f"Aborted (direct): {e}")
        _update_thread("error", f"Aborted: {e}")
        return
    _reconcile(client, exit_manager, ib_positions)


def periodic_reconciliation(client, exit_manager):
    """
    Run via worker queue during normal operation.
    Full two-pass reconciliation — same logic as startup.
    """
    _update_thread("running", "Fetching IB positions...")
    try:
        ib_positions = client.get_ib_positions_raw()
    except Exception as e:
        _update_thread("idle", f"Skipped — IB unavailable: {e}")
        log.debug(f"Periodic reconciliation skipped — IB positions unavailable: {e}")
        return
    _update_thread("running", f"Reconciling {len(ib_positions)} IB positions...")
    _reconcile(client, exit_manager, ib_positions)


def _reconcile(client, exit_manager, ib_positions):
    """
    Core two-pass reconciliation.

    Pass 1: For each open trade in DB → verify it exists on IB.
    Pass 2: For each IB position → verify it exists in DB.
    """
    # ── Build IB position lookup by conId ──
    # Include ALL positions — positive AND negative. Reconciliation mirrors
    # IB reality into DB. Negative positions are bugs but DB must reflect them
    # so the system knows they exist and doesn't open duplicates.
    ib_by_con_id = {}
    for p in ib_positions:
        con_id = p.get("conId")
        qty = p.get("qty", 0)
        if con_id and qty != 0:
            ib_by_con_id[con_id] = p
            if qty < 0:
                log.warning(f"[RECONCILE] NEGATIVE position on IB: {p.get('ticker')} "
                            f"{p.get('symbol')} conId={con_id} qty={qty}")

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
    # Detailed tracking so the summary shows WHAT was closed/adopted,
    # not just counts. Each entry is a single-line human-readable string.
    closed_items: list[str] = []
    adopted_items: list[str] = []

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
            close_trade(db_id, exit_price, result, "RECONCILE", {"source": "periodic", "detail": "closed on IB"})
            closed_count += 1
            closed_items.append(f"{ticker} {symbol} db_id={db_id} {result}({pnl_pct:+.1%})")
            log.info(f"[RECONCILE] Closed db_id={db_id} {ticker}: {result} P&L={pnl_pct:+.1%}")
            from strategy.audit import log_trade_action
            log_trade_action(
                db_id, "reconcile_close", "reconciliation",
                f"DB trade not on IB → closed with {result} (P&L {pnl_pct:+.1%})",
                extra={"ticker": ticker, "symbol": symbol,
                       "exit_price": exit_price, "result": result,
                       "entry_price": entry_price, "pnl_pct": round(pnl_pct * 100, 2)},
            )
        except Exception as e:
            log.error(f"[RECONCILE] Failed to close db_id={db_id}: {e}")

        # Cache auto-refreshes from DB — no need to manually remove from exit_manager
        exit_manager.invalidate_cache()

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
        # Defensive: IB's localSymbol is OCC-padded (e.g. 'QQQ   260420C00645000');
        # downstream regex in ib_occ_to_contract requires the canonical
        # unpadded form or price lookup silently fails and the trade
        # becomes un-monitorable. Strip ALL whitespace, not just ends.
        sym = "".join((pos.get("symbol") or "").split())
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
        adopted_items.append(
            f"{ticker} {sym} {qty}x@${avg_cost:.2f} {direction} "
            f"db_id={trade.get('db_id', 'MISSING')}"
        )
        log.info(f"[RECONCILE] Adopted {ticker} {sym} (conId={con_id}): "
                 f"{qty}x @ ${avg_cost:.2f} {direction} db_id={trade.get('db_id', 'MISSING')}")
        from strategy.audit import log_trade_action
        log_trade_action(
            trade.get("db_id"), "reconcile_adopt", "reconciliation",
            f"IB orphan → adopted {sym} {qty}x @ ${avg_cost:.2f} {direction}",
            level="warn",  # adoption means we had a DB/IB mismatch — always worth a warn
            extra={"ticker": ticker, "symbol": sym,
                   "direction": direction, "contracts": qty,
                   "entry_price": avg_cost, "ib_con_id": con_id},
        )

    # ══════════════════════════════════════════════════════════
    # PASS 3: Orphan bracket detection
    # ══════════════════════════════════════════════════════════
    # Working SELL orders with no matching open DB trade and no
    # positive IB position to sell from. If they fire they go short.
    # Stateful detector with a grace period — see
    # docs/orphan_bracket_detector.md.
    orphan_items: list[str] = []
    try:
        open_con_ids_after = _get_db_open_con_ids()
        # Positions already fetched at the top of this function
        ib_qty_by_con_id = {cid: int(pos.get("qty", 0))
                            for cid, pos in ib_by_con_id.items()}
        # Include zero-qty adopted contracts too (rare)
        detector = _get_orphan_detector()
        orphans = detector.scan(client, open_con_ids_after, ib_qty_by_con_id)
        for o in orphans:
            orphan_items.append(
                f"orderId={o.get('orderId')} {o.get('symbol')} "
                f"{o.get('orderType')} @ "
                f"${o.get('lmtPrice') or o.get('auxPrice')} "
                f"({o.get('_outcome', 'handled')})"
            )
    except Exception as e:
        log.warning(f"[RECONCILE] PASS 3 orphan detector error: {e}")

    # ── Summary ──
    total_ib = len(ib_by_con_id)
    total_db = len(db_open_trades)
    orphan_count = len(orphan_items)
    headline = (f"closed={closed_count}, adopted={adopted_count}, "
                f"orphans={orphan_count}, "
                f"IB={total_ib}, DB was={total_db}, "
                f"DB now={total_db - closed_count + adopted_count}")

    # Build a detail string that names WHICH trades were touched. When
    # counts are 0 on both sides, omit detail so the log stays concise.
    details_parts: list[str] = []
    if closed_items:
        details_parts.append("closed: [" + "; ".join(closed_items) + "]")
    if adopted_items:
        details_parts.append("adopted: [" + "; ".join(adopted_items) + "]")
    if orphan_items:
        details_parts.append("orphans: [" + "; ".join(orphan_items) + "]")
    detail = (" | " + " | ".join(details_parts)) if details_parts else ""

    full_summary = headline + detail
    log.info(f"[RECONCILE] Done: {full_summary}")
    _log_to_db("info", f"Reconciliation: {full_summary}")
    _update_thread("idle", f"Done: {headline}")  # thread status stays terse
    log.info("=" * 50)

    exit_manager.invalidate_cache()  # Force refresh from DB on next access


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
    """
    Find the actual exit price from IB.

    Search order:
    1. IB fills by conId (exact match — most reliable)
    2. IB fills by ticker (broader match)
    3. Fall back to last known current_price from DB (stale but better than entry)
    4. Fall back to entry_price (worst case)
    """
    con_id = db_trade.get("ib_con_id")

    # Try conId-based fill search first (most precise)
    if con_id:
        try:
            fill = client.check_fill_by_conid(con_id)
            if fill and fill.get("price"):
                log.info(f"[RECONCILE] Found IB fill by conId={con_id} for {ticker}: "
                         f"${fill['price']:.2f}")
                return fill["price"]
        except Exception:
            pass

    # Try ticker-based fill search
    try:
        fill = client.check_recent_fills(ticker)
        if fill and fill.get("price"):
            log.info(f"[RECONCILE] Found IB fill by ticker for {ticker}: ${fill['price']:.2f}")
            return fill["price"]
    except Exception:
        pass

    # Fall back to last live price from DB (updated every 5s by exit_manager)
    # This is the price BEFORE the trade closed — not ideal but better than entry
    try:
        from db.connection import get_session
        from sqlalchemy import text
        session = get_session()
        if session:
            row = session.execute(
                text("SELECT current_price FROM trades WHERE id = :id"),
                {"id": db_trade.get("id")}
            ).fetchone()
            session.close()
            if row and row[0] and float(row[0]) > 0:
                price = float(row[0])
                log.warning(f"[RECONCILE] No IB fill found for {ticker} — "
                            f"using last DB current_price: ${price:.2f}")
                return price
    except Exception:
        pass

    # Last resort — entry price
    entry = db_trade.get("entry_price", 0)
    log.warning(f"[RECONCILE] No exit price found for {ticker} — using entry_price: ${entry}")
    return entry


    # _remove_from_exit_manager removed — ARCH-001: DB is source of truth.
    # exit_manager.invalidate_cache() causes it to refresh from DB on next access.


def _log_to_db(level, message):
    """Best-effort log to system_log table."""
    try:
        from db.writer import add_system_log
        add_system_log("reconciliation", level, message)
    except Exception:
        pass


def _update_thread(status, message):
    """Update reconciliation thread_status for dashboard visibility."""
    try:
        from db.writer import update_thread_status
        update_thread_status("reconciliation", None, status, message)
    except Exception:
        pass
