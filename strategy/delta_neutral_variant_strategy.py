"""Variant-aware Delta-Neutral strategy for live trading.

Reuses `DNVariant` config from `delta_neutral_variants.py` + the same
math as the backtest engine (`dn_variants_engine`) so live and
simulation behave identically.

Five concrete subclasses register themselves in StrategyRegistry so
the existing multi-strategy scanner/entry-manager plumbing handles
them without any engine changes:

  - DNVariantStrategyV1Baseline  → strategy name 'dn_v1'
  - DNVariantStrategyV2HoldDay   → strategy name 'dn_v2'
  - DNVariantStrategyV3PhaseB    → strategy name 'dn_v3'
  - DNVariantStrategyV4Filtered  → strategy name 'dn_v4'
  - DNVariantStrategyV5Hedged    → strategy name 'dn_v5'

For each signal tick, each variant evaluates its own filters + builds
its own leg spec. User ships all 5 live on paper tomorrow to validate
the backtest-predicted ranking.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import List, Optional

import pandas as pd

from strategy.base_strategy import BaseStrategy, LegSpec, Signal, StrategyRegistry
from strategy.delta_neutral_variants import (
    DNVariant, V1_BASELINE, V2_HOLD_DAY, V3_PHASEB,
    V4_FILTERED, V5_HEDGED, V5B_SWEEP_WINNER,
    ZDN_0DTE, ZDN_WEEKLY, ZDN_MONTHLY, ZDN_NEXT_MONTH,
)
from strategy.delta_neutral_strategy import (
    _next_expiry_yyyymmdd, _format_occ, _round_to_interval,
)

log = logging.getLogger(__name__)


# Backtest engine's helpers — reuse so live == sim
try:
    from backtest_engine.dn_variants_engine import (
        strike_by_delta as _strike_by_delta,
        _is_earnings_blackout,
    )
except Exception:
    # Fallback stubs if the backtest engine isn't importable at runtime
    def _strike_by_delta(underlying, td, dte, sigma, right, step):
        return _round_to_interval(underlying, step)
    def _is_earnings_blackout(*a, **kw):
        return False


def _third_friday(year: int, month: int) -> date:
    """Return the 3rd Friday of (year, month)."""
    from datetime import timedelta
    d = date(year, month, 1)
    # First Friday
    while d.weekday() != 4:
        d += timedelta(days=1)
    # Advance two more weeks
    return d + timedelta(days=14)


def _expiry_for_mode(mode: str, target_dte: int,
                      min_dte: int, max_dte: int) -> str:
    """Pick a concrete expiry YYYYMMDD for a given expiry_mode.

    Modes:
      'target_dte' (default) — Friday nearest target_dte in window.
      '0dte'       — today if weekday, else nearest weekly Friday.
      'weekly'     — next Friday.
      'monthly'    — 3rd Friday of current month (or next if already past).
      'next_month' — 3rd Friday of next calendar month.

    Simplification: uses Fridays only. Production should query the
    option chain's listed expirations.
    """
    from datetime import timedelta
    today = date.today()

    if mode == "0dte":
        # If today is a weekday, use today; else nearest coming Friday
        if today.weekday() < 5:
            return today.strftime("%Y%m%d")
        d = today
        while d.weekday() != 4:
            d += timedelta(days=1)
        return d.strftime("%Y%m%d")

    if mode == "weekly":
        d = today + timedelta(days=1)
        while d.weekday() != 4:
            d += timedelta(days=1)
        return d.strftime("%Y%m%d")

    if mode == "monthly":
        d = _third_friday(today.year, today.month)
        if d <= today:
            ny = today.year + (1 if today.month == 12 else 0)
            nm = 1 if today.month == 12 else today.month + 1
            d = _third_friday(ny, nm)
        return d.strftime("%Y%m%d")

    if mode == "next_month":
        ny = today.year + (1 if today.month == 12 else 0)
        nm = 1 if today.month == 12 else today.month + 1
        return _third_friday(ny, nm).strftime("%Y%m%d")

    # Default: target_dte window
    d = today + timedelta(days=max(min_dte, 1))
    while d.weekday() != 4:   # Friday
        d += timedelta(days=1)
    dte = (d - today).days
    while dte < target_dte - 3:
        d += timedelta(days=7)
        dte = (d - today).days
        if dte > max_dte:
            d -= timedelta(days=7)
            break
    return d.strftime("%Y%m%d")


def _target_dte_expiry(target_dte: int, min_dte: int, max_dte: int) -> str:
    """Backward-compatible wrapper."""
    return _expiry_for_mode("target_dte", target_dte, min_dte, max_dte)


def _is_before_entry_time_et(entry_time_et: str | None) -> bool:
    """True if the current wall-clock America/New_York time is earlier
    than HH:MM given by ``entry_time_et``. None/empty disables the
    gate (returns False)."""
    if not entry_time_et:
        return False
    try:
        hh, mm = entry_time_et.split(":")
        gate_h, gate_m = int(hh), int(mm)
    except Exception:
        return False
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = datetime.utcnow()  # fallback — approximate
    return (now.hour, now.minute) < (gate_h, gate_m)


class DNVariantStrategy(BaseStrategy):
    """Base class — subclasses inject a `DNVariant` config."""
    VARIANT: DNVariant = V1_BASELINE    # overridden

    @property
    def name(self) -> str:
        return self.VARIANT.name

    @property
    def description(self) -> str:
        return (f"Delta-Neutral variant {self.VARIANT.label} "
                f"({self.VARIANT.name}) — "
                f"DTE={self.VARIANT.target_dte}, "
                f"strike_mode={self.VARIANT.strike_mode}, "
                f"filters(ivr>={self.VARIANT.ivr_min},"
                f"regime={self.VARIANT.regime_filter},"
                f"blackout={self.VARIANT.event_blackout})")

    def __init__(self):
        self._seen_setups: set[str] = set()
        self.alerts_today: int = 0
        self._has_open_trade: bool = False

    def configure(self, settings: dict) -> None:
        """No per-variant config from settings yet; all behavior is
        baked into the DNVariant dataclass. Future: allow per-variant
        settings scoped by strategy_id."""
        pass

    def reset_daily(self) -> None:
        self._seen_setups.clear()
        self.alerts_today = 0

    def mark_used(self, setup_id: str) -> None:
        self._seen_setups.add(setup_id)

    # ── detection ──────────────────────────────────────

    def detect(self, bars_1m, bars_1h, bars_4h, levels, ticker):
        v = self.VARIANT
        if bars_1m is None or len(bars_1m) < 60:
            return []
        if self._has_open_trade:
            return []

        # Realized-vol proxy → IV sigma
        closes = bars_1m["close"].astype(float).tail(60)
        try:
            rvol = float(closes.pct_change().dropna().std()
                          * (252 * 390) ** 0.5)
        except Exception:
            return []
        sigma = max(rvol, 0.08)

        # Entry filter: earnings / FOMC blackout
        if v.event_blackout and _is_earnings_blackout(ticker, date.today()):
            return []

        # Entry time gate (e.g., ZDN waits until 10:00 ET for the
        # morning chop to settle before opening an ATM butterfly).
        if _is_before_entry_time_et(v.entry_time_et):
            return []

        # IVR filter (using realized-vol-quantile proxy)
        if v.ivr_min > 0:
            rvol_series = (closes.pct_change().rolling(20).std().dropna()
                           * (252 * 390) ** 0.5)
            if len(rvol_series) >= 10:
                cur = float(rvol_series.iloc[-1])
                lo = float(rvol_series.min())
                hi = float(rvol_series.max())
                ivr = 100 * (cur - lo) / (hi - lo) if hi > lo else 50.0
                if ivr < v.ivr_min:
                    return []

        # Regime filter (VIX/VIX3M): optional, delegate to live client.
        # In live the ib_singleton can be queried; skip if unavailable.
        if v.regime_filter:
            try:
                from broker.ib_singleton import get_client
                client = get_client()
                if client is not None:
                    vix = client.get_realtime_equity_price("VIX")
                    vix3m = client.get_realtime_equity_price("VIX3M")
                    if vix and vix3m and vix3m > 0 and (vix / vix3m) >= 1.0:
                        return []
            except Exception:
                pass

        current_price = float(closes.iloc[-1])
        setup_id = f"{v.name}-{ticker}-{bars_1m.index[-1].date()}"
        if setup_id in self._seen_setups:
            return []

        self.alerts_today += 1
        return [Signal(
            signal_type=f"DELTA_NEUTRAL_{v.label}",
            direction="LONG",
            entry_price=current_price,
            sl=0.0, tp=0.0,
            setup_id=setup_id,
            ticker=ticker,
            strategy_name=self.name,
            confidence=0.7,
            details={
                "variant": v.name,
                "label": v.label,
                "current_price": current_price,
                "sigma": sigma,
                "target_dte": v.target_dte,
                "expiry": _expiry_for_mode(
                    v.expiry_mode, v.target_dte, v.min_dte, v.max_dte),
            },
        )]

    # ── leg spec ───────────────────────────────────────

    def place_legs(self, signal: Signal) -> List[LegSpec]:
        v = self.VARIANT
        details = signal.details or {}
        S = float(details.get("current_price") or signal.entry_price)
        sigma = float(details.get("sigma") or 0.20)
        expiry = details.get("expiry") or _expiry_for_mode(
            v.expiry_mode, v.target_dte, v.min_dte, v.max_dte)
        dte = v.target_dte

        # Strike selection
        if v.structure == "iron_butterfly":
            # Both short legs share the ATM strike (classic butterfly).
            atm = _round_to_interval(S, v.strike_interval)
            sc = atm
            sp = atm
            lc = atm + v.wing_width_dollars
            lp = atm - v.wing_width_dollars
        elif v.strike_mode == "delta_targeted":
            sc = _strike_by_delta(S, v.short_delta, dte, sigma, "C",
                                   v.strike_interval)
            lc = _strike_by_delta(S, v.long_delta, dte, sigma, "C",
                                   v.strike_interval)
            sp = _strike_by_delta(S, -v.short_delta, dte, sigma, "P",
                                   v.strike_interval)
            lp = _strike_by_delta(S, -v.long_delta, dte, sigma, "P",
                                   v.strike_interval)
        else:
            atm = _round_to_interval(S, v.strike_interval)
            sc = atm
            lc = atm + v.wing_width_dollars
            sp = atm
            lp = atm - v.wing_width_dollars

        # Sizing
        contracts = v.base_contracts
        if v.sizing_mode == "ivr_bucketed":
            # IVR not re-computed here; rely on signal.details when
            # present, else base
            ivr = float(details.get("ivr") or 50.0)
            if ivr >= 70:
                contracts = v.base_contracts * 3
            elif ivr >= 50:
                contracts = v.base_contracts * 2
            else:
                contracts = v.base_contracts

        def _leg(role, right, strike, direction):
            return LegSpec(
                sec_type="OPT",
                symbol=_format_occ(signal.ticker, expiry, right, strike),
                direction=direction, contracts=contracts,
                strike=float(strike), right=right, expiry=expiry,
                multiplier=100, exchange="SMART", currency="USD",
                leg_role=role, underlying=signal.ticker,
            )
        return [
            _leg("short_call", "C", sc, "SHORT"),
            _leg("long_call",  "C", lc, "LONG"),
            _leg("short_put",  "P", sp, "SHORT"),
            _leg("long_put",   "P", lp, "LONG"),
        ]


# ── Five registered subclasses ───────────────────────────

@StrategyRegistry.register
class DNVariantStrategyV1Baseline(DNVariantStrategy):
    VARIANT = V1_BASELINE


@StrategyRegistry.register
class DNVariantStrategyV2HoldDay(DNVariantStrategy):
    VARIANT = V2_HOLD_DAY


@StrategyRegistry.register
class DNVariantStrategyV3PhaseB(DNVariantStrategy):
    VARIANT = V3_PHASEB


@StrategyRegistry.register
class DNVariantStrategyV4Filtered(DNVariantStrategy):
    VARIANT = V4_FILTERED


@StrategyRegistry.register
class DNVariantStrategyV5Hedged(DNVariantStrategy):
    VARIANT = V5_HEDGED


@StrategyRegistry.register
class DNVariantStrategyV5bSweepWinner(DNVariantStrategy):
    """ENH-061 — sweep-optimized configuration from 2026-04-23
    603-combo scan. Ships alongside V5 canonical for live A/B."""
    VARIANT = V5B_SWEEP_WINNER


# ── ZDN (zero-delta-neutral) gamma-scalping family 2026-04-24 ──
#
# All four ZDN variants share iron-butterfly structure + tight ±10-share
# delta hedge band + 10:00 ET entry gate + 3:45 ET exit gate + 25% SL.
# They differ only in expiry: 0DTE, weekly, monthly, next-month.

@StrategyRegistry.register
class DNVariantStrategyZDN0DTE(DNVariantStrategy):
    """ZDN 0-DTE: same-day expiry iron butterfly; max theta, max gamma."""
    VARIANT = ZDN_0DTE


@StrategyRegistry.register
class DNVariantStrategyZDNWeekly(DNVariantStrategy):
    """ZDN weekly: next-Friday expiry iron butterfly."""
    VARIANT = ZDN_WEEKLY


@StrategyRegistry.register
class DNVariantStrategyZDNMonthly(DNVariantStrategy):
    """ZDN monthly: 3rd-Friday-this-month iron butterfly."""
    VARIANT = ZDN_MONTHLY


@StrategyRegistry.register
class DNVariantStrategyZDNNextMonth(DNVariantStrategy):
    """ZDN next-month: 3rd-Friday-next-month iron butterfly."""
    VARIANT = ZDN_NEXT_MONTH
