"""Variant-aware DN backtest engine.

Runs a configured ``DNVariant`` across historical bars for a ticker
and returns a list of completed trade dicts + performance metrics.

Fully self-contained — does NOT touch the live strategy / live
exit_manager code paths. Shares BS pricing and data provider with
the existing engine.

Assumptions / simplifications (documented in dn_variant_decisions.md):
- Fixed 20% IV for pricing when no live IV source available
- Earnings blackout via static ``data/macro_events.csv`` (best-effort)
- VIX regime filter reads ``^VIX`` + ``^VIX3M`` from yfinance
- 5-minute base bars (yfinance 60-day limit)
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from backtest_engine.data_provider import fetch_bars, fetch_multi_timeframe
from backtest_engine.option_pricer import bs_price, bs_greeks
from strategy.delta_neutral_variants import DNVariant

log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────

def _round_to(x: float, step: float) -> float:
    if step <= 0:
        return x
    return round(x / step) * step


def strike_by_delta(underlying: float, target_delta: float,
                     dte_days: float, sigma: float, right: str,
                     strike_interval: float = 5.0) -> float:
    """Solve for strike K such that BS delta ≈ target_delta.

    Bisection on a strike grid. Works for both calls (+delta target)
    and puts (negative delta target). Returns the listed strike
    (rounded to ``strike_interval``) closest to the theoretical
    solution.
    """
    T = max(dte_days, 1.0) / 365.0
    sigma = max(sigma, 0.05)
    # Candidate grid: ±50% around underlying in strike_interval steps
    lo = _round_to(underlying * 0.50, strike_interval)
    hi = _round_to(underlying * 1.50, strike_interval)
    if lo == hi:
        return underlying
    # For each strike, compute delta; pick the one closest to target.
    best_k = underlying
    best_err = float("inf")
    k = lo
    while k <= hi:
        try:
            g = bs_greeks(underlying, k, T, 0.04, sigma, right)
            err = abs(g.delta - target_delta)
            if err < best_err:
                best_err = err
                best_k = k
        except Exception:
            pass
        k += strike_interval
    return best_k


def _is_earnings_blackout(ticker: str, bar_date: date,
                           buffer_days: int = 2) -> bool:
    """Best-effort earnings blackout using a small in-module table.
    Indices (SPY/QQQ/IWM) never blackout. Real implementation would
    load from data/macro_events.csv."""
    # Indices and ETFs are never blackouted
    if ticker in ("SPY", "QQQ", "IWM", "DIA"):
        return False
    # Minimal hand-curated list for Q1-Q2 2026. In production this
    # would come from IB fundamentals + a cron refresh.
    EARNINGS: dict[str, list[date]] = {
        "AAPL": [date(2026, 5, 1)],
        "MSFT": [date(2026, 4, 29)],
        "NVDA": [date(2026, 5, 28)],
        "AMZN": [date(2026, 5, 1)],
        "GOOGL": [date(2026, 4, 29)],
        "META": [date(2026, 4, 30)],
        "TSLA": [date(2026, 4, 23)],
        "AMD": [date(2026, 5, 6)],
        "AVGO": [date(2026, 6, 5)],
        "COIN": [date(2026, 5, 8)],
        "MSTR": [date(2026, 5, 1)],
        "DELL": [date(2026, 5, 29)],
        "INTC": [date(2026, 4, 24)],
        "PLTR": [date(2026, 5, 5)],
        "MU": [date(2026, 6, 25)],
    }
    MACRO: list[date] = [
        date(2026, 4, 30),  # FOMC
        date(2026, 5, 13),  # CPI
        date(2026, 6, 11),  # FOMC
        date(2026, 6, 12),  # CPI
    ]
    events = EARNINGS.get(ticker, []) + MACRO
    for ev in events:
        if abs((bar_date - ev).days) <= buffer_days:
            return True
    return False


def _vix_contango(vix_bars: pd.DataFrame, vix3m_bars: pd.DataFrame,
                   when: pd.Timestamp) -> bool:
    """Returns True iff VIX < VIX3M at `when` (contango = OK to enter).
    Missing data defaults to True (don't block entries when data
    unavailable — fail safe toward more trades so variant is
    distinguishable)."""
    try:
        if vix_bars is None or vix_bars.empty or vix3m_bars is None or vix3m_bars.empty:
            return True
        def _last_on_or_before(df, ts):
            idx = df.index[df.index <= ts]
            if len(idx) == 0:
                return None
            return float(df.loc[idx[-1], "close"])
        v1 = _last_on_or_before(vix_bars, when)
        v3 = _last_on_or_before(vix3m_bars, when)
        if v1 is None or v3 is None or v3 <= 0:
            return True
        return (v1 / v3) < 1.0
    except Exception:
        return True


def _ivr_bucket_size(ivr: float, base: int) -> int:
    """IVR-bucketed sizing: 30-50 → 1×, 50-70 → 2×, 70+ → 3×."""
    if ivr >= 70:
        return max(1, base * 3)
    if ivr >= 50:
        return max(1, base * 2)
    return max(1, base)


def _approx_ivr(series: pd.Series, current: float) -> float:
    """Cheap IVR approximation from a series of realized-vol
    values. Returns 0-100 percentile of current vs the series
    min/max. Used when we don't have a real ATM-IV time series."""
    if series is None or len(series) < 10:
        return 50.0
    try:
        s_min, s_max = float(series.min()), float(series.max())
        if s_max <= s_min:
            return 50.0
        return max(0.0, min(100.0, 100.0 * (current - s_min) / (s_max - s_min)))
    except Exception:
        return 50.0


