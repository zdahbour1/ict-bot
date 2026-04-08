"""
Seed Script — Migrate tickers.txt and .env/config.py values into PostgreSQL.
Run once after initial database setup: python scripts/seed_from_files.py

Idempotent: uses ON CONFLICT DO NOTHING for tickers, upserts for settings.
"""
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from db.connection import get_engine, db_available
from db.models import Base, Ticker, Setting

def seed_tickers():
    """Import tickers from tickers.txt into the tickers table."""
    from sqlalchemy.orm import Session
    engine = get_engine()
    if not engine:
        print("ERROR: DATABASE_URL not set")
        return

    tickers_file = os.path.join(os.path.dirname(__file__), "..", "tickers.txt")
    if not os.path.exists(tickers_file):
        print("WARNING: tickers.txt not found, skipping ticker seed")
        return

    with open(tickers_file, "r") as f:
        symbols = [line.strip().upper() for line in f
                   if line.strip() and not line.strip().startswith("#")]

    # Well-known ticker names
    names = {
        "QQQ": "Invesco QQQ Trust", "SPY": "SPDR S&P 500 ETF",
        "AAPL": "Apple Inc.", "NVDA": "NVIDIA Corporation",
        "TSLA": "Tesla Inc.", "IWM": "iShares Russell 2000 ETF",
        "AMD": "Advanced Micro Devices", "AMZN": "Amazon.com Inc.",
        "META": "Meta Platforms Inc.", "MSFT": "Microsoft Corporation",
        "GOOGL": "Alphabet Inc.", "NFLX": "Netflix Inc.",
        "PLTR": "Palantir Technologies", "SLV": "iShares Silver Trust",
        "XLF": "Financial Select Sector SPDR", "MU": "Micron Technology",
        "INTC": "Intel Corporation", "TQQQ": "ProShares UltraPro QQQ",
        "SSO": "ProShares Ultra S&P500",
    }

    with Session(engine) as session:
        added = 0
        for sym in symbols:
            existing = session.query(Ticker).filter(Ticker.symbol == sym).first()
            if not existing:
                session.add(Ticker(symbol=sym, name=names.get(sym), contracts=2))
                added += 1
        session.commit()
        total = session.query(Ticker).count()
        print(f"Tickers: {added} added, {total} total in database")


