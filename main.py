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
    log.info(f"Tickers:   {', '.join(config.TICKERS)}")
    for t in config.TICKERS:
        contracts = config.CONTRACTS_PER_TICKER.get(t, config.CONTRACTS)
        log.info(f"  {t}: {contracts} contracts")
    log.info(f"Option TP: {config.PROFIT_TARGET:.0%}   SL: {config.STOP_LOSS:.0%}")
    log.info(f"Window:    {config.TRADE_WINDOW_START_PT}:00-{config.TRADE_WINDOW_END_PT}:00 PT")
    log.info(f"Strategy:  Raid + Displacement + iFVG/OB (full ICT)")
    log.info(f"Max alerts/day: {config.MAX_ALERTS_PER_DAY}")
    broker_name = ("Interactive Brokers" if config.USE_IB else
                   "Schwab paperMoney" if config.USE_SCHWAB else
                   "Alpaca Paper Trading" if config.USE_ALPACA else "Tastytrade")
    log.info(f"Broker:    {broker_name}")
    log.info("=" * 60)

    # ── Connect to broker ─────────────────────────────────
    if config.USE_IB:
        from broker.ib_client import IBClient
        client = IBClient()
    elif config.USE_SCHWAB:
        from broker.schwab_client import SchwabClient
        client = SchwabClient()
    elif config.USE_ALPACA:
        from broker.alpaca_client import AlpacaClient
        client = AlpacaClient()
    else:
        from broker.tastytrade_client import TastytradeClient
        client = TastytradeClient()
    client.connect()

    # ── Update bot state in DB ────────────────────────────
    try:
        from db.writer import update_bot_state
        update_bot_state("running", account=config.IB_ACCOUNT, pid=os.getpid(),
                         total_tickers=len(config.TICKERS))
    except Exception:
        pass

    # ── Start exit monitor (shared, thread-safe) ──────────
    exit_manager = ExitManager(client)

    # ── Startup reconciliation: sync DB with IB positions ─
    try:
        from strategy.reconciliation import startup_reconciliation
        startup_reconciliation(client, exit_manager)
    except Exception as e:
        log.warning(f"Startup reconciliation failed: {e}")

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

    # ── Mark IB connected in DB ─────────────────────────
    try:
        from db.writer import set_ib_connected, set_bot_error, add_system_log
        set_ib_connected(True)
        set_bot_error(None)  # clear any previous error
        add_system_log("bot", "info", "Bot started, IB connected", {"pid": os.getpid(), "account": config.IB_ACCOUNT})
    except Exception:
        pass

    # ── Check DB for scan state (restore on restart) ──
    try:
        from db.writer import get_bot_state
        state = get_bot_state()
        if state and state.get("scans_active"):
            log.info("DB shows scans_active=True — starting scanners...")
            for i, ticker in enumerate(config.TICKERS):
                scanner = Scanner(client, exit_manager, ticker=ticker, scan_offset=i * 2)
                scanner.start()
                scanners.append(scanner)
            log.info(f"Started {len(scanners)} scanner threads: {', '.join(config.TICKERS)}")
            add_system_log("scanner", "info", f"Scanners started ({len(scanners)} tickers)")
        else:
            log.info("Scans not active — waiting for 'Start Scans' command.")
    except Exception as e:
        log.warning(f"Could not check scan state: {e}")

    # ── Main loop: read state from DB, process IB orders ──
    import time
    _last_state_check = 0
    STATE_CHECK_INTERVAL = 2  # check DB every 2 seconds

    log.info("Main thread: processing IB order queue (state managed via DB)...")
    while True:
        try:
            now = time.time()

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

                        # Scan state changed?
                        db_scans = state.get("scans_active", False)
                        currently_scanning = len(scanners) > 0

                        if db_scans and not currently_scanning:
                            # Start scanners
                            log.info("DB: scans_active=True — starting scanners...")
                            scanners.clear()
                            for i, ticker in enumerate(config.TICKERS):
                                scanner = Scanner(client, exit_manager, ticker=ticker, scan_offset=i * 2)
                                scanner.start()
                                scanners.append(scanner)
                            log.info(f"Started {len(scanners)} scanner threads")
                            try:
                                add_system_log("scanner", "info", f"Scanners started ({len(scanners)} tickers)")
                            except Exception:
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
                            except Exception:
                                pass
                            scanners.clear()
                            log.info("All scanners stopped.")
                except Exception as e:
                    log.debug(f"State check error: {e}")

            # ── Process IB orders ─────────────────────
            if hasattr(client, 'process_orders'):
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
            except Exception:
                pass
            time.sleep(1)

    _shutdown_cleanup()


def _shutdown_cleanup():
    """Mark bot and all threads as stopped in DB."""
    try:
        from db.writer import update_bot_state, update_thread_status, set_ib_connected, set_stop_requested, add_system_log
        update_bot_state("stopped")
        set_ib_connected(False)
        set_stop_requested(False)
        for ticker in config.TICKERS:
            update_thread_status(f"scanner-{ticker}", ticker, "stopped", "Bot shut down")
        add_system_log("bot", "info", "Bot shut down gracefully")
        log.info("DB updated: bot and threads marked as stopped")
    except Exception as e:
        log.debug(f"Shutdown DB cleanup: {e}")


if __name__ == "__main__":
    main()