# ── Simulator ─────────────────────────────────────────────────

@dataclass
class VariantResult:
    variant_name: str
    ticker: str
    trades: list[dict] = field(default_factory=list)

    def metrics(self) -> dict:
        """Aggregate perf. Handles zero-trade case."""
        if not self.trades:
            return {
                "variant": self.variant_name, "ticker": self.ticker,
                "trades": 0, "wins": 0, "losses": 0, "scratches": 0,
                "win_rate": 0.0, "total_pnl": 0.0, "avg_trade_pnl": 0.0,
                "max_drawdown": 0.0, "profit_factor": 0.0,
                "avg_hold_days": 0.0, "sharpe_ish": 0.0,
            }
        pnls = [t["pnl_usd"] for t in self.trades]
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        scratches = sum(1 for p in pnls if p == 0)
        total = sum(pnls)
        gw = sum(p for p in pnls if p > 0)
        gl = abs(sum(p for p in pnls if p < 0))
        # Running max-DD
        peak = 0.0
        running = 0.0
        max_dd = 0.0
        for p in pnls:
            running += p
            peak = max(peak, running)
            max_dd = min(max_dd, running - peak)
        holds = [t.get("hold_days", 0) for t in self.trades]
        # sharpe_ish: per-trade mean / stdev × sqrt(trades), very loose
        import statistics
        std = statistics.pstdev(pnls) if len(pnls) > 1 else 1.0
        sharpe_ish = (total / len(pnls)) / (std if std > 0 else 1.0) * math.sqrt(len(pnls))
        return {
            "variant": self.variant_name, "ticker": self.ticker,
            "trades": len(self.trades),
            "wins": wins, "losses": losses, "scratches": scratches,
            "win_rate": round(wins / max(wins + losses, 1) * 100, 1),
            "total_pnl": round(total, 2),
            "avg_trade_pnl": round(total / len(pnls), 2),
            "max_drawdown": round(max_dd, 2),
            "profit_factor": round(gw / gl, 2) if gl > 0 else float("inf") if gw > 0 else 0.0,
            "avg_hold_days": round(sum(holds) / len(holds), 1) if holds else 0.0,
            "sharpe_ish": round(sharpe_ish, 2),
        }


