"""
Import historical CSV trade logs into PostgreSQL.
Run: python scripts/import_csv_trades.py
Reads all CSV files from logs/ directory and imports closed trades.
"""
import os
import sys
import csv
import json
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from db.connection import get_engine, db_available
from db.models import Trade
from sqlalchemy.orm import Session


def import_csv(filepath: str, engine, account: str = "unknown"):
    """Import a single CSV file into the trades table."""
    imported = 0
    skipped = 0

    # Extract account from filename if possible (format: ACCOUNT_TIMESTAMP.csv)
    basename = os.path.basename(filepath)
    if "_" in basename:
        account = basename.split("_")[0]

    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        with Session(engine) as session:
            for row in reader:
                # Skip if already imported (check by entry_time + symbol)
                entry_time_str = row.get("entry_time", "")
                symbol = row.get("symbol", "")
                if not entry_time_str or not symbol:
                    skipped += 1
                    continue

                existing = session.query(Trade).filter(
                    Trade.symbol == symbol,
                    Trade.entry_time == entry_time_str,
                ).first()
                if existing:
                    skipped += 1
                    continue

                # Build enrichment JSONB from CSV columns
                entry_enrichment = {}
                exit_enrichment = {}
                for prefix, target in [("entry_", entry_enrichment), ("exit_", exit_enrichment)]:
                    for key in ["delta", "gamma", "theta", "vega", "vwap", "vix",
                                "rsi_14", "sma_7", "sma_10", "sma_20", "sma_50",
                                "ema_7", "ema_10", "ema_20", "ema_50",
                                "macd_line", "macd_signal", "macd_histogram",
                                "stock_price"]:
                        csv_key = f"{prefix}{key}"
                        val = row.get(csv_key)
                        if val and val != "None" and val != "":
                            try:
                                target[key] = float(val)
                            except ValueError:
                                target[key] = val

                entry_price = float(row.get("entry_price", 0) or 0)
                exit_price = float(row.get("exit_price", 0) or 0)
                contracts = int(row.get("contracts", 2) or 2)

                pnl_pct = float(row.get("pnl_pct", 0) or 0)
                pnl_usd = float(row.get("pnl_usd", 0) or 0)

                trade = Trade(
                    account=account,
                    ticker=row.get("ticker", "UNK"),
                    symbol=symbol,
                    direction=row.get("direction", "LONG"),
                    contracts_entered=contracts,
                    contracts_open=0,
                    contracts_closed=contracts,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    current_price=exit_price,
                    pnl_pct=pnl_pct,
                    pnl_usd=pnl_usd,
                    profit_target=entry_price * 2,
                    stop_loss_level=entry_price * 0.4,
                    entry_time=entry_time_str,
                    exit_time=row.get("exit_time"),
                    status="closed",
                    exit_reason=row.get("reason"),
                    exit_result=row.get("result"),
                    entry_enrichment=entry_enrichment,
                    exit_enrichment=exit_enrichment,
                )
                session.add(trade)
                imported += 1

            session.commit()

    return imported, skipped


if __name__ == "__main__":
    print("=" * 50)
    print("ICT Trading Bot — CSV Trade Import")
    print("=" * 50)

    if not db_available():
        print("\nERROR: Cannot connect to database. Set DATABASE_URL.")
        sys.exit(1)

    engine = get_engine()
    logs_dir = os.path.join(os.path.dirname(__file__), "..", "logs")

    if not os.path.exists(logs_dir):
        print(f"\nNo logs/ directory found at {logs_dir}")
        sys.exit(0)

    csv_files = sorted([f for f in os.listdir(logs_dir) if f.endswith(".csv")])
    if not csv_files:
        print("\nNo CSV files found in logs/")
        sys.exit(0)

    total_imported = 0
    total_skipped = 0

    for fname in csv_files:
        filepath = os.path.join(logs_dir, fname)
        imported, skipped = import_csv(filepath, engine)
        total_imported += imported
        total_skipped += skipped
        print(f"  {fname}: {imported} imported, {skipped} skipped")

    print(f"\nTotal: {total_imported} trades imported, {total_skipped} skipped")
