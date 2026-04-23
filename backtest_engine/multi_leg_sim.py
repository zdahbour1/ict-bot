"""
Multi-leg simulation helpers (ENH-038 Part 2).

Backtest engine support for strategies that implement ``place_legs()`` —
iron condors, spreads, hedged positions. Each leg is priced independently
with Black-Scholes (or Black '76 for FOP), summed into a combined
position P&L the existing exit logic can evaluate against.

Design:
- ``build_leg_state(legs, underlying, now, sigma, r)`` — prices each leg
  at open and returns a list of per-leg state dicts.
- ``price_legs_now(leg_state, underlying_now, now, sigma, r)`` — repriced
  at bar i; returns (list[current_price_per_leg], per_share_pnl_sum).
- ``synth_price(entry_basis, net_pnl_per_share, basis)`` — collapses the
  multi-leg position into a single scalar "option_price" compatible with
  ``evaluate_exit``. pnl_pct = net_pnl_per_share / basis; synthetic price
  = entry_basis * (1 + pnl_pct).
- ``build_legs_for_writer(leg_state, exit_prices, exit_time)`` — returns
  the list of per-leg dicts that ``record_multi_leg_trade`` writes to
  ``backtest_trade_legs``.

We keep the strategy's single-leg path untouched; multi-leg only kicks
in when ``strategy.place_legs(signal)`` returns a non-empty list.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from typing import Iterable, List, Optional

from backtest_engine.option_pricer import bs_price


def _leg_to_dict(leg) -> dict:
    """Accept either a LegSpec dataclass or a plain dict. Returns a dict."""
    if isinstance(leg, dict):
        return dict(leg)
    if is_dataclass(leg):
        return asdict(leg)
    # Attribute fallback
    return {
        k: getattr(leg, k, None) for k in (
            "sec_type", "symbol", "direction", "contracts",
            "strike", "right", "expiry", "multiplier",
            "exchange", "currency", "leg_role", "underlying",
        )
    }


def _dte_days(expiry_yyyymmdd: Optional[str], now: datetime) -> float:
    """Days between `now` and option expiry. Clamped to >= 0."""
    if not expiry_yyyymmdd:
        return 7.0  # sane default for backtest when strategy omits it
    try:
        exp = datetime.strptime(expiry_yyyymmdd, "%Y%m%d").date()
    except Exception:
        return 7.0
    today = now.date() if isinstance(now, datetime) else (
        now if isinstance(now, date) else date.today())
    return max((exp - today).days, 0.0)


def _sign(direction: str) -> int:
    return +1 if str(direction).upper() == "LONG" else -1


def price_leg(
    leg: dict,
    underlying: float,
    now: datetime,
    *,
    sigma: float = 0.20,
    r: float = 0.04,
) -> float:
    """Black-Scholes (or Black '76 for FOP) price of a single leg."""
    sec = (leg.get("sec_type") or "OPT").upper()
    right = (leg.get("right") or "C").upper()
    strike = float(leg.get("strike") or underlying)
    dte = _dte_days(leg.get("expiry"), now)
    T = dte / 365.0
    model = "black76" if sec == "FOP" else "bs"
    return bs_price(underlying, strike, T, r, sigma, right, model=model)


def build_leg_state(
    legs: Iterable,
    underlying_entry: float,
    entry_time: datetime,
    *,
    sigma: float = 0.20,
    r: float = 0.04,
) -> List[dict]:
    """Initialize per-leg state at entry. Returns a list of dicts, one
    per leg, carrying the invariant contract metadata, the BS entry
    price, and the sign (+1 LONG / -1 SHORT).
    """
    state: List[dict] = []
    for idx, leg in enumerate(legs):
        d = _leg_to_dict(leg)
        entry_px = price_leg(d, underlying_entry, entry_time,
                             sigma=sigma, r=r)
        state.append({
            "leg_index": idx,
            "leg_role": d.get("leg_role"),
            "sec_type": d.get("sec_type", "OPT"),
            "symbol": d.get("symbol"),
            "underlying": d.get("underlying"),
            "strike": float(d.get("strike")) if d.get("strike") is not None else None,
            "right": d.get("right"),
            "expiry": d.get("expiry"),
            "multiplier": int(d.get("multiplier") or 100),
            "direction": d.get("direction", "LONG"),
            "contracts": int(d.get("contracts") or 1),
            "entry_price": float(entry_px),
            "entry_time": entry_time,
            "_sign": _sign(d.get("direction", "LONG")),
        })
    return state


def price_legs_now(
    leg_state: List[dict],
    underlying_now: float,
    now: datetime,
    *,
    sigma: float = 0.20,
    r: float = 0.04,
) -> tuple[List[float], float]:
    """Reprice each leg at the current bar and return:
    - list of current per-leg prices (same length / order as leg_state)
    - net P&L per share across all legs = sum(sign * (now - entry) * contracts)

    "Per share" here = per option contract-share (multiply by multiplier
    for dollars). Multiplier is applied by the caller / writer.
    """
    prices: List[float] = []
    net_pnl = 0.0
    for s in leg_state:
        px = price_leg(s, underlying_now, now, sigma=sigma, r=r)
        prices.append(px)
        net_pnl += s["_sign"] * (px - s["entry_price"]) * s["contracts"]
    return prices, net_pnl


def entry_basis(leg_state: List[dict]) -> float:
    """Reference basis for pct-P&L: sum of |entry_price * contracts| across
    legs. Non-zero denominator for credit/debit spreads — a flat iron
    condor with net ~0 still has gross premium on each leg.
    """
    b = sum(abs(s["entry_price"]) * s["contracts"] for s in leg_state)
    return b or 1.0   # guard against accidental zero


def synth_price(entry_proxy: float, net_pnl_per_share: float,
                basis: float) -> float:
    """Collapse multi-leg P&L into a single scalar so the existing
    single-leg evaluate_exit logic still works.

    synthetic_price = entry_proxy * (1 + net_pnl / basis)
    """
    pct = (net_pnl_per_share / basis) if basis > 0 else 0.0
    return float(entry_proxy) * (1.0 + pct)


def build_legs_for_writer(
    leg_state: List[dict],
    exit_prices: List[float],
    exit_time: datetime,
) -> List[dict]:
    """Produce the list of leg dicts ``record_multi_leg_trade`` expects.

    Fields match backtest_trade_legs columns (see migration 011):
    leg_index, leg_role, sec_type, symbol, underlying, strike, right,
    expiry, multiplier, direction, contracts, entry_price, exit_price,
    entry_time, exit_time. pnl_usd is left to the writer so the default
    (exit - entry) * contracts * mult * sign calc kicks in.
    """
    if len(exit_prices) != len(leg_state):
        raise ValueError(
            f"exit_prices length {len(exit_prices)} != "
            f"leg_state length {len(leg_state)}"
        )
    out: List[dict] = []
    for s, xp in zip(leg_state, exit_prices):
        out.append({
            "leg_index": s["leg_index"],
            "leg_role": s["leg_role"],
            "sec_type": s["sec_type"],
            "symbol": s["symbol"],
            "underlying": s["underlying"],
            "strike": s["strike"],
            "right": s["right"],
            "expiry": s["expiry"],
            "multiplier": s["multiplier"],
            "direction": s["direction"],
            "contracts": s["contracts"],
            "entry_price": float(s["entry_price"]),
            "exit_price": float(xp),
            "entry_time": s["entry_time"],
            "exit_time": exit_time,
        })
    return out