def run_variant_backtest(
    variant: DNVariant,
    ticker: str,
    start_date,
    end_date,
    vix_bars: pd.DataFrame | None = None,
    vix3m_bars: pd.DataFrame | None = None,
) -> VariantResult:
    """Simulate the variant on historical bars for one ticker.

    Returns a VariantResult; caller aggregates across variants/tickers.
    """
    result = VariantResult(variant_name=variant.name, ticker=ticker)

    try:
        tf = fetch_multi_timeframe(ticker, base_interval="5m",
                                    start=start_date, end=end_date)
    except Exception as e:
        log.warning(f"[{variant.name}/{ticker}] fetch_bars failed: {e}")
        return result
    base = tf.get("base")
    if base is None or base.empty:
        return result

    # Realized-vol rolling series — cheap IVR proxy
    closes = base["close"].astype(float)
    rvol_proxy = closes.pct_change().rolling(60).std() * (252 * 78) ** 0.5
    sigma_default = float(rvol_proxy.dropna().median() or 0.20)
    sigma_default = max(0.08, min(1.0, sigma_default))

    # Trade-tracking state
    open_trade: Optional[dict] = None
    last_exit_idx: int = -1_000  # bar index of last exit
    cooldown_bars = 78 * 3         # 3 trading days between entries
    last_entry_day: Optional[date] = None   # one entry per day max
    stop_loss_multiple = 2.0       # close when we'd lose 2× the credit

    start_idx = min(60, len(base) - 1)

    for i in range(start_idx, len(base)):
        bar = base.iloc[i]
        ts: pd.Timestamp = base.index[i]
        current_price = float(bar["close"])
        bar_day = ts.date() if hasattr(ts, "date") else ts

        # ── Exit logic for open trade ────────────────────
        if open_trade is not None:
            # Reprice legs; decide exit
            elapsed_days = max((ts - open_trade["_entry_ts"]).total_seconds() / 86400.0, 0.01)
            remaining_dte = max(open_trade["_entry_dte"] - elapsed_days, 0.01)
            # Combined option P&L based on current prices
            net_now = _net_credit(open_trade["legs"], current_price,
                                   remaining_dte, sigma_default)
            entry_net = open_trade["_entry_net_credit"]
            # For a credit spread we want net_now to shrink (we close cheaper)
            pnl_fraction = (entry_net - net_now) / abs(entry_net) if entry_net != 0 else 0
            pnl_usd = (entry_net - net_now) * 100 * open_trade["contracts"]
            exit_reason = None

            # 1) Profit target
            if variant.profit_target_pct > 0 and pnl_fraction >= variant.profit_target_pct:
                exit_reason = "PROFIT_TARGET"
            # 2) Hard DTE exit
            elif variant.hard_exit_dte > 0 and remaining_dte <= variant.hard_exit_dte:
                exit_reason = f"HARD_DTE_{variant.hard_exit_dte}"
            # 3) EOD close
            elif variant.eod_close and _is_last_bar_of_day(base, i):
                exit_reason = "EOD"
            # 4) Hold-days cap
            elif elapsed_days >= variant.hold_days_max:
                exit_reason = "HOLD_MAX"
            # 5) Stop loss — lose stop_loss_multiple × the credit received
            elif pnl_fraction <= -stop_loss_multiple:
                exit_reason = "STOP_LOSS"

            if exit_reason:
                open_trade.update({
                    "exit_time": ts.to_pydatetime(),
                    "exit_reason": exit_reason,
                    "exit_result": "WIN" if pnl_usd > 0 else "LOSS" if pnl_usd < 0 else "SCRATCH",
                    "pnl_usd": pnl_usd,
                    "pnl_pct": pnl_fraction,
                    "hold_days": elapsed_days,
                })
                result.trades.append({k: v for k, v in open_trade.items() if not k.startswith("_")})
                last_exit_idx = i
                open_trade = None
                continue

        # ── Entry logic ──────────────────────────────────
        if open_trade is not None:
            continue
        if (i - last_exit_idx) < cooldown_bars:
            continue
        # One entry per calendar day max
        if last_entry_day is not None and bar_day == last_entry_day:
            continue

        # Filters
        if variant.event_blackout and _is_earnings_blackout(ticker, bar_day):
            continue
        if variant.regime_filter and not _vix_contango(vix_bars, vix3m_bars, ts):
            continue
        # IVR gate (using realized-vol percentile as proxy)
        current_rvol = float(rvol_proxy.iloc[i]) if not math.isnan(rvol_proxy.iloc[i]) else sigma_default
        ivr = _approx_ivr(rvol_proxy.dropna().iloc[:i+1], current_rvol)
        if variant.ivr_min > 0 and ivr < variant.ivr_min:
            continue

        # Build legs
        sigma = max(current_rvol, 0.08)
        dte = variant.target_dte
        # Strike selection
        if variant.strike_mode == "delta_targeted":
            sc_k = strike_by_delta(current_price, variant.short_delta, dte, sigma, "C", variant.strike_interval)
            lc_k = strike_by_delta(current_price, variant.long_delta, dte, sigma, "C", variant.strike_interval)
            sp_k = strike_by_delta(current_price, -variant.short_delta, dte, sigma, "P", variant.strike_interval)
            lp_k = strike_by_delta(current_price, -variant.long_delta, dte, sigma, "P", variant.strike_interval)
        else:
            atm = _round_to(current_price, variant.strike_interval)
            sc_k = atm
            lc_k = atm + variant.wing_width_dollars
            sp_k = atm
            lp_k = atm - variant.wing_width_dollars

        # Contract sizing
        contracts = variant.base_contracts
        if variant.sizing_mode == "ivr_bucketed":
            contracts = _ivr_bucket_size(ivr, variant.base_contracts)

        legs = [
            {"role": "short_call", "right": "C", "strike": sc_k, "direction": "SHORT"},
            {"role": "long_call", "right": "C", "strike": lc_k, "direction": "LONG"},
            {"role": "short_put", "right": "P", "strike": sp_k, "direction": "SHORT"},
            {"role": "long_put", "right": "P", "strike": lp_k, "direction": "LONG"},
        ]
        entry_net = _net_credit(legs, current_price, dte, sigma)
        # Only enter if it's a positive net credit
        if entry_net <= 0:
            continue

        open_trade = {
            "variant": variant.name, "ticker": ticker,
            "entry_time": ts.to_pydatetime(),
            "entry_underlying": current_price,
            "contracts": contracts,
            "legs": legs,
            "ivr_at_entry": round(ivr, 1),
            "sigma_at_entry": round(sigma, 3),
            "_entry_ts": ts,
            "_entry_dte": dte,
            "_entry_net_credit": entry_net,
        }
        last_entry_day = bar_day

    # Close any still-open trade at the last bar
    if open_trade is not None:
        ts = base.index[-1]
        current_price = float(base["close"].iloc[-1])
        elapsed_days = max((ts - open_trade["_entry_ts"]).total_seconds() / 86400.0, 0.01)
        remaining_dte = max(open_trade["_entry_dte"] - elapsed_days, 0.01)
        net_now = _net_credit(open_trade["legs"], current_price, remaining_dte, sigma_default)
        entry_net = open_trade["_entry_net_credit"]
        pnl_fraction = (entry_net - net_now) / abs(entry_net) if entry_net != 0 else 0
        pnl_usd = (entry_net - net_now) * 100 * open_trade["contracts"]
        open_trade.update({
            "exit_time": ts.to_pydatetime(),
            "exit_reason": "END_OF_RANGE",
            "exit_result": "WIN" if pnl_usd > 0 else "LOSS" if pnl_usd < 0 else "SCRATCH",
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_fraction,
            "hold_days": elapsed_days,
        })
        result.trades.append({k: v for k, v in open_trade.items() if not k.startswith("_")})
    return result


def _net_credit(legs: list[dict], underlying: float, dte_days: float,
                 sigma: float) -> float:
    """Per-share net credit of the 4-leg structure.
    Short legs add premium (+), long legs subtract (−)."""
    T = max(dte_days, 0.01) / 365.0
    net = 0.0
    for leg in legs:
        right = leg["right"]
        strike = float(leg["strike"])
        px = bs_price(underlying, strike, T, 0.04, sigma, right)
        if leg["direction"] == "SHORT":
            net += px
        else:
            net -= px
    return net


def _is_last_bar_of_day(bars: pd.DataFrame, i: int) -> bool:
    if i >= len(bars) - 1:
        return True
    return bars.index[i].date() != bars.index[i + 1].date()
