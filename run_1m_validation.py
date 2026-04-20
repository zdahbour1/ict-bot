"""
1-minute-interval validation of the top run per strategy.

For each of ICT (INTC), ORB (INTC), VWAP (NFLX), re-run the winning
config at 1m interval. yfinance caps 1m data to ~7 days rolling, so
the date range is intentionally short.

This is an out-of-sample sanity check: if the edge is real, the
strategy should still be profitable (directionally) at finer
resolution. If it flips sign or collapses, the 5m result may be a
bar-alignment artifact.

Run:  python run_1m_validation.py
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from backtest_engine.sweep import SweepResult, run_sweep


TOP_RUNS = [
    # (strategy, ticker, stop_loss, profit_target, 5m_pnl)
    ("ict",         "INTC", 0.6, 1.0,  6013.30),
    ("orb",         "INTC", 0.8, 1.0,  3775.64),
    ("vwap_revert", "NFLX", 0.6, 1.0,  2846.08),
]

# yfinance 1m data: rolling ~7 days. Use 5 days to be safe.
END_DATE   = date.today()
START_DATE = END_DATE - timedelta(days=5)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s - %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("1m-validation")

    all_results: list[tuple[str, str, SweepResult, float]] = []

    for strategy, ticker, sl, pt, five_m_pnl in TOP_RUNS:
        log.info("== %s on %s (1m, SL=%.1f PT=%.1f) ==", strategy, ticker, sl, pt)
        try:
            results = run_sweep(
                strategy_name=strategy,
                tickers=[ticker],
                start_date=START_DATE,
                end_date=END_DATE,
                base_config={
                    "base_interval":   "1m",
                    "option_dte_days": 7,
                    "option_vol":      0.20,
                    "stop_loss":       sl,
                    "profit_target":   pt,
                },
                grid={},
                name_prefix=f"{strategy}-{ticker}-1m-valid",
                per_ticker=False,
            )
        except Exception as e:
            log.error("FAIL %s/%s: %s", strategy, ticker, e)
            continue
        if not results:
            log.warning("no result rows for %s/%s", strategy, ticker)
            continue
        r = results[0]
        all_results.append((strategy, ticker, r, five_m_pnl))

    # Comparison
    print("\n" + "=" * 88)
    print("  1m VALIDATION OF TOP RUN PER STRATEGY")
    print("  (5-day window; yfinance caps 1m at ~7 days rolling)")
    print("=" * 88)
    print(f"  {'Strategy':<14} {'Ticker':<6} {'Trades':>7} "
          f"{'Win%':>7} {'PnL (1m)':>12} {'PnL (5m)':>12} {'Verdict':<18}")
    print("-" * 88)
    for strategy, ticker, r, five_m_pnl in all_results:
        sign_match = (r.total_pnl > 0) == (five_m_pnl > 0)
        verdict = "confirms edge" if sign_match and r.total_pnl > 0 else (
            "still losing" if not sign_match and r.total_pnl < 0 else
            "SIGN FLIP (investigate)" if not sign_match else
            "still profitable"
        )
        print(f"  {strategy:<14} {ticker:<6} {r.total_trades:>7} "
              f"{r.win_rate:>6.1f}% ${r.total_pnl:>10.2f} ${five_m_pnl:>10.2f}  {verdict}")
    print("=" * 88)
    print()

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
