"""Run all 5 DN variants across all tier-0..3 tickers, write a
comparison report + CSV.

Usage from repo root:

    python scripts/run_dn_variant_backtest.py

Outputs:
  - docs/backtest_report_<date>.md
  - data/backtest_results_<date>.csv
"""
from __future__ import annotations

import csv
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest_engine.data_provider import fetch_bars
from backtest_engine.dn_variants_engine import run_variant_backtest, VariantResult
from strategy.delta_neutral_variants import VARIANTS, TIERS, all_tier_tickers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-6s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dn-variant-backtest")


def _fetch_vix(start: date, end: date):
    try:
        vix = fetch_bars("^VIX", interval="5m", start=start, end=end)
    except Exception as e:
        log.warning(f"^VIX fetch failed: {e}")
        vix = None
    try:
        vix3m = fetch_bars("^VIX3M", interval="5m", start=start, end=end)
    except Exception as e:
        log.warning(f"^VIX3M fetch failed: {e}")
        vix3m = None
    return vix, vix3m


def main():
    end_d = date.today()
    start_d = end_d - timedelta(days=58)   # stay inside yfinance 60-day limit
    log.info(f"Backtest window: {start_d} → {end_d}")

    log.info("Fetching VIX / VIX3M for regime filter…")
    vix, vix3m = _fetch_vix(start_d, end_d)
    log.info(f"  VIX bars: {len(vix) if vix is not None else 0}; "
             f"VIX3M bars: {len(vix3m) if vix3m is not None else 0}")

    tickers = all_tier_tickers()
    total_runs = len(tickers) * len(VARIANTS)
    log.info(f"Running {total_runs} backtests "
             f"({len(tickers)} tickers × {len(VARIANTS)} variants)…")

    results: list[dict] = []
    all_trades: list[dict] = []
    run_idx = 0
    for tier, ticker in tickers:
        for v in VARIANTS:
            run_idx += 1
            log.info(f"[{run_idx}/{total_runs}] tier{tier} {ticker} × {v.label} ({v.name})")
            try:
                r = run_variant_backtest(v, ticker, start_d, end_d, vix, vix3m)
                m = r.metrics()
                m["tier"] = tier
                results.append(m)
                for t in r.trades:
                    t["tier"] = tier
                all_trades.extend(r.trades)
            except Exception as e:
                log.error(f"  {ticker}/{v.name} FAILED: {e}", exc_info=True)
                results.append({
                    "variant": v.name, "ticker": ticker, "tier": tier,
                    "trades": 0, "error": str(e)[:200],
                })

    # ── Write CSV ─────────────────────────────────────────
    out_dir = Path("data")
    out_dir.mkdir(exist_ok=True)
    csv_path = out_dir / f"backtest_results_{end_d}.csv"
    fields = ["tier", "variant", "ticker", "trades", "wins", "losses",
              "scratches", "win_rate", "total_pnl", "avg_trade_pnl",
              "max_drawdown", "profit_factor", "avg_hold_days",
              "sharpe_ish"]
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in results:
            w.writerow(row)
    log.info(f"CSV written: {csv_path}")

    # Individual-trade CSV too
    if all_trades:
        trades_path = out_dir / f"backtest_trades_{end_d}.csv"
        with trades_path.open("w", newline="") as f:
            keys = ["tier", "variant", "ticker", "entry_time", "exit_time",
                    "contracts", "ivr_at_entry", "sigma_at_entry",
                    "entry_underlying", "pnl_usd", "pnl_pct",
                    "exit_reason", "exit_result", "hold_days"]
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            for t in all_trades:
                w.writerow(t)
        log.info(f"Trade-level CSV: {trades_path}")

    # ── Build markdown report ─────────────────────────────
    report = _build_report(results, start_d, end_d, vix, vix3m)
    md_path = Path("docs") / f"backtest_report_{end_d}.md"
    md_path.write_text(report, encoding="utf-8")
    log.info(f"Report written: {md_path}")

    return 0


