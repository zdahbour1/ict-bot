"""Parameter sweep across V1 and V5 DN variants.

Generates a Cartesian product of parameter variations, runs each
against a small universe, and reports the top-N combinations by
sharpe_ish + profit factor.

Usage:

    python scripts/sweep_dn_variants.py
    python scripts/sweep_dn_variants.py --variant v5 --tickers SPY,QQQ,COIN,MSTR
    python scripts/sweep_dn_variants.py --top 20

Outputs:
  - data/sweep_results_<date>.csv
  - docs/sweep_report_<date>.md
"""
from __future__ import annotations

import argparse
import csv
import itertools
import logging
import os
import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest_engine.data_provider import fetch_bars
from backtest_engine.dn_variants_engine import run_variant_backtest
from strategy.delta_neutral_variants import (
    V1_BASELINE, V5_HEDGED, TIERS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-6s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dn-sweep")


# V1 parameter grid — small, the baseline has few knobs that matter
V1_GRID = {
    "wing_width_dollars": [5.0, 10.0, 20.0],
    "profit_target_pct": [0.25, 0.50, 0.75],
    "target_dte":        [5, 7, 14],
}

# V5 parameter grid — the alpha-generator has more meaningful knobs
V5_GRID = {
    "short_delta":        [0.10, 0.16, 0.25],
    "long_delta":         [0.03, 0.05, 0.10],
    "ivr_min":            [0, 20, 30, 50],
    "profit_target_pct":  [0.25, 0.50, 0.75],
    "hard_exit_dte":      [14, 21, 30],
    "target_dte":         [30, 45],
}


def _expand(grid: dict) -> list[dict]:
    keys = list(grid.keys())
    combos = itertools.product(*[grid[k] for k in keys])
    out = []
    for vals in combos:
        d = dict(zip(keys, vals))
        # Skip invalid iron-condor geometry: long_delta must be strictly
        # less than short_delta (wings OTM of the short strikes).
        # Without this, the grid spends hours on combos that yield 0
        # trades because net credit <= 0.
        if "short_delta" in d and "long_delta" in d:
            if d["long_delta"] >= d["short_delta"]:
                continue
        out.append(d)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=["v1", "v5", "both"],
                    default="both")
    ap.add_argument("--tickers",
                    default="SPY,QQQ,COIN,MSTR,AMD,TSLA,NVDA,MSFT")
    ap.add_argument("--days", type=int, default=58)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    end_d = date.today()
    start_d = end_d - timedelta(days=args.days)
    tickers = args.tickers.split(",")
    log.info(f"Sweep window: {start_d} → {end_d}")
    log.info(f"Universe: {tickers}")

    # VIX once
    try:
        vix = fetch_bars("^VIX", interval="5m", start=start_d, end=end_d)
        vix3m = fetch_bars("^VIX3M", interval="5m", start=start_d, end=end_d)
    except Exception:
        vix, vix3m = None, None

    combos: list[tuple[str, dict]] = []
    if args.variant in ("v1", "both"):
        for o in _expand(V1_GRID):
            combos.append(("v1_sweep", o))
    if args.variant in ("v5", "both"):
        for o in _expand(V5_GRID):
            combos.append(("v5_sweep", o))

    total_runs = len(combos) * len(tickers)
    log.info(f"Running {len(combos)} parameter combos × {len(tickers)} "
             f"tickers = {total_runs} backtests…")

    results: list[dict] = []
    base_map = {"v1_sweep": V1_BASELINE, "v5_sweep": V5_HEDGED}
    for ci, (base_name, overrides) in enumerate(combos, 1):
        base = base_map[base_name]
        variant = replace(base, name=f"{base_name}_{ci}",
                           label=f"{base.label}_{ci}", **overrides)
        # Run across universe
        agg_trades = 0
        agg_pnl = 0.0
        agg_wins = 0
        agg_losses = 0
        agg_max_dd = 0.0
        for t in tickers:
            try:
                r = run_variant_backtest(variant, t, start_d, end_d, vix, vix3m)
                m = r.metrics()
                agg_trades += m["trades"]
                agg_pnl += m["total_pnl"]
                agg_wins += m["wins"]
                agg_losses += m["losses"]
                agg_max_dd = min(agg_max_dd, m["max_drawdown"])
            except Exception as e:
                log.error(f"  {t}/{variant.name}: {e}")
        row = {
            "combo_id": ci, "base": base_name,
            **overrides,
            "trades": agg_trades,
            "wins": agg_wins, "losses": agg_losses,
            "win_rate": round(agg_wins / max(agg_wins + agg_losses, 1) * 100, 1),
            "total_pnl": round(agg_pnl, 2),
            "avg_trade": round(agg_pnl / max(agg_trades, 1), 2),
            "max_drawdown": round(agg_max_dd, 2),
        }
        results.append(row)
        if ci % 20 == 0:
            log.info(f"  [{ci}/{len(combos)}] last: {base_name} {overrides} "
                     f"pnl={agg_pnl:+.0f} tr={agg_trades}")

    # Rank by total_pnl per trade (proxy for edge)
    results.sort(key=lambda r: r["avg_trade"], reverse=True)

    out_dir = Path("data"); out_dir.mkdir(exist_ok=True)
    csv_path = out_dir / f"sweep_results_{end_d}.csv"
    if results:
        fields = list(results[0].keys())
        with csv_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(results)
        log.info(f"CSV written: {csv_path}")

    # Top-N markdown report
    md_path = Path("docs") / f"sweep_report_{end_d}.md"
    lines = [f"# DN Variant Parameter Sweep — {end_d}", "",
             f"Window: {start_d} → {end_d} ({args.days} days)",
             f"Universe: {', '.join(tickers)}",
             f"Total combos: {len(combos)}", "",
             f"## Top {args.top} by avg P&L per trade"]
    lines.append("")
    if results:
        head_keys = [k for k in results[0].keys() if k not in ("combo_id",)]
        lines.append("| " + " | ".join(head_keys) + " |")
        lines.append("| " + " | ".join("---" for _ in head_keys) + " |")
        for r in results[:args.top]:
            lines.append("| " + " | ".join(str(r.get(k, ""))
                                              for k in head_keys) + " |")
    lines.append("")
    lines.append("## Bottom 5 (worst combos — worth avoiding)")
    lines.append("")
    for r in results[-5:]:
        lines.append(f"- combo {r['combo_id']}: {r['base']} "
                     f"pnl={r['total_pnl']:+.0f} tr={r['trades']} "
                     f"wr={r['win_rate']}% dd={r['max_drawdown']:.0f}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info(f"Report: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
