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
    per_ticker: bool = False,
) -> list[SweepResult]:
    """Run every cell of the grid and return a list of SweepResult,
    sorted by total_pnl descending (best first).

    If per_ticker=True, each ticker gets its own backtest per grid cell.
    So for N tickers × M grid cells → N × M backtest runs. This is what
    you want to see "ORB works on QQQ but not AAPL" type patterns.
    Default (False) aggregates all tickers into one run per grid cell.
    """
    from db.connection import get_session
    from sqlalchemy import text
    from backtest_engine.engine import run_backtest

    # Resolve strategy_id + class_path once
    session = get_session()
    row = session.execute(
        text("SELECT strategy_id, class_path FROM strategies "
             "WHERE name = :n AND enabled = TRUE"),
        {"n": strategy_name},
    ).fetchone()
    session.close()
    if row is None:
        raise ValueError(f"Strategy '{strategy_name}' not found or disabled")
    strategy_id = int(row[0])
    class_path = row[1]

    # BUG FIX: previously sweep passed strategy_id only to run_backtest()
    # but NOT a strategy instance. The engine falls back to SignalEngine
    # (ICT) when strategy=None, so every non-ICT sweep was silently
    # running ICT. Caught when VWAP + ORB per-ticker sweeps returned
    # identical numbers — the tell was exact matches on AAPL/SPY/IWM.
    # Now we instantiate the plugin the same way run_backtest_engine.py
    # does, and pass the instance to run_backtest.
    strategy_instance = None
    if strategy_name != "ict":
        import importlib
        module_path, class_name = class_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        strategy_cls = getattr(module, class_name)
        strategy_instance = strategy_cls()
        # Apply scoped settings (same as run_backtest_engine.py)
        try:
            session = get_session()
            srows = session.execute(
                text("SELECT key, value FROM settings WHERE strategy_id = :sid"),
                {"sid": strategy_id},
            ).fetchall()
            session.close()
            scoped = {k: v for k, v in srows}
            if hasattr(strategy_instance, "configure"):
                strategy_instance.configure(scoped)
        except Exception as e:
            log.warning(f"sweep couldn't apply scoped settings for {strategy_name}: {e}")

    cells = build_grid(grid)

    # If per-ticker, multiply cells × tickers. Each task is (ticker, cell).
    if per_ticker:
        tasks: list[tuple[list[str], SweepCell]] = []
        for t in tickers:
            for c in cells:
                # Inject ticker into the label via a synthetic override
                labeled = SweepCell(overrides={"ticker": t, **c.overrides})
                tasks.append(([t], labeled))
    else:
        tasks = [(list(tickers), c) for c in cells]

    total = len(tasks)
    results: list[SweepResult] = []

    if progress_cb:
        mode = (f"per-ticker ({len(tickers)}t × {len(cells)}c = {total})"
                if per_ticker else f"{total} grid cells")
        progress_cb(f"sweep starting: {strategy_name} × {mode}")

    for idx, (run_tickers, cell) in enumerate(tasks, 1):
        # When per_ticker, drop the synthetic "ticker" override from cfg
        engine_overrides = {k: v for k, v in cell.overrides.items() if k != "ticker"}
        cfg = {**base_config, **engine_overrides}
        label = cell.label()
        run_name = f"{name_prefix} [{idx}/{total}] {label}"

        if progress_cb:
            progress_cb(f"[{idx}/{total}] {label}")

        try:
            result = run_backtest(
                tickers=run_tickers,
                start_date=start_date,
                end_date=end_date,
                strategy_id=strategy_id,
                strategy=strategy_instance,   # plugin or None (ICT legacy path)
                config=cfg,
                run_name=run_name[:100],
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
