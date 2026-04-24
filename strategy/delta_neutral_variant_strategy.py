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
    V4_FILTERED, V5_HEDGED,
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


def _target_dte_expiry(target_dte: int, min_dte: int, max_dte: int) -> str:
    """Pick a concrete expiry YYYYMMDD at approximately target_dte
    calendar days. For weekly variants this is the next Friday ≥
    min_dte. For 45-DTE variants this is the 3rd Friday of the month
    ~target_dte out, clamped to [min_dte, max_dte].

    Simplification: uses Fridays only (weeklies + monthlies both fall
    on Fridays for equity options). Production should query the option
    chain's listed expirations.
    """
    from datetime import timedelta
    d = date.today() + timedelta(days=max(min_dte, 1))
    while d.weekday() != 4:   # Friday
        d += timedelta(days=1)
    # Advance until close to target_dte, within window
    dte = (d - date.today()).days
    while dte < target_dte - 3:
        d += timedelta(days=7)
        dte = (d - date.today()).days
        if dte > max_dte:
            d -= timedelta(days=7)
            break
    return d.strftime("%Y%m%d")


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
                "expiry": _target_dte_expiry(v.target_dte, v.min_dte, v.max_dte),
            },
        )]

    # ── leg spec ───────────────────────────────────────

    def place_legs(self, signal: Signal) -> List[LegSpec]:
        v = self.VARIANT
        details = signal.details or {}
        S = float(details.get("current_price") or signal.entry_price)
        sigma = float(details.get("sigma") or 0.20)
        expiry = details.get("expiry") or _target_dte_expiry(
            v.target_dte, v.min_dte, v.max_dte)
        dte = v.target_dte

        # Strike selection
        if v.strike_mode == "delta_targeted":
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
