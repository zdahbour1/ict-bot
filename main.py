"""
ICT QQQ Options Bot — Entry Point
Run this file to start the bot:  python main.py
"""
import logging
import os
import signal
import threading
import config
from strategy.exit_manager import ExitManager
from strategy.scanner import Scanner
from webhook.server import create_app

# ── Logging setup ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8")
    ]
)
log = logging.getLogger(__name__)


def main():
    log.info("=" * 60)
    log.info("ICT Multi-Ticker Options Bot starting...")
    log.info(f"Mode:      {'DRY RUN (no real orders)' if config.DRY_RUN else 'LIVE TRADING'}")

    # Active-strategy resolution (ENH-024 rollout #4).
    # Settings + tickers loaders already auto-scope to the active
    # strategy (rollouts #2+#3), so config.TICKERS / PROFIT_TARGET /
    # etc. reflect the right values. This block surfaces the decision
    # in the log + warns if the live scanner doesn't yet support the
    # chosen strategy directly.
    active_strategy_name = "ict"
    try:
        from db.settings_loader import (
            get_active_strategy_id, resolve_strategy_id,
        )
        from db.connection import get_session
        from sqlalchemy import text as _sql_text

        resolved_sid = resolve_strategy_id()
        s = get_session()
        if s and resolved_sid is not None:
            row = s.execute(_sql_text(
                "SELECT name, display_name, enabled FROM strategies "
                "WHERE strategy_id = :sid"
            ), {"sid": resolved_sid}).fetchone()
            s.close()
            if row:
                active_strategy_name = row[0]
                log.info(f"Strategy:  {row[1]} ({row[0]}, strategy_id={resolved_sid})")
                if active_strategy_name != "ict":
                    log.warning(
                        f"⚠ ACTIVE_STRATEGY is '{active_strategy_name}' but the "
                        f"live scanner still runs ICT only. "
                        f"Backtest fully supports '{active_strategy_name}'. "
                        f"Scanner plugin wiring ships in a follow-up branch."
                    )
    except Exception as e:
        log.warning(f"Could not resolve active strategy at boot: {e}")
        log.info("Strategy:  Raid + Displacement + iFVG/OB (default ICT)")

    log.info(f"Tickers:   {', '.join(config.TICKERS)}")
    for t in config.TICKERS:
        contracts = config.CONTRACTS_PER_TICKER.get(t, config.CONTRACTS)
        log.info(f"  {t}: {contracts} contracts")
    log.info(f"Option TP: {config.PROFIT_TARGET:.0%}   SL: {config.STOP_LOSS:.0%}")
    log.info(f"Window:    {config.TRADE_WINDOW_START_PT}:00-{config.TRADE_WINDOW_END_PT}:00 PT")
    log.info(f"Max alerts/day: {config.MAX_ALERTS_PER_DAY}")
    broker_name = ("Interactive Brokers" if config.USE_IB else
                   "Schwab paperMoney" if config.USE_SCHWAB else
                   "Alpaca Paper Trading" if config.USE_ALPACA else "Tastytrade")
    log.info(f"Broker:    {broker_name}")
    log.info("=" * 60)

    # ── Connect to broker ─────────────────────────────────
    pool = None  # IB connection pool (None if not using IB)

    if config.USE_IB:
        from broker.ib_client import IBClient
        from broker.ib_pool import IBConnectionPool

        # Create connection pool: 1 exit + 2 scanner connections
        num_scanner_conns = max(1, min(4, (len(config.TICKERS) + 8) // 9))
        log.info(f"Creating IB connection pool: 1 exit + {num_scanner_conns} scanner connections")
        pool = IBConnectionPool(num_scanner_connections=num_scanner_conns)
        pool.start_all()  # Each connection: connect + event loop on same thread

        # Exit manager gets a dedicated IBClient on the exit connection
        client = IBClient(pool.exit_conn, pool.contract_cache, pool.cache_lock)
        log.info(f"IB connection pool active: {len(pool.all_connections)} connections")

    elif config.USE_SCHWAB:
        from broker.schwab_client import SchwabClient
        client = SchwabClient()
        client.connect()
    elif config.USE_ALPACA:
        from broker.alpaca_client import AlpacaClient
        client = AlpacaClient()
        client.connect()
    else:
        from broker.tastytrade_client import TastytradeClient
        client = TastytradeClient()
        client.connect()

    # ── Update bot state in DB ────────────────────────────
    try:
        from db.writer import update_bot_state
        update_bot_state("running", account=config.IB_ACCOUNT, pid=os.getpid(),
                         total_tickers=len(config.TICKERS))
    except Exception as e:
        pass

    # ── Start exit monitor (shared, thread-safe) ──────────
    exit_manager = ExitManager(client)

    # ── Startup reconciliation: run directly on main thread (not via queue) ─
    # NOTE: This runs BEFORE the main loop, so we call IB directly instead
    # of going through the worker queue (which nobody is processing yet).
    try:
        from strategy.reconciliation import startup_reconciliation_direct
        startup_reconciliation_direct(client, exit_manager)
    except Exception as e:
        log.warning(f"Startup reconciliation failed: {e}")
        try:
            from strategy.error_handler import handle_error
            handle_error("bot", "startup_reconciliation", e, critical=True)
        except Exception:
            pass

    # ── Cleanup orphaned IB orders (not matched to any DB trade) ──
    try:
        cancelled = client.cleanup_orphaned_orders()
        if cancelled:
            add_system_log("bot", "info", f"Startup: cancelled {cancelled} orphaned IB order(s)")
    except Exception as e:
        log.warning(f"Orphaned order cleanup failed: {e}")

    exit_manager.start()

    # ── Scanners NOT auto-started — user must click "Start Scans" ──
    scanners = []
    log.info(f"Bot ready with {len(config.TICKERS)} tickers. Scans NOT started — click 'Start Scans' in dashboard.")

    # ── Start webhook server in background thread ──────────
    app = create_app(client, exit_manager)
    log.info(f"Webhook server on port {config.PORT} (manual override)")
    log.info(f"Health check: http://localhost:{config.PORT}/status")
    log.info("Bot is running. All scanners active during trade window.")
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=config.PORT, use_reloader=False),
        daemon=True, name="flask-webhook"
    )
    flask_thread.start()

    # ── Clean up any stale control files from old file-based system ──
    for f in [".bot_stop", ".pause_scans", ".resume_scans", ".scans_active"]:
        path = os.path.join(os.path.dirname(__file__), f)
        if os.path.exists(path):
            os.remove(path)

    # ── Mark IB connected in DB ─────────────────────────
    try:
        from db.writer import set_ib_connected, set_bot_error, add_system_log
        set_ib_connected(True)
        set_bot_error(None)  # clear any previous error
        add_system_log("bot", "info", "Bot started, IB connected", {"pid": os.getpid(), "account": config.IB_ACCOUNT})
    except Exception as e:
        pass

    # ── Reset scan state on startup — user must explicitly start scans ──
    try:
        from db.writer import set_scans_active, set_stop_requested
        set_scans_active(False)
        set_stop_requested(False)
        log.info("Scans not active — user must click 'Start Scans' in dashboard.")
    except Exception as e:
        pass

    # ── Main loop: read state from DB, process IB orders ──
    import time
    _last_state_check = 0
    _last_heartbeat = 0
    STATE_CHECK_INTERVAL = 2  # check DB every 2 seconds
    HEARTBEAT_INTERVAL = 30   # heartbeat every 30 seconds

    log.info("Main thread: processing IB order queue (state managed via DB)...")
    while True:
        try:
            now = time.time()

            # ── Bot main heartbeat ────────────────────
            if now - _last_heartbeat >= HEARTBEAT_INTERVAL:
                _last_heartbeat = now
                try:
                    from db.writer import update_thread_status
                    scan_status = f"{len(scanners)} scanners" if scanners else "scans off"
                    update_thread_status("bot-main", None, "running",
                                         f"Main loop active ({scan_status})")
                except Exception:
                    pass

            # ── Periodic state check from DB ──────────
            if now - _last_state_check >= STATE_CHECK_INTERVAL:
                _last_state_check = now
                try:
                    from db.writer import get_bot_state
                    state = get_bot_state()
                    if state:
                        # Stop requested?
                        if state.get("stop_requested"):
                            log.info("Stop requested via DB — shutting down...")
                            from db.writer import set_stop_requested
                            set_stop_requested(False)
                            break

                        # Manual reconciliation requested?
                        if state.get("last_error") == "RECONCILE_REQUESTED":
                            log.info("Manual reconciliation requested via dashboard...")
                            try:
                                from db.writer import set_bot_error
                                set_bot_error(None)  # Clear the signal
                                from strategy.reconciliation import periodic_reconciliation
                                periodic_reconciliation(client, exit_manager)
                                exit_manager.invalidate_cache()
                                add_system_log("reconciliation", "info", "Manual reconciliation completed")
                            except Exception as e:
                                log.error(f"Manual reconciliation failed: {e}")
                                add_system_log("reconciliation", "error", f"Manual reconciliation failed: {e}")

                        # Scan state changed?
                        db_scans = state.get("scans_active", False)
                        currently_scanning = len(scanners) > 0

                        if db_scans and not currently_scanning:
                            # Start scanners — each gets its own IBClient from the pool
                            log.info("DB: scans_active=True — starting scanners...")
                            scanners.clear()
                            for i, ticker in enumerate(config.TICKERS):
                                if pool:
                                    # Pool mode: each scanner gets its own IBClient
                                    # backed by a scanner connection from the pool
                                    scanner_conn = pool.get_scanner_connection(ticker)
                                    scanner_client = IBClient(scanner_conn,
                                                              pool.contract_cache,
                                                              pool.cache_lock)
                                else:
                                    # Non-IB broker: shared client
                                    scanner_client = client
                                scanner = Scanner(scanner_client, exit_manager,
                                                  ticker=ticker, scan_offset=i * 2)
                                scanner.start()
                                scanners.append(scanner)
                            log.info(f"Started {len(scanners)} scanner threads"
                                     f"{f' on {len(pool.scanner_conns)} IB connections' if pool else ''}")
                            try:
                                add_system_log("scanner", "info", f"Scanners started ({len(scanners)} tickers)")
                            except Exception as e:
                                pass

                        elif not db_scans and currently_scanning:
                            # Stop scanners
                            log.info("DB: scans_active=False — stopping scanners...")
                            for s in scanners:
                                s.stop()
                            try:
                                from db.writer import update_thread_status
                                for ticker in config.TICKERS:
                                    update_thread_status(f"scanner-{ticker}", ticker, "stopped", "Scans stopped by user")
                                add_system_log("scanner", "info", "Scanners stopped by user")
                            except Exception as e:
                                pass
                            scanners.clear()
                            log.info("All scanners stopped.")
                except Exception as e:
                    log.debug(f"State check error: {e}")

            # ── Process IB orders ─────────────────────
            if pool:
                # Pool mode: event loops run on their own threads
                # Main thread just sleeps briefly
                time.sleep(0.5)
            elif hasattr(client, 'process_orders'):
                client.process_orders()
            else:
                time.sleep(0.5)

        except KeyboardInterrupt:
            log.info("Shutting down (KeyboardInterrupt)...")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}")
            try:
                set_bot_error(str(e))
                add_system_log("bot", "error", f"Main loop error: {e}")
            except Exception as e:
                pass
            time.sleep(1)

    _shutdown_cleanup(pool)


def _shutdown_cleanup(pool=None):
    """Mark bot and all threads as stopped in DB. Stop IB pool if active."""
    # Stop IB connection pool
    if pool:
        try:
            pool.stop_all()
            log.info("IB connection pool stopped")
        except Exception as e:
            log.debug(f"Pool shutdown: {e}")

    # Update DB
    try:
        from db.writer import update_bot_state, update_thread_status, set_ib_connected, set_stop_requested, add_system_log
        update_bot_state("stopped")
        set_ib_connected(False)
        set_stop_requested(False)
        for ticker in config.TICKERS:
            update_thread_status(f"scanner-{ticker}", ticker, "stopped", "Bot shut down")
        update_thread_status("exit_manager", None, "stopped", "Bot shut down")
        update_thread_status("bot-main", None, "stopped", "Bot shut down")
        add_system_log("bot", "info", "Bot shut down gracefully")
        log.info("DB updated: bot and threads marked as stopped")
    except Exception as e:
        log.debug(f"Shutdown DB cleanup: {e}")


if __name__ == "__main__":
    main()
