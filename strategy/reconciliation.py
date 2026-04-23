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
from sqlalchemy import text
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
        qty_raw = int(pos["qty"])
        # Store POSITIVE contracts; direction column carries the sign.
        # Writing negative contracts to trade_legs breaks every downstream
        # P&L calc + UI column (see 2026-04-23 AMZN -4x regression).
        qty = abs(qty_raw)
        avg_cost = pos.get("avg_cost", 0)
        right = pos.get("right", "C")
        # Market-bias direction. Depends on BOTH position sign and right,
        # not just right:
        #   qty>0 C → bought call  → LONG  (bullish)
        #   qty>0 P → bought put   → SHORT (bearish)
        #   qty<0 C → sold  call   → SHORT (bearish, naked)
        #   qty<0 P → sold  put    → LONG  (bullish, naked)
        if qty_raw >= 0:
            direction = "LONG" if right == "C" else "SHORT"
        else:
            direction = "SHORT" if right == "C" else "LONG"

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

    # ══════════════════════════════════════════════════════════
    # PASS 4: Per-trade bracket health audit
    # ══════════════════════════════════════════════════════════
    # For every OPEN DB trade, look up its recorded TP+SL permIds in
    # IB's order table and write their current status back to the
    # trade row. UI uses these fields to show "protected" vs
    # "UNPROTECTED" per trade. Also emits an 'unprotected_position'
    # audit when a trade's brackets have gone to a terminal non-fill
    # state (Cancelled / Inactive / missing) while the position is
    # still held — this is the exact state that led to today's
    # 10-position naked exposure incident.
    unprotected_items: list[str] = []
    try:
        from strategy.orphan_detector import OrphanBracketDetector
        _TERMINAL_BAD = {"Cancelled", "ApiCancelled", "Inactive"}
        _ACTIVE = OrphanBracketDetector._ACTIVE

        # Build a lookup of all live orders by permId (from the refresh
        # our orphan detector just ran). Cheap because we already have
        # the list.
        all_orders = []
        try:
            all_orders = client.get_all_working_orders()
        except Exception:
            pass
        orders_by_perm = {int(o["permId"]): o for o in all_orders
                          if o.get("permId")}

        from db.connection import get_session
        session = get_session()
        if session is not None:
            # Phase 2c: symbol, ib_tp_perm_id, ib_sl_perm_id all live on
            # trade_legs now; read via the flattened view.
            # ENH-046: also pull n_legs — multi-leg defined-risk trades
            # (iron condor, spread) don't want per-leg bracket restoration.
            rows = session.execute(text(
                "SELECT t.id, t.ticker, l.symbol, l.ib_tp_perm_id, "
                "       l.ib_sl_perm_id, COALESCE(t.n_legs, 1) "
                "FROM trades t "
                "JOIN trade_legs l ON l.trade_id = t.id AND l.leg_index = 0 "
                "WHERE t.status='open'"
            )).fetchall()
            for tid, tkr, sym, tp_pid, sl_pid, n_legs in rows:
                tp_status = None
                tp_price = None
                tp_order_id = None
                sl_status = None
                sl_price = None
                sl_order_id = None

                if tp_pid:
                    o = orders_by_perm.get(int(tp_pid))
                    if o:
                        tp_status = o.get("status")
                        tp_price = o.get("lmtPrice") or None
                        tp_order_id = o.get("orderId")
                    else:
                        # permId not in live IB orders → either
                        # cancelled/filled long ago OR never actually
                        # made it to IB. Mark MISSING.
                        tp_status = "MISSING"
                if sl_pid:
                    o = orders_by_perm.get(int(sl_pid))
                    if o:
                        sl_status = o.get("status")
                        sl_price = o.get("auxPrice") or o.get("lmtPrice") or None
                        sl_order_id = o.get("orderId")
                    else:
                        sl_status = "MISSING"

                # Write back to the trade row. Phase 2c: all bracket status
                # fields moved to trade_legs — target leg 0 (single-leg).
                session.execute(text(
                    "UPDATE trade_legs SET "
                    "  ib_tp_status=:tps, ib_sl_status=:sls, "
                    "  ib_tp_price=:tpp, ib_sl_price=:slp, "
                    "  ib_tp_order_id=:tpo, ib_sl_order_id=:slo, "
                    "  ib_brackets_checked_at=now() "
                    "WHERE trade_id=:id AND leg_index=0"
                ), {"tps": tp_status, "sls": sl_status,
                    "tpp": tp_price, "slp": sl_price,
                    "tpo": tp_order_id, "slo": sl_order_id,
                    "id": tid})

                # ENH-046: Multi-leg defined-risk trades (iron condors,
                # spreads, hedged) have their max loss capped by the
                # structure itself — per-leg SL brackets trigger
                # prematurely and are not how these positions are meant
                # to be protected. Skip the restore path for n_legs > 1;
                # risk is handled at the combo / trade-level instead.
                if int(n_legs or 1) > 1:
                    continue

                # Unprotected detection — both TP AND SL gone.
                tp_bad = tp_status is None or tp_status in _TERMINAL_BAD or tp_status == "MISSING"
                sl_bad = sl_status is None or sl_status in _TERMINAL_BAD or sl_status == "MISSING"
                if tp_bad and sl_bad:
                    unprotected_items.append(
                        f"{tkr} db_id={tid} {sym} "
                        f"(TP={tp_status or 'never placed'}, "
                        f"SL={sl_status or 'never placed'})"
                    )
                    # Audit emitted at warn — shows up in Trades tab Audit
                    try:
                        from strategy.audit import log_trade_action
                        log_trade_action(
                            tid, "unprotected_position", "reconciliation",
                            f"{sym} has no working bracket — TP={tp_status}, "
                            f"SL={sl_status}. Position still held; attempting "
                            f"bracket restoration.",
                            level="warn",
                            extra={"ticker": tkr, "symbol": sym,
                                   "tp_status": tp_status, "sl_status": sl_status,
                                   "tp_perm_id": tp_pid, "sl_perm_id": sl_pid},
                        )
                    except Exception:
                        pass

                    # ── ROLLBACK / RESTORATION ────────────────────
                    # Transaction semantics: if a cancel previously
                    # succeeded (leaving brackets gone) but the close
                    # didn't complete, compensate by placing fresh
                    # protection. Without this the position rides
                    # naked — exactly the failure mode we observed on
                    # 10 positions between 10:52-10:55 PT.
                    try:
                        _restore_brackets_for(session, client, tid, tkr, sym)
                    except Exception as e:
                        log.error(f"[RECONCILE] Bracket restoration FAILED "
                                  f"for db_id={tid} {tkr}: {e}")
                        try:
                            from strategy.audit import log_trade_action
                            log_trade_action(
                                tid, "bracket_restore_failed", "reconciliation",
                                f"{sym}: failed to restore brackets — {e}",
                                level="error",
                                extra={"ticker": tkr, "symbol": sym,
                                       "error": str(e)[:500]},
                            )
                        except Exception:
                            pass
            session.commit()
            session.close()
    except Exception as e:
        log.warning(f"[RECONCILE] PASS 4 bracket-status refresh error: {e}")

    # ── Summary ──
    total_ib = len(ib_by_con_id)
    total_db = len(db_open_trades)
    orphan_count = len(orphan_items)
    headline = (f"closed={closed_count}, adopted={adopted_count}, "
                f"orphans={orphan_count}, "
                f"unprotected={len(unprotected_items)}, "
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
    if unprotected_items:
        details_parts.append(
            "unprotected: [" + "; ".join(unprotected_items) + "]"
        )
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
        # Phase 2c: symbol/ib_con_id/entry_price/direction live on trade_legs;
        # read via the flattened single-leg view.
        rows = session.execute(
            text("SELECT id, ticker, symbol, ib_con_id, entry_price, direction "
                 "FROM v_trades_with_first_leg WHERE status='open'")
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


def _restore_brackets_for(session, client, trade_id: int, ticker: str, symbol: str) -> None:
    """Place fresh TP + SL protection on an existing long position.

    Implements the rollback leg of the "every IB action has a
    compensating action" principle (see
    docs/bracket_rollback_semantics.md). When a close flow sent cancels
    that eventually landed but the close SELL never fired — the bug
    pattern observed 2026-04-20 10:52–10:55 PT on 10 positions — the
    position is left unprotected. This helper is the compensating
    transaction: put fresh TP + SL onto IB so exposure is bounded
    again.

    Reads entry_price + profit_target + stop_loss_level from the trade
    row to reproduce the original bracket levels. If those are null
    we fall back to the global settings (PROFIT_TARGET=1.0,
    STOP_LOSS=0.6). Writes the new perm/order IDs back into the trade
    row so the next PASS 4 sees them as healthy.

    Never raises. Audits ``bracket_restored`` or
    ``bracket_restore_failed``. Runs synchronously on the reconcile
    thread — safe because reconcile already holds the 'one action at
    a time' lock.
    """
    from sqlalchemy import text
    # Read current state from DB so we use committed values. Phase 2c:
    # contracts_open/entry_price/profit_target/stop_loss_level moved to
    # trade_legs; client_trade_id still lives on the trades envelope.
    row = session.execute(text(
        "SELECT l.contracts_open, l.entry_price, l.profit_target, "
        "       l.stop_loss_level, t.client_trade_id "
        "FROM trades t "
        "JOIN trade_legs l ON l.trade_id = t.id AND l.leg_index = 0 "
        "WHERE t.id=:id"
    ), {"id": trade_id}).fetchone()
    if row is None:
        log.warning(f"[RECONCILE] restore: trade {trade_id} not found")
        return
    contracts, entry, tp_level, sl_level, existing_ref = row
    contracts = int(contracts or 0)
    if contracts <= 0:
        log.info(f"[RECONCILE] restore: trade {trade_id} has contracts_open={contracts}, skipping")
        return

    # Derive TP/SL option-premium prices. Fallback to config defaults
    # if the trade row doesn't have them (shouldn't normally happen).
    tp_price = float(tp_level) if tp_level else round(float(entry) * (1 + config.PROFIT_TARGET), 2)
    sl_price = float(sl_level) if sl_level else round(float(entry) * (1 - config.STOP_LOSS), 2)

    # IB rejects zero or negative stop prices
    if sl_price <= 0:
        sl_price = 0.05
    if tp_price <= 0:
        tp_price = round(float(entry) * 2, 2)

    log.warning(
        f"[RECONCILE] RESTORING BRACKETS for db_id={trade_id} {ticker} "
        f"{symbol} {contracts}x  TP=${tp_price:.2f}  SL=${sl_price:.2f}"
    )

    # Reuse the trade's original correlation ID so the restored
    # brackets carry the same tag. Easier to trace in audit logs.
    result = client.place_protection_brackets(
        symbol, contracts, tp_price, sl_price,
        order_ref=existing_ref,
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"place_protection_brackets returned {result!r}")

    tp_order_id = result.get("tp_order_id")
    tp_perm_id  = result.get("tp_perm_id")
    sl_order_id = result.get("sl_order_id")
    sl_perm_id  = result.get("sl_perm_id")

    # Phase 2c: every bracket column moved to trade_legs.
    session.execute(text(
        "UPDATE trade_legs SET "
        "  ib_tp_order_id=:tpo, ib_tp_perm_id=:tpp, ib_tp_price=:tpx, "
        "  ib_sl_order_id=:slo, ib_sl_perm_id=:slp, ib_sl_price=:slx, "
        "  ib_tp_status=:tps, ib_sl_status=:sls, "
        "  ib_brackets_checked_at=now() "
        "WHERE trade_id=:id AND leg_index=0"
    ), {"tpo": tp_order_id, "tpp": tp_perm_id, "tpx": tp_price,
        "slo": sl_order_id, "slp": sl_perm_id, "slx": sl_price,
        "tps": result.get("tp_status"), "sls": result.get("sl_status"),
        "id": trade_id})
    # commit happens in the outer PASS 4 block

    from strategy.audit import log_trade_action
    log_trade_action(
        trade_id, "bracket_restored", "reconciliation",
        f"{symbol}: restored TP @ ${tp_price:.2f} (permId={tp_perm_id}), "
        f"SL @ ${sl_price:.2f} (permId={sl_perm_id})",
        level="warn",  # warn because a restoration means something went wrong
        extra={"ticker": ticker, "symbol": symbol,
               "tp_perm_id": tp_perm_id, "sl_perm_id": sl_perm_id,
               "tp_price": tp_price, "sl_price": sl_price,
               "oca_group": result.get("oca_group")},
    )


def _get_db_open_con_ids() -> set:
    """Get the set of ALL ib_con_id values covered by open DB trades —
    across every leg of every open trade.

    The prior query joined only ``leg_index = 0`` which missed legs 1-3
    of any multi-leg trade. Reconciliation then saw those legs as
    orphan IB positions and re-adopted them as fresh single-leg ICT
    trades, creating duplicate DB rows AND duplicate SL brackets on
    the same contract (2026-04-23 COIN + MSFT regression). Must include
    every leg with contracts_open > 0 so an adopted DN iron condor
    doesn't get torn back apart.
    """
    try:
        from db.connection import get_session
        from sqlalchemy import text
        session = get_session()
        if not session:
            return set()
        rows = session.execute(
            text(
                "SELECT l.ib_con_id FROM trades t "
                "JOIN trade_legs l ON l.trade_id = t.id "
                "WHERE t.status='open' "
                "  AND l.leg_status='open' "
                "  AND l.contracts_open > 0 "
                "  AND l.ib_con_id IS NOT NULL"
            )
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
            # Phase 2c: current_price moved to trade_legs.
            row = session.execute(
                text(
                    "SELECT current_price FROM trade_legs "
                    "WHERE trade_id = :id AND leg_index = 0"
                ),
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