def _build_report(results, start_d, end_d, vix, vix3m) -> str:
    from collections import defaultdict
    lines = []
    lines.append(f"# DN Variant Backtest Report — {end_d}")
    lines.append("")
    lines.append(f"**Window**: {start_d} → {end_d} ({(end_d - start_d).days} days, yfinance 5m bars)")
    lines.append(f"**Universe**: {sum(len(v) for v in TIERS.values())} tickers across 4 tiers")
    lines.append(f"**Variants**: {', '.join(v.name for v in VARIANTS)}")
    lines.append(f"**VIX data**: {'✅ present' if (vix is not None and not vix.empty) else '❌ missing'}")
    lines.append("")
    lines.append("## Executive summary by variant")
    lines.append("")
    lines.append("| Variant | Trades | Net P&L $ | Avg Trade $ | Win Rate | Max DD $ | PF | Hold Days |")
    lines.append("|---|---|---|---|---|---|---|---|")
    by_variant: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_variant[r["variant"]].append(r)
    for vname, rows in by_variant.items():
        tc = sum(r.get("trades", 0) for r in rows)
        total = sum(r.get("total_pnl", 0) for r in rows)
        avg_t = total / tc if tc else 0
        wins = sum(r.get("wins", 0) for r in rows)
        losses = sum(r.get("losses", 0) for r in rows)
        win_rate = wins / (wins + losses) * 100 if (wins + losses) else 0
        max_dd = min((r.get("max_drawdown", 0) for r in rows), default=0)
        gw = sum(r.get("total_pnl", 0) for r in rows if r.get("total_pnl", 0) > 0)
        gl = abs(sum(r.get("total_pnl", 0) for r in rows if r.get("total_pnl", 0) < 0))
        pf = gw / gl if gl > 0 else 0.0
        holds = [r.get("avg_hold_days", 0) for r in rows if r.get("trades", 0) > 0]
        avg_hold = sum(holds) / len(holds) if holds else 0
        lines.append(f"| {vname} | {tc} | {total:+.0f} | {avg_t:+.1f} | "
                     f"{win_rate:.0f}% | {max_dd:.0f} | {pf:.2f} | {avg_hold:.1f} |")
    lines.append("")
    lines.append("## Per-tier, per-variant detail")
    lines.append("")
    for tier in sorted(TIERS.keys()):
        tier_tickers = TIERS[tier]
        lines.append(f"### Tier {tier} — {', '.join(tier_tickers)}")
        lines.append("")
        lines.append("| Ticker | V1 base | V2 hold-day | V3 phaseB | V4 filtered | V5 hedged |")
        lines.append("|---|---|---|---|---|---|")
        for ticker in tier_tickers:
            row = [ticker]
            for v in VARIANTS:
                m = next((r for r in results
                          if r["variant"] == v.name and r["ticker"] == ticker), None)
                if not m or m.get("trades", 0) == 0:
                    row.append("—")
                else:
                    row.append(f"{m['total_pnl']:+.0f} ({m['trades']}t/{m['win_rate']:.0f}%)")
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
    lines.append("## Interpretation notes")
    lines.append("")
    lines.append("- **Cells show** `net_pnl_$ (trades/win_rate)`.")
    lines.append("- 60-day window is short for statistical significance;")
    lines.append("  use for directional signal, not absolute profit expectations.")
    lines.append("- V3-V5 target 45 DTE → fewer completed trades in a 60-day window.")
    lines.append("- V5 hedged approximates delta-hedge cost via BS pricing only;")
    lines.append("  slippage + commissions NOT included — treat as upper-bound.")
    lines.append("- Compare V1→V3 to isolate Phase-B entry construction effect.")
    lines.append("- Compare V3→V4 to see filter value-add.")
    lines.append("- Compare V4→V5 to see Phase-C risk-management value-add.")
    lines.append("")
    lines.append("## Raw data")
    lines.append("")
    lines.append(f"- Per-(variant, ticker) metrics: `data/backtest_results_{end_d}.csv`")
    lines.append(f"- Individual trades: `data/backtest_trades_{end_d}.csv`")
    lines.append(f"- Variant configs: `strategy/delta_neutral_variants.py`")
    lines.append(f"- Decisions log: `docs/dn_variant_decisions.md`")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
