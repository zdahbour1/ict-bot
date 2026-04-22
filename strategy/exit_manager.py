"""
Exit Manager — orchestrates trade monitoring, exit decisions, and position closure.

ARCH-001: The DATABASE is the single source of truth for all trade state.
The exit_manager reads open trades from DB every cycle (cached 5s).
No in-memory list or JSON file acts as a parallel source of truth.

ARCH-002: Trade state transitions (open→closed) use row-level locking
(SELECT FOR UPDATE) in close_trade() to prevent double-close.

Responsibilities:
- Read open trades from DB every 5 seconds
- Fetch batch IB prices for all open trades
- Evaluate exit conditions (TP, SL, trail, time, EOD, roll)
- Execute exits: cancel brackets → verify position → sell
- Update DB for all state changes
- Process UI commands (close from dashboard)
"""
import logging
import threading
import time
from datetime import datetime
import pytz

from alerts.emailer import send_trade_result_email
from strategy.exit_conditions import evaluate_exit, update_trailing_stop
from strategy.exit_executor import execute_exit, execute_roll
from strategy.trade_logger import log_trade_result, collect_exit_enrichment
from strategy.reconciliation import periodic_reconciliation
from strategy.error_handler import handle_error, safe_call
import config

log = logging.getLogger(__name__)
PT = pytz.timezone("America/Los_Angeles")


from utils.occ_parser import is_expired as _is_expired