def seed_settings():
    """Import settings from .env and config defaults into the settings table."""
    from sqlalchemy.orm import Session
    engine = get_engine()
    if not engine:
        print("ERROR: DATABASE_URL not set")
        return

    # All settings with (category, key, default_value, data_type, description, is_secret)
    settings_data = [
        # Broker: IB
        ("broker", "USE_IB", os.getenv("USE_IB", "true"), "bool", "Use Interactive Brokers as the broker", False),
        ("broker", "IB_HOST", os.getenv("IB_HOST", "127.0.0.1"), "string", "IB Gateway/TWS host address", False),
        ("broker", "IB_PORT", os.getenv("IB_PORT", "7497"), "int", "IB port (7497=TWS paper, 4002=Gateway paper)", False),
        ("broker", "IB_CLIENT_ID", os.getenv("IB_CLIENT_ID", "1"), "int", "IB API client ID", False),
        ("broker", "IB_ACCOUNT", os.getenv("IB_ACCOUNT", ""), "string", "IB account number", False),
        ("broker", "DRY_RUN", os.getenv("DRY_RUN", "false"), "bool", "Log trades without placing orders", False),
        ("broker", "PAPER_TRADING", os.getenv("PAPER_TRADING", "true"), "bool", "Paper trading mode", False),
        # Broker: Tastytrade
        ("broker", "USE_TASTYTRADE", "false", "bool", "Use Tastytrade as the broker", False),
        ("broker", "TASTYTRADE_USERNAME", os.getenv("TASTYTRADE_USERNAME", ""), "string", "Tastytrade login email", True),
        ("broker", "TASTYTRADE_PASSWORD", os.getenv("TASTYTRADE_PASSWORD", ""), "string", "Tastytrade password", True),
        ("broker", "TASTYTRADE_ACCOUNT", os.getenv("TASTYTRADE_ACCOUNT", ""), "string", "Tastytrade account number", False),
        # Broker: Schwab
        ("broker", "USE_SCHWAB", "false", "bool", "Use Schwab as the broker", False),
        ("broker", "SCHWAB_APP_KEY", os.getenv("SCHWAB_APP_KEY", ""), "string", "Schwab OAuth app key", True),
        ("broker", "SCHWAB_APP_SECRET", os.getenv("SCHWAB_APP_SECRET", ""), "string", "Schwab OAuth app secret", True),
        ("broker", "SCHWAB_CALLBACK_URL", os.getenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1"), "string", "Schwab OAuth callback URL", False),
        ("broker", "SCHWAB_PAPER_ACCOUNT", os.getenv("SCHWAB_PAPER_ACCOUNT", ""), "string", "Schwab paper account", False),
        # Broker: Alpaca
        ("broker", "USE_ALPACA", "false", "bool", "Use Alpaca as the broker", False),
        ("broker", "ALPACA_API_KEY", os.getenv("ALPACA_API_KEY", ""), "string", "Alpaca API key", True),
        ("broker", "ALPACA_SECRET_KEY", os.getenv("ALPACA_SECRET_KEY", ""), "string", "Alpaca secret key", True),
        # Strategy
        ("strategy", "RAID_THRESHOLD", "0.05", "float", "Min $ penetration below level for raid", False),
        ("strategy", "BODY_MULT", "1.2", "float", "Displacement candle body multiplier", False),
        ("strategy", "DISPLACEMENT_LOOKBACK", "20", "int", "Bars back for median body calculation", False),
        ("strategy", "N_CONFIRM_BARS", "2", "int", "Bars after raid to confirm displacement", False),
        ("strategy", "FVG_MIN_SIZE", "0.10", "float", "Minimum FVG size in dollars", False),
        ("strategy", "OB_MAX_CANDLES", "3", "int", "Max bearish candles for order block", False),
        ("strategy", "SL_BUFFER", "0.05", "float", "Buffer below raid low for stop loss ($)", False),
        ("strategy", "TP_LOOKBACK", "40", "int", "Bars back to find swing high TP", False),
        ("strategy", "MAX_ALERTS_PER_DAY", "999", "int", "Max signal alerts per day", False),
        ("strategy", "EMA_PERIOD_1H", "20", "int", "1H EMA period for trend filter", False),
        ("strategy", "NEWS_BUFFER_MIN", "30", "int", "Minutes around news to block trades", False),
        # Exit rules
        ("exit_rules", "PROFIT_TARGET", "1.00", "float", "Exit when option premium up this % (1.00=100%)", False),
        ("exit_rules", "STOP_LOSS", "0.60", "float", "Exit when option premium down this % (0.60=60%)", False),
        ("exit_rules", "COOLDOWN_MINUTES", os.getenv("COOLDOWN_MINUTES", "15"), "int", "Min wait after exit before re-entry per ticker", False),
        ("exit_rules", "USE_BRACKET_ORDERS", "true", "bool", "Place OCO bracket orders on IB", False),
        ("exit_rules", "ROLL_ENABLED", "true", "bool", "Enable option rolling at threshold", False),
        ("exit_rules", "ROLL_THRESHOLD", "0.70", "float", "Roll at this fraction of TP (0.70=70%)", False),
        ("exit_rules", "TP_TO_TRAIL", "true", "bool", "At TP move SL to TP level instead of exit", False),
        ("exit_rules", "RECONCILIATION_INTERVAL_MIN", "5", "int", "Minutes between IB position reconciliation", False),
        # Trade window
        ("trade_window", "TRADE_WINDOW_START_PT", "6", "int", "Trade window start hour (PT)", False),
        ("trade_window", "TRADE_WINDOW_START_MIN", "30", "int", "Trade window start minute (PT)", False),
        ("trade_window", "TRADE_WINDOW_END_PT", "13", "int", "Trade window end hour (PT)", False),
        # General
        ("general", "CONTRACTS", "2", "int", "Default contracts per trade", False),
        ("general", "MONITOR_INTERVAL", "5", "int", "Seconds between exit monitor checks", False),
        # Email
        ("email", "EMAIL_TO", os.getenv("EMAIL_TO", ""), "string", "Email for trade alerts", False),
        ("email", "EMAIL_FROM", os.getenv("EMAIL_FROM", ""), "string", "Sender email (Gmail)", False),
        ("email", "EMAIL_APP_PASSWORD", os.getenv("EMAIL_APP_PASSWORD", ""), "string", "Gmail app password", True),
        # Webhook
        ("webhook", "PORT", "5000", "int", "Webhook server port", False),
        ("webhook", "WEBHOOK_SECRET", os.getenv("WEBHOOK_SECRET", "ict-secret-token"), "string", "Webhook auth token", True),
    ]

    with Session(engine) as session:
        added = 0
        updated = 0
        for cat, key, value, dtype, desc, secret in settings_data:
            existing = session.query(Setting).filter(Setting.key == key).first()
            if not existing:
                session.add(Setting(
                    category=cat, key=key, value=value or "",
                    data_type=dtype, description=desc, is_secret=secret
                ))
                added += 1
            else:
                # Update description if it changed
                if existing.description != desc:
                    existing.description = desc
                    updated += 1
        session.commit()
        total = session.query(Setting).count()
        print(f"Settings: {added} added, {updated} updated, {total} total in database")


def seed_bot_state():
    """Ensure bot_state singleton exists."""
    from sqlalchemy.orm import Session
    from db.models import BotState
    engine = get_engine()
    if not engine:
        return
    with Session(engine) as session:
        existing = session.query(BotState).filter(BotState.id == 1).first()
        if not existing:
            session.add(BotState(id=1, status="stopped"))
            session.commit()
            print("Bot state: initialized (stopped)")
        else:
            print(f"Bot state: already exists ({existing.status})")


if __name__ == "__main__":
    print("=" * 50)
    print("ICT Trading Bot — Database Seed Script")
    print("=" * 50)

    if not db_available():
        print("\nERROR: Cannot connect to database.")
        print("Set DATABASE_URL environment variable, e.g.:")
        print("  export DATABASE_URL=postgresql://ict_bot:ict_bot_dev@localhost:5432/ict_bot")
        sys.exit(1)

    print()
    seed_tickers()
    seed_settings()
    seed_bot_state()
    print()
    print("Seed complete!")
