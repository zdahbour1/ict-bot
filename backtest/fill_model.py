"""
Simulated fill model for backtests.

Designs intentionally keep slippage conservative — real IB fills on
liquid options are typically mid-price or better, but backtests shouldn't
assume best-case execution. Adjust these knobs per run from the config.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FillConfig:
    """Controls backtest fill simulation."""
    slippage_pct: float = 0.002   # 0.2% worse than target price on entry/exit
    commission_per_contract: float = 0.65  # IBKR Lite commission per contract
    use_next_bar_open: bool = True  # fills happen at the bar AFTER the signal


def simulate_entry_fill(signal_price: float, contracts: int,
                        direction: str, cfg: FillConfig = FillConfig()) -> dict:
    """Simulate an entry fill on the next bar.

    Slippage moves AGAINST us: LONG pays up, SHORT collects less.
    Commission is subtracted from the trade's cash at close, returned here
    so the engine records it.
    """
    if direction == "LONG":
        fill_price = signal_price * (1.0 + cfg.slippage_pct)
    else:
        fill_price = signal_price * (1.0 - cfg.slippage_pct)
    commission = cfg.commission_per_contract * contracts
    return {
        "fill_price": round(fill_price, 4),
        "slippage_paid": round(abs(fill_price - signal_price) * contracts, 4),
        "commission": round(commission, 4),
    }


def simulate_exit_fill(signal_price: float, contracts: int,
                       direction: str, cfg: FillConfig = FillConfig()) -> dict:
    """Exit mirrors entry — slippage moves the fill against us."""
    if direction == "LONG":
        fill_price = signal_price * (1.0 - cfg.slippage_pct)
    else:
        fill_price = signal_price * (1.0 + cfg.slippage_pct)
    commission = cfg.commission_per_contract * contracts
    return {
        "fill_price": round(fill_price, 4),
        "slippage_paid": round(abs(fill_price - signal_price) * contracts, 4),
        "commission": round(commission, 4),
    }


def compute_pnl(entry_fill: float, exit_fill: float, contracts: int,
                direction: str, total_commission: float = 0.0) -> dict:
    """Compute dollar + percent P&L for a simulated round trip.

    Options contracts = 100 shares, so dollar P&L multiplies by 100.
    pnl_pct is relative to entry (same formula as live exit_conditions).
    """
    if direction == "LONG":
        pnl_per_contract = exit_fill - entry_fill
    else:
        pnl_per_contract = entry_fill - exit_fill
    pnl_usd = pnl_per_contract * contracts * 100.0 - total_commission
    pnl_pct = (exit_fill - entry_fill) / entry_fill if entry_fill else 0.0
    if direction == "SHORT":
        pnl_pct = -pnl_pct
    return {
        "pnl_per_contract": round(pnl_per_contract, 4),
        "pnl_usd": round(pnl_usd, 2),
        "pnl_pct": round(pnl_pct, 4),
    }