class ExitManager:
    """
    Trade monitoring engine. Reads open trades from DB (source of truth).

    The open_trades property returns a cached list from DB, refreshed every 5s.
    add_trade() writes to DB FIRST — if DB fails, trade is NOT tracked.
    """

    def __init__(self, client):
        self.client = client
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        # DB-backed cache
        self._open_trades_cache = []
        self._cache_time = 0
        self._CACHE_TTL = 5  # seconds
        # Load initial state from DB
        self._refresh_cache()
        if self._open_trades_cache:
            log.info(f"Loaded {len(self._open_trades_cache)} open trade(s) from database")

    # ── DB-backed trade list (ARCH-001) ──────────────────────

    def _refresh_cache(self):
        """Refresh open trades cache from database."""
        try:
            from db.writer import get_open_trades_from_db
            trades = get_open_trades_from_db()
            if trades is not None:
                self._open_trades_cache = trades
                self._cache_time = time.time()
        except Exception as e:
            handle_error("exit_manager", "refresh_cache", e)

    @property
    def open_trades(self) -> list:
        """Get open trades. Cached from DB, refreshed every 5 seconds."""
        if time.time() - self._cache_time >= self._CACHE_TTL:
            self._refresh_cache()
        return self._open_trades_cache

    def invalidate_cache(self):
        """Force cache refresh on next access."""
        self._cache_time = 0

    # ── Trade management (DB-first) ─────��────────────────────

    def add_trade(self, trade: dict):
        """
        Add a new trade. DB-FIRST: writes to DB, then invalidates cache.
        If DB write fails, trade is NOT tracked (fail-safe).

        ARCH-006: Checks DB for existing open trade on same ticker before INSERT
        to prevent duplicate entries from race conditions (scanner + roller).
        """
        trade["peak_pnl_pct"] = trade.get("peak_pnl_pct", 0.0)
        trade["dynamic_sl_pct"] = trade.get("dynamic_sl_pct", -config.STOP_LOSS)
        ticker = trade.get("ticker", "UNK")

        # ARCH-006: Check for existing open trade on same ticker OR same conId
        con_id = trade.get("ib_con_id")
        try:
            from sqlalchemy import text
            from db.connection import get_session
            session = get_session()
            if session:
                # Check by conId first (exact match), then by ticker.
                # Phase 2c: ib_con_id moved to trade_legs — join on leg_index=0
                # (single-leg trades) so the duplicate guard still works.
                if con_id:
                    existing = session.execute(
                        text(
                            "SELECT t.id FROM trades t "
                            "JOIN trade_legs l ON l.trade_id = t.id AND l.leg_index = 0 "
                            "WHERE l.ib_con_id = :cid AND t.status = 'open' LIMIT 1"
                        ),
                        {"cid": con_id}
                    ).fetchone()
                else:
                    existing = session.execute(
                        text("SELECT id FROM trades WHERE ticker = :ticker AND status = 'open' LIMIT 1"),
                        {"ticker": ticker}
                    ).fetchone()
                session.close()
                if existing:
                    log.warning(f"[{ticker}] DUPLICATE GUARD: open trade already exists "
                                f"(db_id={existing[0]}, conId={con_id}) — skipping add_trade")
                    return
        except Exception as e:
            log.warning(f"[{ticker}] Duplicate check failed: {e} — proceeding with insert")

        try:
            from db.writer import insert_trade
            db_id = insert_trade(trade, config.IB_ACCOUNT or "unknown")
            if db_id:
                trade["db_id"] = db_id
                log.info(f"Trade saved to DB: id={db_id} {ticker} {trade.get('symbol')}")
                self.invalidate_cache()
            else:
                handle_error("exit_manager", "add_trade_db",
                             RuntimeError("insert_trade returned None — trade NOT tracked"),
                             context={"ticker": ticker, "symbol": trade.get("symbol")},
                             critical=True)
        except Exception as e:
            handle_error("exit_manager", "add_trade_db", e,
                         context={"ticker": ticker, "symbol": trade.get("symbol")},
                         critical=True)

    def start(self):
        thread = threading.Thread(target=self._monitor_loop, daemon=True)
        thread.start()
        log.info("Exit manager started.")

    def stop(self):
        self._stop_event.set()

    # ── Main monitoring loop ──────────────────────────────
    def _monitor_loop(self):
        reconcile_counter = 0
        heartbeat_counter = 0
        reconcile_interval = config.RECONCILIATION_INTERVAL_MIN * 60 // config.MONITOR_INTERVAL
        while not self._stop_event.is_set():
            try:
                self._check_exits()
            except Exception as e:
                handle_error("exit_manager", "check_exits_loop", e, critical=True)

            reconcile_counter += 1
            if reconcile_counter >= reconcile_interval:
                reconcile_counter = 0
                try:
                    periodic_reconciliation(self.client, self)
                    self.invalidate_cache()  # Reconciliation may have changed DB state
                except Exception as e:
                    handle_error("exit_manager", "periodic_reconciliation", e)

            # Heartbeat every 30s
            heartbeat_counter += 1
            if heartbeat_counter >= 6:
                heartbeat_counter = 0
                try:
                    from db.writer import update_thread_status
                    update_thread_status(
                        "exit_manager", None, "running",
                        f"Monitoring {len(self.open_trades)} trades",
                    )
                except Exception:
                    pass

            time.sleep(config.MONITOR_INTERVAL)

    def _check_exits(self):
        from strategy.market_hours import get_market_clock
        clock = get_market_clock()
        now_pt = clock.now_pt

        # Read open trades from DB (cached, refreshes every 5s)
        trades = list(self.open_trades)  # Copy to avoid mutation during iteration

        if not trades:
            return

        # ── Hard cutoff gate ───────────────────────────────
        # After market close no order will fill. Sending new MKT SELLs
        # just piles up parked orders. Skip the entire exit pipeline.
        # See docs/market_hours_guards.md and the 2026-04-20 afternoon
        # retry-storm that motivated this guard.
        if clock.is_past_close():
            return

        # ── EOD sweep window ───────────────────────────────
        # In the last N minutes before close we force-close every
        # open trade with reason='EOD' so MKT SELLs can fill while
        # the market is still open. Each trade is closed at most
        # once per session via a per-trade session flag.
        if clock.in_eod_sweep_window():
            self._run_eod_sweep(trades)
            # After the sweep, exit early — do NOT run normal
            # exit_conditions in the last 5 min. Avoids a race where
            # TP/SL/ROLL triggers fire at 12:58 on the same trade
            # the sweep just touched.
            return

        # ── Remove expired contracts (using locked close) ─
        for trade in trades:
            if _is_expired(trade.get("symbol", "")):
                ticker = trade.get("ticker", "UNK")
                db_id = trade.get("db_id")
                log.warning(f"[{ticker}] Contract EXPIRED: {trade['symbol']} — auto-closing")
                if db_id:
                    from db.writer import close_trade
                    close_trade(db_id, trade.get("entry_price", 0), "LOSS", "EXPIRED", {"detail": trade.get("symbol", "")})
                self.invalidate_cache()

        # Re-read after expiry closures
        trades = [t for t in self.open_trades if not _is_expired(t.get("symbol", ""))]

        # ── Batch fetch all prices ────────────────────
        symbols = [t["symbol"] for t in trades]
        try:
            batch_prices = self.client.get_option_prices_batch(symbols)
        except Exception as e:
            handle_error("exit_manager", "batch_price_fetch", e,
                         context={"symbol_count": len(symbols)})
            batch_prices = {}

        # ── Bulk DB update for priced trades ──────────
        if batch_prices:
            for trade in trades:
                price = batch_prices.get(trade["symbol"])
                if price and trade.get("db_id"):
                    entry = trade["entry_price"]
                    pnl = (price - entry) / entry if entry > 0 else 0
                    pnl_usd = (price - entry) * 100 * trade.get("contracts", 0)
                    try:
                        from db.writer import update_trade_price
                        update_trade_price(trade["db_id"], price, pnl, pnl_usd,
                                           trade.get("peak_pnl_pct", 0),
                                           trade.get("dynamic_sl_pct", -0.6))
                    except Exception as e:
                        handle_error("exit_manager", "update_trade_price", e,
                                     context={"db_id": trade.get("db_id"),
                                              "ticker": trade.get("ticker")})

        # ── Process each trade ────────────────────────
        for trade in trades:
            try:
                current_price = batch_prices.get(trade["symbol"])
                if current_price is None:
                    continue

                entry_price = trade["entry_price"]
                pnl_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0

                # Update peak (local tracking for this cycle)
                if pnl_pct > trade.get("peak_pnl_pct", 0):
                    trade["peak_pnl_pct"] = pnl_pct

                # Update trailing stop + bracket SL on IB
                old_sl = trade.get("dynamic_sl_pct", -config.STOP_LOSS)
                trade["dynamic_sl_pct"] = update_trailing_stop(trade, pnl_pct)
                if trade["dynamic_sl_pct"] != old_sl and trade.get("ib_sl_order_id"):
                    new_sl_price = round(entry_price * (1 + trade["dynamic_sl_pct"]), 2)
                    try:
                        self.client.update_bracket_sl(trade["ib_sl_order_id"], new_sl_price)
                        log.info(f"[{trade.get('ticker')}] Bracket SL → ${new_sl_price:.2f}")
                    except Exception as e:
                        handle_error("exit_manager", "update_bracket_sl", e,
                                     context={"ticker": trade.get("ticker"),
                                              "sl_order_id": trade.get("ib_sl_order_id")})

                log.info(
                    f"[{trade.get('ticker')}] MONITOR db_id={trade.get('db_id')} "
                    f"{trade.get('symbol')} | "
                    f"${current_price:.2f} | "
                    f"P&L:{pnl_pct:+.1%} | "
                    f"Peak:{trade.get('peak_pnl_pct', 0):+.1%} | "
                    f"SL:{trade.get('dynamic_sl_pct', 0):+.1%} | "
                    f"conId={trade.get('ib_con_id')} "
                    f"TP_id={trade.get('ib_tp_order_id')} SL_id={trade.get('ib_sl_order_id')}"
                )

                # ── Evaluate exit conditions ──────────
                exit_info = evaluate_exit(trade, current_price, now_pt)

                if exit_info:
                    result = exit_info["result"]
                    reason = exit_info["reason"]
                    reason_detail = exit_info.get("reason_detail", "")
                    should_roll = exit_info.get("should_roll", False)

                    # ── Atomic close: lock DB → IB work → update DB → release ──
                    self._atomic_close(trade, current_price, result, reason,
                                       pnl_pct, should_roll, reason_detail=reason_detail)

            except Exception as e:
                handle_error("exit_manager", "monitor_trade", e,
                             context={"symbol": trade.get("symbol", "?"),
                                      "ticker": trade.get("ticker", "?")},
                             critical=True)

        # ── Process UI commands ───────────────────────
        self._process_ui_commands()

    def _run_eod_sweep(self, trades: list) -> None:
        """Force-close every open trade with reason='EOD'.

        Called from ``_check_exits`` when ``MarketClock.in_eod_sweep_window()``
        is true. The lead minutes buffer (default 5 min before close)
        gives MKT SELLs time to fill on IB.

        Uses the normal ``_atomic_close`` machinery so the close follows
        the same atomic-lock + bracket-cancel + verify flow as TP/SL
        exits. Audit row ``close:EOD`` is emitted per trade.

        De-duplication: we stamp a per-trade per-session flag so repeat
        calls to _check_exits within the same EOD window don't re-
        enter _atomic_close on the same trade. The flag lives in the
        in-memory trade dict, not the DB — it's intentionally session-
        scoped so a bot restart at 12:58 PT starts fresh.
        """
        swept = 0
        for trade in trades:
            if trade.get("_eod_closed_this_session"):
                continue  # already handed to _atomic_close this session
            ticker = trade.get("ticker", "UNK")
            db_id = trade.get("db_id")
            current_price = trade.get("current_price") or trade.get("entry_price", 0)
            entry = trade.get("entry_price", 0) or 0
            pnl_pct = ((current_price - entry) / entry) if entry else 0.0
            result = ("WIN" if pnl_pct > 0.001
                      else "LOSS" if pnl_pct < -0.001
                      else "SCRATCH")
            log.warning(
                f"[{ticker}] EOD SWEEP: db_id={db_id} closing at "
                f"${current_price:.2f} (P&L {pnl_pct:+.1%}) — market closes soon"
            )
            trade["_eod_closed_this_session"] = True
            try:
                self._atomic_close(
                    trade, current_price, result, "EOD", pnl_pct,
                    should_roll=False,
                    reason_detail="force-close in EOD sweep window",
                )
                swept += 1
            except Exception as e:
                log.error(f"[{ticker}] EOD SWEEP: _atomic_close failed: {e}")
        if swept:
            log.warning(f"[EOD SWEEP] force-closed {swept} open trade(s)")

    def _verify_close_on_ib(self, trade: dict) -> bool:
        """Post-close verification (Fix C — docs/roll_close_bug_fixes.md).

        Poll IB for up to 3 seconds, expecting:
          1. Position qty for this contract = 0 (our SELL filled, or
             the bracket fired)
          2. No remaining working orders (any stragglers get cancelled
             defensively to prevent a later bracket trigger from
             flipping us short)

        Returns True if IB side is clean, False if position is still
        non-zero after the poll window. False means the caller MUST
        release the DB lock WITHOUT calling finalize_close so the next
        exit cycle retries.

        If we cannot verify (no conId, IB queries all fail) we return
        True to avoid blocking the close — the pre-existing behavior.
        The caller's reconciliation loop is the backstop.
        """
        import time
        ticker = trade.get("ticker", "UNK")
        con_id = trade.get("ib_con_id")
        symbol = trade.get("symbol", "")

        if not con_id:
            log.warning(f"[{ticker}] VERIFY CLOSE: no conId — skipping verification")
            return True

        # _bracket_fired flag is set by execute_exit when the bracket
        # closed the position before we could send our own SELL. In
        # that case we already know position is 0; skip the poll.
        if trade.get("_bracket_fired"):
            log.info(f"[{ticker}] VERIFY CLOSE: bracket already fired, skipping poll")
            # Still refresh + sweep orders defensively
            self._cancel_stragglers(trade)
            return True

        # Poll position up to 3s (6 × 500ms). Our SELL may still be
        # working when we arrive here; give it time to fill.
        final_qty = None
        for attempt in range(6):
            time.sleep(0.5)
            try:
                qty = self.client.get_position_quantity(con_id)
                final_qty = qty
                if qty == 0:
                    log.info(f"[{ticker}] VERIFY CLOSE: position=0 after "
                             f"{(attempt + 1) * 0.5:.1f}s — sweeping stragglers")
                    self._cancel_stragglers(trade)
                    # Re-check position after sweeping — a straggler can
                    # fill between our position=0 read and the cancel
                    # hitting IB. If the post-sweep position is negative,
                    # trigger the recovery buy (see below).
                    try:
                        post_sweep_qty = self.client.get_position_quantity(con_id)
                    except Exception:
                        post_sweep_qty = 0
                    if post_sweep_qty < 0:
                        log.error(
                            f"[{ticker}] VERIFY CLOSE: position went NEGATIVE "
                            f"after sweep ({post_sweep_qty}) — a straggler "
                            f"bracket fired during cleanup. Attempting recovery BUY."
                        )
                        return self._recover_negative_position(
                            trade, post_sweep_qty, reason="sweep_race"
                        )
                    return True
                if qty < 0:
                    # Direct negative — a bracket fired before or during
                    # our flatten attempt. Try to recover.
                    log.error(
                        f"[{ticker}] VERIFY CLOSE: position is NEGATIVE ({qty}) "
                        f"during polling — bracket fired after our close SELL. "
                        f"Attempting recovery BUY."
                    )
                    return self._recover_negative_position(
                        trade, qty, reason="late_bracket_fill"
                    )
            except Exception as e:
                log.warning(f"[{ticker}] VERIFY CLOSE attempt {attempt}: {e}")

        log.error(f"[{ticker}] VERIFY CLOSE FAILED: position still "
                  f"{final_qty} after 3s (symbol={symbol}, conId={con_id}) — "
                  f"releasing DB lock, will retry next cycle")
        return False

    def _recover_negative_position(self, trade: dict, qty: int,
                                    reason: str) -> bool:
        """Defensive BUY to restore flat after a straggler bracket fired
        and flipped us short.

        ONLY fires when ``qty < 0``. Buys exactly ``abs(qty)`` contracts
        at market. Writes an ``AUDIT short_recovery_buy`` row so the
        incident is permanently traceable.

        Returns False regardless of success — the caller should NOT
        finalize_close; the trade stays open while reconcile sorts
        out the DB/IB state on the next cycle. That keeps the lock
        logic conservative: the presence of a short means we're still
        in an abnormal state that a human may want to see.
        """
        from strategy.audit import log_trade_action

        ticker = trade.get("ticker", "UNK")
        symbol = trade.get("symbol", "")
        trade_id = trade.get("db_id")
        to_buy = abs(int(qty))

        if to_buy <= 0:
            return False

        log_trade_action(
            trade_id, "short_recovery_attempt", "exit_manager",
            f"position={qty} — attempting recovery BUY of {to_buy}x {symbol}",
            level="error",
            extra={"ticker": ticker, "symbol": symbol,
                   "position_before_recovery": qty, "reason": reason},
        )

        try:
            direction = trade.get("direction", "LONG")
            # The original trade held long options. To unwind a short we
            # BUY back the same contract. Use the same client call the
            # open path uses but in BUY direction.
            #
            # We reuse buy_call / buy_put — they take a bare OCC symbol
            # and a count. If the client exposes a more neutral helper,
            # prefer that; until then these two branches cover
            # everything the bot ever trades today.
            if direction == "LONG":
                # long calls — close a short in calls by buying calls
                fill = self.client.buy_call(symbol, to_buy)
            else:
                # long puts (ICT bearish) — close a short in puts by buying puts
                fill = self.client.buy_put(symbol, to_buy)
            log.warning(f"[{ticker}] RECOVERY BUY submitted for {to_buy}x {symbol}. "
                        f"Fill response: {fill}")
            log_trade_action(
                trade_id, "short_recovery_buy", "exit_manager",
                f"recovery BUY submitted: {to_buy}x {symbol}",
                level="warn",
                extra={"ticker": ticker, "symbol": symbol,
                       "qty_bought": to_buy, "reason": reason,
                       "fill_response": str(fill)[:500]},
            )
        except Exception as e:
            log.error(f"[{ticker}] RECOVERY BUY FAILED: {e} — BOT NEEDS HUMAN "
                      f"ATTENTION. Position is short {to_buy}x {symbol}.")
            log_trade_action(
                trade_id, "short_recovery_failed", "exit_manager",
                f"recovery BUY FAILED: {e} — still short {to_buy}x",
                level="error",
                extra={"ticker": ticker, "symbol": symbol,
                       "error": str(e)[:500], "reason": reason},
            )

        # Always return False — we don't want to finalize_close with a
        # trade that may still be in an abnormal state on IB. Reconcile
        # will pick up the actual situation next cycle.
        return False

    def _cancel_stragglers(self, trade: dict) -> None:
        """Cancel any leftover working orders for this contract. Called
        after a verified close to prevent a forgotten bracket from
        firing later and flipping us into a short position (the IWM
        incident 2026-04-20)."""
        ticker = trade.get("ticker", "UNK")
        con_id = trade.get("ib_con_id")
        symbol = (trade.get("symbol") or "").replace(" ", "")
        if not con_id:
            return
        try:
            # Refresh cross-client view first (Fix A)
            self.client.refresh_all_open_orders()
            stragglers = self.client.find_open_orders_for_contract(con_id, symbol)
        except Exception as e:
            log.warning(f"[{ticker}] VERIFY CLOSE: straggler query failed: {e}")
            return
        if not stragglers:
            return
        log.warning(f"[{ticker}] VERIFY CLOSE: {len(stragglers)} straggler "
                    f"orders found after close — cancelling defensively: "
                    f"{[o.get('orderId') for o in stragglers]}")
        for o in stragglers:
            try:
                self.client.cancel_order_by_id(o["orderId"])
            except Exception as e:
                log.warning(f"[{ticker}] VERIFY CLOSE: failed to cancel "
                            f"orderId={o.get('orderId')}: {e}")

    def _atomic_close(self, trade: dict, current_price: float, result: str,
                      reason: str, pnl_pct: float, should_roll: bool,
                      reason_detail: str = ""):
        """
        Atomic close: lock DB → read CURRENT state → IB work → update DB → release.

        CRITICAL: After locking, we read the CURRENT DB state — NOT the stale
        trade dict from cache. The trade dict may be seconds old. In a multi-threaded
        system with IB firing orders independently, stale data causes double-closes.
        """
        from db.writer import lock_trade_for_close, finalize_close, release_trade_lock

        trade_id = trade.get("db_id")
        ticker = trade.get("ticker", "UNK")

        if not trade_id:
            log.warning(f"[{ticker}] Cannot atomic close — no db_id")
            return

        # Step 1: Lock the DB record (NOWAIT — skip if another thread has it)
        session, locked_data = lock_trade_for_close(trade_id)
        if not session:
            log.info(f"[{ticker}] Trade {trade_id} already closed or locked — skipping")
            self.invalidate_cache()
            return

        # Step 2: Build live_trade from LOCKED DB data — this is the CURRENT truth
        # Do NOT rely on the stale cached trade dict for any state decisions
        live_trade = {
            "db_id": locked_data["id"],
            "ticker": locked_data["ticker"],
            "symbol": locked_data["symbol"],
            "contracts": locked_data["contracts_open"],
            "entry_price": locked_data["entry_price"],
            "direction": locked_data["direction"],
            "ib_con_id": locked_data["ib_con_id"],
            "ib_order_id": locked_data["ib_order_id"],
            "ib_perm_id": locked_data["ib_perm_id"],
            "ib_tp_order_id": locked_data["ib_tp_order_id"],
            "ib_sl_order_id": locked_data["ib_sl_order_id"],
            # These come from cache — acceptable for exit conditions/enrichment
            "peak_pnl_pct": trade.get("peak_pnl_pct", 0),
            "dynamic_sl_pct": trade.get("dynamic_sl_pct", -0.6),
            "entry_time": trade.get("entry_time"),
        }

        log.info(f"[{ticker}] ATOMIC CLOSE: db_id={trade_id} LOCKED. "
                 f"DB state: {locked_data['contracts_open']} contracts, "
                 f"conId={locked_data['ib_con_id']}, "
                 f"direction={locked_data['direction']}, "
                 f"symbol={locked_data['symbol']}")

        try:
            # Step 3: IB work using live_trade (current DB state)
            if should_roll:
                rolled = safe_call(
                    execute_roll, self.client, live_trade, pnl_pct,
                    component="exit_manager", operation="execute_roll",
                    context={"ticker": ticker}
                )
            else:
                execute_exit(self.client, live_trade, reason)
                rolled = None

            # ── FIX C (docs/roll_close_bug_fixes.md) ─────────────────
            # Verify IB actually flattened BEFORE we mark the trade
            # closed in the DB. If the sell never fired (Bug A or B),
            # or if it fired but the fill was partial, or if brackets
            # stayed alive and caused a position flip, we MUST NOT
            # call finalize_close. Release the lock and let the next
            # exit cycle retry. Violates ARCH-005 otherwise.
            #
            # EXCEPTION — legitimate roll (SPY 2026-04-21 incident fix).
            # When execute_roll returns a NEW trade with a different
            # symbol, the OLD position was closed by execute_exit's own
            # post-SELL verification. Running _verify_close_on_ib against
            # the old conId here is misleading because the new position
            # may be on the same contract family (even if picked at a
            # different strike, the symbol differs but the account-level
            # positions call is scoped by conId so should be fine). We
            # trust execute_roll's close-then-open; skip this check.
            if should_roll and rolled is not None:
                log.info(f"[{ticker}] VERIFY CLOSE: skipped — legitimate roll "
                         f"to {rolled.get('symbol')}; trusting execute_roll's "
                         f"own post-SELL verification")
                close_ok = True
            else:
                close_ok = self._verify_close_on_ib(live_trade)
            from strategy.audit import log_trade_action
            if not close_ok:
                log.error(
                    f"[{ticker}] CLOSE VERIFICATION FAILED — not finalizing DB close. "
                    f"db_id={trade_id} will stay open, next exit cycle will retry."
                )
                log_trade_action(
                    trade_id, "verify_close_fail", "exit_manager",
                    f"position did not flatten after 3s — releasing lock, will retry",
                    level="error",
                    extra={"ticker": ticker, "symbol": live_trade.get("symbol"),
                           "reason": reason, "ib_con_id": live_trade.get("ib_con_id")},
                )
                release_trade_lock(session)
                self.invalidate_cache()
                return
            # Verification passed
            log_trade_action(
                trade_id, "verify_close_ok", "exit_manager",
                "IB position flattened + stragglers swept",
                extra={"ticker": ticker, "symbol": live_trade.get("symbol"),
                       "reason": reason},
            )

            # Step 4: Collect enrichment
            exit_enrichment = safe_call(
                collect_exit_enrichment, self.client, live_trade,
                component="exit_manager", operation="collect_exit_enrichment",
                default={}, context={"ticker": ticker}
            )

            if reason_detail:
                exit_enrichment["reason_detail"] = reason_detail
            if should_roll:
                exit_enrichment["roll_pnl_pct"] = round(pnl_pct * 100, 1)
                exit_enrichment["roll_from_symbol"] = live_trade.get("symbol", "")

            # Step 5: CSV log
            log_trade_result(live_trade, current_price, result, reason, exit_enrichment)

            # Step 6: Finalize in DB and release lock
            finalize_close(session, trade_id, current_price, result, reason, exit_enrichment)
            log.info(f"[{ticker}] ATOMIC CLOSE COMPLETE: db_id={trade_id} → {result} ({reason})")
            log_trade_action(
                trade_id, f"close:{reason}", "exit_manager",
                f"closed {live_trade.get('symbol')} @ ${current_price:.2f} → {result} ({reason})",
                extra={
                    "ticker": ticker, "symbol": live_trade.get("symbol"),
                    "direction": live_trade.get("direction"),
                    "contracts": live_trade.get("contracts"),
                    "exit_price": current_price,
                    "result": result,
                    "reason": reason,
                    "reason_detail": reason_detail,
                    "pnl_pct": round(pnl_pct * 100, 2) if pnl_pct else None,
                    "rolled": bool(should_roll),
                },
            )

            # Post-close actions (after lock released)
            if not should_roll:
                safe_call(send_trade_result_email, live_trade, result, current_price,
                          component="exit_manager", operation="send_trade_result_email",
                          context={"ticker": ticker})

            if should_roll and rolled:
                self.add_trade(rolled)
                log.info(f"[{ticker}] Roll complete: "
                         f"closed {live_trade.get('symbol')} → opened {rolled.get('symbol')}")
                log_trade_action(
                    rolled.get("db_id"), "roll_open", "exit_manager",
                    f"rolled from {live_trade.get('symbol')} → "
                    f"{rolled.get('symbol')} @ ${rolled.get('entry_price', 0):.2f}",
                    extra={
                        "ticker": ticker,
                        "from_symbol": live_trade.get("symbol"),
                        "from_trade_id": trade_id,
                        "to_symbol": rolled.get("symbol"),
                        "entry_price": rolled.get("entry_price"),
                    },
                )

            self.invalidate_cache()

        except Exception as e:
            release_trade_lock(session)
            handle_error("exit_manager", "atomic_close", e,
                         context={"trade_id": trade_id, "ticker": ticker},
                         critical=True)
            self.invalidate_cache()

    def _process_ui_commands(self):
        """Process close commands from the dashboard using atomic close."""
        try:
            from db.writer import check_pending_commands, complete_command
            commands = check_pending_commands()
            for cmd in (commands or []):
                try:
                    target = None
                    for t in self.open_trades:
                        if t.get("db_id") == cmd["trade_id"]:
                            target = t
                            break
                    if target:
                        contracts = cmd.get("contracts") or target.get("contracts", 0)
                        reason = "UI_CLOSE"
                        # Use atomic close: lock → IB close → DB update → release
                        self._atomic_close(target, target.get("current_price", 0),
                                           "SCRATCH", reason, pnl_pct=0, should_roll=False)
                        log.info(f"UI command executed: {reason} {target['symbol']}")
                        complete_command(cmd["id"])
                    else:
                        error_msg = f"Trade {cmd['trade_id']} not found in open trades"
                        log.warning(f"UI command failed: {error_msg}")
                        complete_command(cmd["id"], error=error_msg)
                except Exception as e:
                    handle_error("exit_manager", "process_ui_command", e,
                                 context={"command_id": cmd.get("id"),
                                          "trade_id": cmd.get("trade_id")})
                    try:
                        complete_command(cmd["id"], error=str(e))
                    except Exception as e2:
                        handle_error("exit_manager", "complete_command_error", e2)
        except Exception as e:
            handle_error("exit_manager", "check_pending_commands", e)
