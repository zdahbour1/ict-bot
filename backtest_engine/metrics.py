"""
Backtest metrics — compute summary statistics from a list of simulated trades.

Pure functions. No DB. No I/O. Unit-tested in isolation so the engine
can rely on them without needing fixtures.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Sequence
import math


@dataclass
class BacktestSummary:
    """Column-aligned with backtest_runs so the writer can pass the
    dict through unchanged."""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    scratches: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float | None = None
    profit_factor: float | None = None
    avg_hold_min: float = 0.0
    max_win_streak: int = 0
    max_loss_streak: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _classify(pnl_usd: float, stored: str | None) -> str:
    if stored in ("WIN", "LOSS", "SCRATCH"):
        return stored
    if pnl_usd > 0:
        return "WIN"
    if pnl_usd < 0:
        return "LOSS"
    return "SCRATCH"


def _max_drawdown(pnls: list[float]) -> float:
    if not pnls:
        return 0.0
    cum = peak = worst = 0.0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        dd = cum - peak
        if dd < worst:
            worst = dd
    return worst


def _sharpe(pnls: list[float]) -> float | None:
    n = len(pnls)
    if n < 2:
        return None
    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var)


def _profit_factor(pnls: list[float]) -> float | None:
    gp = sum(p for p in pnls if p > 0)
    gl = sum(-p for p in pnls if p < 0)
    if gl == 0:
        return None
    return gp / gl


def _streak(results: list[str], target: str) -> int:
    best = cur = 0
    for r in results:
        if r == target:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def compute_summary(trades: Sequence[dict]) -> BacktestSummary:
    """Trades must be ordered by exit_time for drawdown/streak accuracy."""
    s = BacktestSummary()
    if not trades:
        return s

    pnls, results, holds, wins, losses = [], [], [], [], []
    for t in trades:
        pnl = float(t.get("pnl_usd", 0.0) or 0.0)
        r = _classify(pnl, t.get("exit_result"))
        pnls.append(pnl)
        results.append(r)
        if t.get("hold_minutes") is not None:
            try:
                holds.append(float(t["hold_minutes"]))
            except (TypeError, ValueError):
                pass
        if r == "WIN":
            s.wins += 1
            wins.append(pnl)
        elif r == "LOSS":
            s.losses += 1
            losses.append(pnl)
        else:
            s.scratches += 1

    s.total_trades = len(trades)
    s.total_pnl = round(sum(pnls), 2)
    decided = s.wins + s.losses
    s.win_rate = round(100.0 * s.wins / decided, 2) if decided else 0.0
    s.avg_win = round(sum(wins) / len(wins), 2) if wins else 0.0
    s.avg_loss = round(sum(losses) / len(losses), 2) if losses else 0.0
    s.max_drawdown = round(_max_drawdown(pnls), 2)
    sh = _sharpe(pnls)
    s.sharpe_ratio = round(sh, 4) if sh is not None else None
    pf = _profit_factor(pnls)
    s.profit_factor = round(pf, 4) if pf is not None else None
    s.avg_hold_min = round(sum(holds) / len(holds), 1) if holds else 0.0
    s.max_win_streak = _streak(results, "WIN")
    s.max_loss_streak = _streak(results, "LOSS")
    return s
