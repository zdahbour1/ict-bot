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

        # ARCH-006: Check for existing open trade on same ticker
        try:
            from sqlalchemy import text
            from db.connection import get_session
            session = get_session()
            if session:
                existing = session.execute(
                    text("SELECT id FROM trades WHERE ticker = :ticker AND status = 'open' LIMIT 1"),
                    {"ticker": ticker}
                ).fetchone()
                session.close()
                if existing:
                    log.warning(f"[{ticker}] DUPLICATE GUARD: open trade already exists "
                                f"(db_id={existing[0]}) — skipping add_trade")
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
        now_pt = datetime.now(PT)

        # Read open trades from DB (cached, refreshes every 5s)
        trades = list(self.open_trades)  # Copy to avoid mutation during iteration

        if not trades:
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

            # Post-close actions (after lock released)
            if not should_roll:
                safe_call(send_trade_result_email, live_trade, result, current_price,
                          component="exit_manager", operation="send_trade_result_email",
                          context={"ticker": ticker})

            if should_roll and rolled:
                self.add_trade(rolled)
                log.info(f"[{ticker}] Roll complete: "
                         f"closed {live_trade.get('symbol')} → opened {rolled.get('symbol')}")

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
