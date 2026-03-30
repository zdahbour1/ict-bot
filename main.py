"""
ICT QQQ Options Bot — Entry Point
Run this file to start the bot:  python main.py
"""
import logging
import config
from broker.tastytrade_client import TastytradeClient
from broker.schwab_client import SchwabClient
from broker.alpaca_client import AlpacaClient
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
    log.info("ICT QQQ Options Bot starting...")
    log.info(f"Mode:      {'DRY RUN (no real orders)' if config.DRY_RUN else 'LIVE TRADING'}")
    log.info(f"Ticker:    {config.TICKER}")
    log.info(f"Contracts: {config.CONTRACTS}")
    log.info(f"Option TP: {config.PROFIT_TARGET:.0%}   SL: {config.STOP_LOSS:.0%}")
    log.info(f"Window:    {config.TRADE_WINDOW_START_PT}:00-{config.TRADE_WINDOW_END_PT}:00 PT")
    log.info(f"Strategy:  Raid + Displacement + iFVG/OB (full ICT)")
    log.info(f"Max alerts/day: {config.MAX_ALERTS_PER_DAY}")
    broker_name = "Schwab paperMoney" if config.USE_SCHWAB else ("Alpaca Paper Trading" if config.USE_ALPACA else "Tastytrade")
    log.info(f"Broker:    {broker_name}")
    log.info("=" * 60)

    # ── Connect to broker ─────────────────────────────────
    if config.USE_SCHWAB:
        client = SchwabClient()
    elif config.USE_ALPACA:
        client = AlpacaClient()
    else:
        client = TastytradeClient()
    client.connect()

    # ── Start exit monitor (background thread) ────────────
    exit_manager = ExitManager(client)
    exit_manager.start()

    # ── Start ICT strategy scanner (background thread) ────
    scanner = Scanner(client, exit_manager)
    scanner.start()

    # ── Start webhook server (also accepts manual signals) ─
    app = create_app(client, exit_manager)
    log.info(f"Webhook server on port {config.PORT} (manual override)")
    log.info(f"Health check: http://localhost:{config.PORT}/status")
    log.info("Bot is running. ICT scanner active during trade window.")
    app.run(host="0.0.0.0", port=config.PORT)


if __name__ == "__main__":
    main()
