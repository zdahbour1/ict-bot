"""ENH-066 — backtest validation: does delta hedging help?

Runs the same variant configuration with and without ``delta_hedge``
across the same tickers + window, prints a side-by-side comparison.
The hedged version uses the variant's ``hedge_delta_band_shares``;
the unhedged version sets ``delta_hedge=False`` so the engine skips
the rebalance loop entirely.

This is the validator for the multi-leg completion plan: if hedging
materially changes drawdown / win-rate / Sharpe, the live Phase 3
EnvelopeExitMonitor work is justified. If not, Class A brackets
alone are good enough.

Usage:
    python scripts/backtest_hedge_validation.py
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import replace
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest_engine.dn_variants_engine import run_variant_backtest
from strategy.delta_neutral_variants import (
    V5_HEDGED, V5B_SWEEP_WINNER, ZDN_WEEKLY,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)-6s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("hedge-validation")
log.setLevel(logging.INFO)


# Same 5-ticker partition the user just configured (sans the disabled
# strategies — picking the one ticker per group most likely to yield
# trades within 58 days of yfinance data).
TICKERS = ["AAPL", "MSFT", "NVDA", "META", "AMZN"]


def _summary(label: str, trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"label": label, "n": 0, "win_rate": 0.0,
                "total_pnl": 0.0, "avg_pnl": 0.0,
                "max_win": 0.0, "max_loss": 0.0,
                "opt_pnl": 0.0, "hedge_pnl": 0.0,
                "rebalances": 0}
    pnls = [float(t.get("pnl_usd") or 0.0) for t in trades]
    opt_pnls = [float(t.get("opt_pnl_usd") or t.get("pnl_usd") or 0.0)
                for t in trades]
    hedge_pnls = [float(t.get("hedge_pnl_usd") or 0.0) for t in trades]
    rebs = sum(int(t.get("hedge_rebalance_count") or 0) for t in trades)
    wins = sum(1 for p in pnls if p > 0)
    return {
        "label": label, "n": n,
        "win_rate": 100.0 * wins / n,
        "total_pnl": sum(pnls),
        "avg_pnl": sum(pnls) / n,
        "max_win": max(pnls),
        "max_loss": min(pnls),
        "opt_pnl": sum(opt_pnls),
        "hedge_pnl": sum(hedge_pnls),
        "rebalances": rebs,
    }


def _print_summary(s: dict) -> None:
    print(f"  {s['label']:<25} "
          f"trades={s['n']:>3}  "
          f"win%={s['win_rate']:>5.1f}  "
          f"total=${s['total_pnl']:>+8.0f}  "
          f"avg=${s['avg_pnl']:>+6.0f}  "
          f"max_win=${s['max_win']:>+6.0f}  "
          f"max_loss=${s['max_loss']:>+6.0f}  "
          f"opt=${s['opt_pnl']:>+8.0f}  "
          f"hedge=${s['hedge_pnl']:>+8.0f}  "
          f"rebals={s['rebalances']:>4}")


def run_pair(base_variant, label: str, start_d: date, end_d: date):
    """Run hedged + unhedged copies of base_variant across TICKERS."""
    hedged = base_variant
    unhedged = replace(base_variant, delta_hedge=False,
                        hedge_delta_band_shares=0)

    print(f"\n=== {label} ({start_d} -> {end_d}) ===")
    h_trades: list[dict] = []
    u_trades: list[dict] = []
    for tk in TICKERS:
        try:
            h = run_variant_backtest(hedged, tk, start_d, end_d)
            u = run_variant_backtest(unhedged, tk, start_d, end_d)
        except Exception as e:
            log.warning(f"{label}/{tk} failed: {e}")
            continue
        h_trades.extend(h.trades)
        u_trades.extend(u.trades)

    print(f"{'':<27}{'trades':>8}{'win%':>7}"
          f"{'total':>11}{'avg':>9}{'max_win':>10}{'max_loss':>11}"
          f"{'opt':>11}{'hedge':>11}{'rebals':>8}")
    _print_summary(_summary(f"{label} HEDGED",   h_trades))
    _print_summary(_summary(f"{label} UNHEDGED", u_trades))


def main():
    end_d = date.today()
    start_d = end_d - timedelta(days=58)

    for variant, name in [
        (V5_HEDGED, "V5_HEDGED 45-DTE"),
        (V5B_SWEEP_WINNER, "V5B_SWEEP_WINNER"),
        (ZDN_WEEKLY, "ZDN_WEEKLY butterfly"),
    ]:
        try:
            run_pair(variant, name, start_d, end_d)
        except Exception as e:
            log.error(f"{name} pair failed: {e}", exc_info=True)


if __name__ == "__main__":
    main()
