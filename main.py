"""
ICT QQQ Options Bot — Entry Point
Run this file to start the bot:  python main.py
"""
import logging
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

    # ── Start exit monitor (shared, thread-safe) ──────────
    exit_manager = ExitManager(client)
    exit_manager.start()

    # ── Start one scanner per ticker (parallel threads) ───
    scanners = []
    for i, ticker in enumerate(config.TICKERS):
        scanner = Scanner(client, exit_manager, ticker=ticker, scan_offset=i * 5)
        scanner.start()
        scanners.append(scanner)
    log.info(f"Launched {len(scanners)} scanner threads: {', '.join(config.TICKERS)}")

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

    # ── Main loop: process IB orders on the main thread ───
    # IB's event loop must run on the thread that called connect()
    import time
    log.info("Main thread: processing IB order queue...")
    while True:
        try:
            if hasattr(client, 'process_orders'):
                client.process_orders()
            else:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Shutting down...")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}")
            time.sleep(1)


if __name__ == "__main__":
    main()
