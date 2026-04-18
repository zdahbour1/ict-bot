"""
Backtest metrics — compute summary statistics from a list of simulated trades.

Pure functions, no DB, no I/O. Unit-tested in isolation so the engine
can rely on them without needing DB fixtures.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Sequence
import math


@dataclass
class BacktestSummary:
    """Summary stats for a completed backtest run.

    Field names match the backtest_runs table columns so the ORM layer
    can pass the dict straight through.
    """
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    scratches: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0          # percentage, 0-100
    avg_win: float = 0.0
    avg_loss: float = 0.0           # signed (negative)
    max_drawdown: float = 0.0       # dollars, negative
    sharpe_ratio: float | None = None
    profit_factor: float | None = None
    avg_hold_min: float = 0.0
    max_win_streak: int = 0
    max_loss_streak: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _classify_result(pnl_usd: float, stored_result: str | None) -> str:
    """Prefer the engine's stored classification, fall back to P&L sign."""
    if stored_result in ("WIN", "LOSS", "SCRATCH"):
        return stored_result
    if pnl_usd > 0:
        return "WIN"
    if pnl_usd < 0:
        return "LOSS"
    return "SCRATCH"


def _max_drawdown(pnl_series: list[float]) -> float:
    """Running-peak drawdown on the cumulative P&L curve.

    Returns the largest peak-to-trough distance as a negative number
    (e.g. -1250.0 means a $1,250 drawdown from the peak).
    """
    if not pnl_series:
        return 0.0
    cum = 0.0
    peak = 0.0
    worst = 0.0
    for pnl in pnl_series:
        cum += pnl
        if cum > peak:
            peak = cum
        dd = cum - peak
        if dd < worst:
            worst = dd
    return worst


def _sharpe(pnls: list[float]) -> float | None:
    """Per-trade Sharpe (mean / stdev). Returns None if < 2 trades or stdev=0.

    This is NOT annualized — it's the dimensionless per-trade ratio,
    which is what strategy researchers typically compare in backtests.
    """
    n = len(pnls)
    if n < 2:
        return None
    mean = sum(pnls) / n
    variance = sum((p - mean) ** 2 for p in pnls) / (n - 1)
    if variance <= 0:
        return None
    stdev = math.sqrt(variance)
    return mean / stdev


def _profit_factor(pnls: list[float]) -> float | None:
    """Gross profit / gross loss. None if no losses (infinite PF)."""
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = sum(-p for p in pnls if p < 0)
    if gross_loss == 0:
        return None
    return gross_profit / gross_loss


def _longest_streak(results: list[str], target: str) -> int:
    longest = current = 0
    for r in results:
        if r == target:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def compute_summary(trades: Sequence[dict]) -> BacktestSummary:
    """Compute summary stats for a sequence of backtest trade dicts.

    Each trade dict must carry: pnl_usd (float), exit_result (str, optional),
    hold_minutes (float, optional). Trades are processed in order, so callers
    should pass them sorted by exit_time for drawdown/streak accuracy.
    """
    s = BacktestSummary()
    if not trades:
        return s

    pnls = []
    results = []
    hold_mins = []
    wins_list = []
    losses_list = []

    for t in trades:
        pnl = float(t.get("pnl_usd", 0.0) or 0.0)
        result = _classify_result(pnl, t.get("exit_result"))
        pnls.append(pnl)
        results.append(result)
        if t.get("hold_minutes") is not None:
            try:
                hold_mins.append(float(t["hold_minutes"]))
            except (TypeError, ValueError):
                pass
        if result == "WIN":
            s.wins += 1
            wins_list.append(pnl)
        elif result == "LOSS":
            s.losses += 1
            losses_list.append(pnl)
        else:
            s.scratches += 1

    s.total_trades = len(trades)
    s.total_pnl = round(sum(pnls), 2)
    decided = s.wins + s.losses  # exclude scratches from win rate
    s.win_rate = round(100.0 * s.wins / decided, 2) if decided else 0.0
    s.avg_win = round(sum(wins_list) / len(wins_list), 2) if wins_list else 0.0
    s.avg_loss = round(sum(losses_list) / len(losses_list), 2) if losses_list else 0.0
    s.max_drawdown = round(_max_drawdown(pnls), 2)
    sharpe = _sharpe(pnls)
    s.sharpe_ratio = round(sharpe, 4) if sharpe is not None else None
    pf = _profit_factor(pnls)
    s.profit_factor = round(pf, 4) if pf is not None else None
    s.avg_hold_min = round(sum(hold_mins) / len(hold_mins), 1) if hold_mins else 0.0
    s.max_win_streak = _longest_streak(results, "WIN")
    s.max_loss_streak = _longest_streak(results, "LOSS")
    return s
