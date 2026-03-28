"""
Main entry point — run backtest.

Usage:
  python -m bot.main                        # uses yfinance (requires internet)
  python -m bot.main --csv path/to/data.csv # uses your own CSV file
  python -m bot.main --synthetic            # uses synthetic demo data (no internet needed)
"""
import sys
import argparse
from pathlib import Path
from loguru import logger

from bot import config
from bot.backtest.runner import run_backtest


def main():
    parser = argparse.ArgumentParser(description="ICT Scalping Bot — Backtest Mode")
    parser.add_argument("--synthetic", action="store_true",
                        help="Use synthetic demo data (no API key or internet required)")
    parser.add_argument("--csv", type=str, default=None,
                        help="Path to a CSV file with 1m OHLCV bars")
    parser.add_argument("--days", type=int, default=10,
                        help="Days of synthetic data to generate (default: 10)")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | {level} | {message}")

    backtest_dir = Path("backtest_results")
    backtest_dir.mkdir(exist_ok=True)
    logger.add(backtest_dir / "run.log", level="DEBUG", rotation="10 MB")

    logger.info(f"ICT Scalping Bot v1 — Backtesting {config.SYMBOL}")
    logger.info(f"Trade window: {config.TRADE_WINDOW_START_PT}–{config.TRADE_WINDOW_END_PT} PT")

    # ── Load data ─────────────────────────────────────────────────────────────
    df_1m = None

    if args.csv:
        from bot.data.provider_synthetic import load_from_csv
        df_1m = load_from_csv(args.csv)

    elif args.synthetic:
        from bot.data.provider_synthetic import generate_synthetic_qqq
        df_1m = generate_synthetic_qqq(days=args.days)

    else:
        # Try yfinance first, fall back to synthetic on failure
        try:
            from bot.data.provider_yfinance import fetch_1m_bars
            df_1m = fetch_1m_bars(config.SYMBOL, lookback_hours=config.LOOKBACK_HOURS)
        except Exception as e:
            logger.warning(f"yfinance failed ({e}). Falling back to SYNTHETIC data.")
            logger.warning("For real backtesting, use: python -m bot.main --csv your_data.csv")
            from bot.data.provider_synthetic import generate_synthetic_qqq
            df_1m = generate_synthetic_qqq(days=args.days)

    # ── Run backtest ──────────────────────────────────────────────────────────
    metrics = run_backtest(df_1m, dry_run_alerts=True)
    return metrics


if __name__ == "__main__":
    main()
