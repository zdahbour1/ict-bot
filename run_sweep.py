"""
CLI for running parameter sweeps.

Usage:
    python run_sweep.py <payload.json>

payload.json example:
    {
        "name_prefix": "ict-pt-sl",
        "strategy": "ict",
        "tickers": ["QQQ", "SPY"],
        "start_date": "2026-02-20",
        "end_date": "2026-04-20",
        "base_config": {
            "base_interval": "5m",
            "option_dte_days": 7,
            "option_vol": 0.20
        },
        "grid": {
            "profit_target": [0.50, 1.00, 1.50, 2.00],
            "stop_loss": [0.30, 0.50, 0.70]
        }
    }

Each cell spawns a backtest_runs row (visible in the dashboard).
Prints a ranked summary table at the end.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("run_sweep")

    if len(argv) < 2:
        print("usage: python run_sweep.py '<payload_json_or_@path>'",
              file=sys.stderr)
        return 2

    src = argv[1]
    if src.startswith("@"):
        with open(src[1:]) as f:
            req = json.load(f)
    else:
        try:
            req = json.loads(src)
        except json.JSONDecodeError as e:
            print(f"bad payload: {e}", file=sys.stderr)
            return 2

    from backtest_engine.sweep import run_sweep, format_results_table

    results = run_sweep(
        strategy_name=req["strategy"],
        tickers=list(req["tickers"]),
        start_date=_parse_date(req["start_date"]),
        end_date=_parse_date(req["end_date"]),
        base_config=req.get("base_config", {}),
        grid=req.get("grid", {}),
        name_prefix=req.get("name_prefix", "sweep"),
        progress_cb=lambda msg: log.info(msg),
    )

    print("\n" + "=" * 80)
    print(f"SWEEP RESULTS — {len(results)} runs, sorted by total P&L:")
    print("=" * 80)
    print(format_results_table(results))
    print("=" * 80)

    # One-liner summary (ASCII-only so Windows cp1252 console doesn't crash)
    if results:
        best = results[0]
        pf_str = f"{best.profit_factor:.2f}" if best.profit_factor is not None else "n/a"
        if best.total_pnl > 0:
            print(f"\n** Winner: {best.cell.label()}")
            print(f"   Run #{best.run_id}: ${best.total_pnl:+.2f}, "
                  f"{best.total_trades} trades, PF {pf_str}")
        else:
            print(f"\n[!] No profitable config found in this grid.")
            print(f"   Best (least-losing): {best.cell.label()} -> ${best.total_pnl:+.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
