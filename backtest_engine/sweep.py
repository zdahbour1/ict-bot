"""
Parameter sweep runner — grid-search over backtest configs.

Given a strategy + base config + a grid of parameters to vary, runs
N backtests (one per grid cell) and summarizes which cell won by
profit factor / total P&L / Sharpe.

Sequential by default — IB + Postgres can't handle parallel backtests
well on one machine. If you need parallel, shell out to multiple
`run_backtest_engine.py` subprocesses from the caller.
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

log = logging.getLogger(__name__)


@dataclass
class SweepCell:
    """One config in the grid. `overrides` layers on top of the base
    config — e.g. {"profit_target": 1.5, "stop_loss": 0.5}."""
    overrides: dict[str, float | int | str | list[str]] = field(default_factory=dict)

    def label(self) -> str:
        """Short string summary for reports."""
        return " ".join(
            f"{k}={v}" for k, v in sorted(self.overrides.items())
        )


@dataclass
class SweepResult:
    run_id: int
    cell: SweepCell
    total_pnl: float
    profit_factor: Optional[float]
    win_rate: float
    total_trades: int
    max_drawdown: float
    sharpe_ratio: Optional[float]
    duration_sec: Optional[float]
    error_message: Optional[str] = None


def build_grid(params: dict[str, list]) -> list[SweepCell]:
    """Turn {key: [v1, v2], key2: [w1]} → N cells, one per combination.

    Example:
        build_grid({"profit_target": [0.5, 1.0, 1.5],
                    "stop_loss": [0.4, 0.6]})
        → 6 cells
    """
    if not params:
        return [SweepCell()]
    keys = sorted(params.keys())
    values = [params[k] for k in keys]
    return [
        SweepCell(overrides=dict(zip(keys, combo)))
        for combo in itertools.product(*values)
    ]


def run_sweep(
    *,
    strategy_name: str,
    tickers: list[str],
    start_date: date,
    end_date: date,
    base_config: dict,
    grid: dict[str, list],
    name_prefix: str = "sweep",
    progress_cb: Optional[Callable[[str], None]] = None,
) -> list[SweepResult]:
    """Run every cell of the grid and return a list of SweepResult,
    sorted by total_pnl descending (best first)."""
    from db.connection import get_session
    from sqlalchemy import text
    from backtest_engine.engine import run_backtest

    # Resolve strategy_id once
    session = get_session()
    row = session.execute(
        text("SELECT strategy_id FROM strategies WHERE name = :n AND enabled = TRUE"),
        {"n": strategy_name},
    ).fetchone()
    session.close()
    if row is None:
        raise ValueError(f"Strategy '{strategy_name}' not found or disabled")
    strategy_id = int(row[0])

    cells = build_grid(grid)
    total = len(cells)
    results: list[SweepResult] = []

    if progress_cb:
        progress_cb(f"sweep starting: {strategy_name} × {len(tickers)} tickers × "
                    f"{total} grid cells")

    for idx, cell in enumerate(cells, 1):
        cfg = {**base_config, **cell.overrides}
        label = cell.label()
        run_name = f"{name_prefix} [{idx}/{total}] {label}"

        if progress_cb:
            progress_cb(f"[{idx}/{total}] {label}")

        try:
            result = run_backtest(
                tickers=list(tickers),
                start_date=start_date,
                end_date=end_date,
                strategy_id=strategy_id,
                config=cfg,
                run_name=run_name[:100],  # name column limit
            )
            summary = result["summary"]
            results.append(SweepResult(
                run_id=result["run_id"],
                cell=cell,
                total_pnl=float(summary.get("total_pnl") or 0),
                profit_factor=summary.get("profit_factor"),
                win_rate=float(summary.get("win_rate") or 0),
                total_trades=int(summary.get("total_trades") or 0),
                max_drawdown=float(summary.get("max_drawdown") or 0),
                sharpe_ratio=summary.get("sharpe_ratio"),
                duration_sec=None,
            ))
        except Exception as e:
            log.exception(f"sweep cell failed: {label}")
            results.append(SweepResult(
                run_id=-1, cell=cell, total_pnl=0.0,
                profit_factor=None, win_rate=0.0, total_trades=0,
                max_drawdown=0.0, sharpe_ratio=None, duration_sec=None,
                error_message=str(e),
            ))

    # Sort by total_pnl desc — winners first
    results.sort(key=lambda r: r.total_pnl, reverse=True)

    if progress_cb:
        best = results[0] if results else None
        if best:
            progress_cb(
                f"sweep done. Best: {best.cell.label()} → "
                f"P&L ${best.total_pnl:+.2f}, "
                f"PF {best.profit_factor:.2f}"
                if best.profit_factor is not None else
                f"sweep done. Best: {best.cell.label()} → P&L ${best.total_pnl:+.2f}"
            )

    return results


def format_results_table(results: list[SweepResult]) -> str:
    """Plain-text table for logs / CLI output."""
    if not results:
        return "(no results)"
    rows = [
        ("PNL", "PF", "Win%", "Trades", "MaxDD", "Params"),
    ]
    for r in results:
        rows.append((
            f"{r.total_pnl:+.2f}",
            f"{r.profit_factor:.2f}" if r.profit_factor is not None else "—",
            f"{r.win_rate:.1f}%",
            str(r.total_trades),
            f"{r.max_drawdown:+.2f}",
            r.cell.label() + (f"  [ERR: {r.error_message}]" if r.error_message else ""),
        ))
    # Column widths
    widths = [max(len(row[c]) for row in rows) for c in range(len(rows[0]))]
    lines = []
    for row in rows:
        lines.append("  ".join(cell.ljust(w) for cell, w in zip(row, widths)))
    return "\n".join(lines)
