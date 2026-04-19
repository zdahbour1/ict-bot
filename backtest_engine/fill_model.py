"""Simulated fill model — slippage + commission for backtest entries/exits."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FillConfig:
    """Conservative defaults. Overridden per backtest run via config JSONB."""
    slippage_pct: float = 0.002               # 0.2% against us
    commission_per_contract: float = 0.65     # IBKR Lite
    use_next_bar_open: bool = True


def simulate_entry_fill(signal_price: float, contracts: int,
                        direction: str, cfg: FillConfig = FillConfig()) -> dict:
    if direction == "LONG":
        fill = signal_price * (1.0 + cfg.slippage_pct)
    else:
        fill = signal_price * (1.0 - cfg.slippage_pct)
    return {
        "fill_price": round(fill, 4),
        "slippage_paid": round(abs(fill - signal_price) * contracts, 4),
        "commission": round(cfg.commission_per_contract * contracts, 4),
    }


def simulate_exit_fill(signal_price: float, contracts: int,
                       direction: str, cfg: FillConfig = FillConfig()) -> dict:
    if direction == "LONG":
        fill = signal_price * (1.0 - cfg.slippage_pct)
    else:
        fill = signal_price * (1.0 + cfg.slippage_pct)
    return {
        "fill_price": round(fill, 4),
        "slippage_paid": round(abs(fill - signal_price) * contracts, 4),
        "commission": round(cfg.commission_per_contract * contracts, 4),
    }


def compute_pnl(entry_fill: float, exit_fill: float, contracts: int,
                direction: str, total_commission: float = 0.0) -> dict:
    """Options: 100 shares/contract → dollar P&L multiplier."""
    if direction == "LONG":
        per = exit_fill - entry_fill
    else:
        per = entry_fill - exit_fill
    pnl_usd = per * contracts * 100.0 - total_commission
    pnl_pct = (exit_fill - entry_fill) / entry_fill if entry_fill else 0.0
    if direction == "SHORT":
        pnl_pct = -pnl_pct
    return {
        "pnl_per_contract": round(per, 4),
        "pnl_usd": round(pnl_usd, 2),
        "pnl_pct": round(pnl_pct, 4),
    }
