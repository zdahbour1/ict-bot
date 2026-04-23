"""
Delta-Neutral strategy plugin — Phase 6 skeleton (multi-strategy v2).

A minimal, multi-leg example consumer of the ``place_legs`` plugin hook.
When ``detect()`` fires, ``place_legs()`` returns a 4-leg iron condor:

    short ATM call  / long OTM call   (higher strike)
    short ATM put   / long OTM put    (lower strike)

All legs share the same underlying + expiry. This class is intentionally
simple — the real detection logic lands in a follow-up. The point of this
file today is to exercise the multi-leg execution path end-to-end so
Phase 6 has a consumer from day one.

See docs/delta_neutral_strategy.md and
docs/multi_strategy_architecture_v2.md §§2, 6, 7.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

import pandas as pd

from strategy.base_strategy import BaseStrategy, LegSpec, Signal, StrategyRegistry

log = logging.getLogger(__name__)


def _round_to_interval(x: float, interval: float) -> float:
    """Round x to the nearest multiple of interval."""
    if interval <= 0:
        return float(x)
    return round(x / interval) * interval


def _next_expiry_yyyymmdd(today=None) -> str:
    """Return a sane default expiry: the next Friday at least 1 day out.

    Why Friday: equity options universally list standard weekly expiries
    on Fridays (3rd Friday = monthly). Using today's date fails IB
    contract qualification on any non-Friday, which was causing every
    delta-neutral entry to die with 'Error 200: No security definition'.
    """
    from datetime import date, timedelta
    d = today or date.today()
    # Advance at least 1 day so we never pick a same-day expiry (some
    # tickers have 0DTE, many don't — Friday is the safe bet).
    d = d + timedelta(days=1)
    # Friday = weekday 4. Move forward until we hit one.
    while d.weekday() != 4:
        d = d + timedelta(days=1)
    return d.strftime("%Y%m%d")


def _format_occ(underlying: str, expiry_yyyymmdd: str, right: str,
                strike: float) -> str:
    """Build an OCC option symbol: TICKER + YYMMDD + C/P + strike*1000 (8 digits).

    Example: SPY260501C00500000 = SPY May 1 2026 Call strike 500.
    """
    # Expiry YYYYMMDD -> YYMMDD
    expiry6 = expiry_yyyymmdd[2:] if len(expiry_yyyymmdd) == 8 else expiry_yyyymmdd
    strike_int = int(round(float(strike) * 1000))
    return f"{underlying}{expiry6}{right.upper()}{strike_int:08d}"


@StrategyRegistry.register
class DeltaNeutralStrategy(BaseStrategy):
    """Skeleton delta-neutral / iron-condor strategy.

    Entry signal: "elevated IV + no open trade" — rudimentary starter logic,
    exercises the multi-leg execution path. Real IV-gate work is a follow-up.
    """

    @property
    def name(self) -> str:
        return "delta_neutral"

    @property
    def description(self) -> str:
        return ("Delta-neutral iron condor — sells ATM straddle, buys OTM "
                "wings. Skeleton (Phase 6).")

    def __init__(
        self,
        strike_interval: float = 5.0,   # equities default
        wing_width: float = 10.0,       # points from ATM to long wing
        contracts: int = 1,
        iv_threshold: float = 0.25,     # "elevated" IV cutoff
        default_expiry: Optional[str] = None,  # YYYYMMDD, None=today
    ):
        self.strike_interval = float(strike_interval)
        self.wing_width = float(wing_width)
        self.contracts = int(contracts)
        self.iv_threshold = float(iv_threshold)
        self.default_expiry = default_expiry
        self._seen_setups: set[str] = set()
        self.alerts_today: int = 0
        self._has_open_trade: bool = False  # caller may set

    # ── Configuration ─────────────────────────────────────────
    def configure(self, settings: dict) -> None:
        if not settings:
            return
        if "DELTA_NEUTRAL_STRIKE_INTERVAL" in settings:
            self.strike_interval = float(settings["DELTA_NEUTRAL_STRIKE_INTERVAL"])
        if "DELTA_NEUTRAL_WING_WIDTH" in settings:
            self.wing_width = float(settings["DELTA_NEUTRAL_WING_WIDTH"])
        if "DELTA_NEUTRAL_CONTRACTS" in settings:
            self.contracts = int(settings["DELTA_NEUTRAL_CONTRACTS"])
        if "DELTA_NEUTRAL_IV_THRESHOLD" in settings:
            self.iv_threshold = float(settings["DELTA_NEUTRAL_IV_THRESHOLD"])

    def reset_daily(self) -> None:
        self._seen_setups.clear()
        self.alerts_today = 0

    def mark_used(self, setup_id: str) -> None:
        self._seen_setups.add(setup_id)

    # ── Detection (starter logic) ─────────────────────────────
    def detect(
        self,
        bars_1m: pd.DataFrame,
        bars_1h: pd.DataFrame,
        bars_4h: pd.DataFrame,
        levels: list,
        ticker: str,
    ) -> List[Signal]:
        """Rudimentary: fire when recent-bar IV proxy is 'elevated' and we
        have no open trade on this ticker. Real IV gate is a follow-up.
        """
        if bars_1m is None or len(bars_1m) < 10:
            return []
        if self._has_open_trade:
            return []

        # ENH-035 — production IV: compute implied vol from ATM option
        # chain bid/ask midpoints when available, fall back to the
        # legacy rolling-std pct-change proxy when we don't have an
        # option client to quote. ATM IV tracks real vol much more
        # closely than historical realized vol so the entry gate is
        # meaningful rather than "volatile stock" = elevated.
        iv_proxy = 0.0
        iv_source = "proxy"
        try:
            iv_proxy = self._compute_atm_iv(bars_1m, ticker)
            if iv_proxy > 0:
                iv_source = "bs_implied"
        except Exception:
            pass
        if iv_proxy <= 0:
            try:
                closes = bars_1m["close"].astype(float).tail(60)
                iv_proxy = float(closes.pct_change().dropna().std()
                                  * (252 * 390) ** 0.5)
            except Exception:
                return []
        if iv_proxy < self.iv_threshold:
            return []

        last = bars_1m.iloc[-1]
        try:
            current_price = float(last["close"])
        except Exception:
            return []

        setup_id = f"dn-{ticker}-{bars_1m.index[-1].date()}"
        if setup_id in self._seen_setups:
            return []

        sig = Signal(
            signal_type="DELTA_NEUTRAL_CONDOR",
            direction="LONG",  # logical — the deal is net credit, but the
                                # BaseStrategy direction field is required
            entry_price=current_price,
            sl=0.0,
            tp=0.0,
            setup_id=setup_id,
            ticker=ticker,
            strategy_name=self.name,
            confidence=min(1.0, iv_proxy / max(self.iv_threshold, 1e-6)),
            details={
                "iv_proxy": iv_proxy,
                "iv_source": iv_source,
                "current_price": current_price,
                "strike_interval": self.strike_interval,
                "wing_width": self.wing_width,
                "expiry": self.default_expiry or _next_expiry_yyyymmdd(),
            },
        )
        self.alerts_today += 1
        return [sig]

    def _compute_atm_iv(self, bars_1m, ticker: str) -> float:
        """ENH-035 — compute ATM implied vol from live option quotes.

        Tries two strategies before giving up (returns 0.0 on every
        failure path so caller falls back to the rolling-std proxy):

        1. If an IB client is reachable at module scope, fetch the
           ATM call & put mid prices, back out Black-Scholes implied
           vol from each, average.
        2. Otherwise 0.0.

        Keeps the strategy pure-functional by doing lazy imports —
        the IB client gets plumbed in via the broker singleton only
        if the deployment has one, otherwise we stay in backtest-safe
        mode.
        """
        try:
            from backtest_engine.option_pricer import implied_vol
        except Exception:
            return 0.0
        try:
            from broker.ib_singleton import get_client
            client = get_client()
        except Exception:
            return 0.0
        if client is None:
            return 0.0
        try:
            current = float(bars_1m["close"].iloc[-1])
        except Exception:
            return 0.0
        from datetime import date, datetime, timedelta, timezone
        # Pick next Friday ≥ 1 DTE as the expiry we quote against.
        expiry_str = self.default_expiry or _next_expiry_yyyymmdd()
        try:
            exp_date = datetime.strptime(expiry_str, "%Y%m%d").date()
            dte = max((exp_date - date.today()).days, 1)
            T = dte / 365.0
        except Exception:
            return 0.0
        atm = _round_to_interval(current, self.strike_interval)
        ivs = []
        for right in ("C", "P"):
            try:
                sym = _format_occ(ticker, expiry_str, right, atm)
                px = float(client.get_option_price(sym))
                if px <= 0:
                    continue
                iv = implied_vol(px, current, atm, T, r=0.04, right=right)
                if iv and 0.02 < iv < 3.0:   # sanity-bound
                    ivs.append(iv)
            except Exception:
                continue
        return float(sum(ivs) / len(ivs)) if ivs else 0.0

    # ── Multi-leg execution spec (Phase 6) ────────────────────
    def place_legs(self, signal: Signal) -> List[LegSpec]:
        """Four-leg iron condor around ATM of ``signal.entry_price``.

            leg 0: short_call  — SHORT  ATM call
            leg 1: long_call   — LONG   (ATM + wing_width) call
            leg 2: short_put   — SHORT  ATM put
            leg 3: long_put    — LONG   (ATM - wing_width) put
        """
        details = signal.details or {}
        current_price = float(details.get("current_price") or signal.entry_price)
        strike_interval = float(details.get("strike_interval") or self.strike_interval)
        wing_width = float(details.get("wing_width") or self.wing_width)
        expiry = details.get("expiry") or (self.default_expiry
                                           or _next_expiry_yyyymmdd())

        atm = _round_to_interval(current_price, strike_interval)
        long_call_strike = atm + wing_width
        long_put_strike = atm - wing_width
        underlying = signal.ticker

        def _leg(role, right, strike, direction):
            return LegSpec(
                sec_type="OPT",
                symbol=_format_occ(underlying, expiry, right, strike),
                direction=direction,
                contracts=self.contracts,
                strike=float(strike),
                right=right,
                expiry=expiry,
                multiplier=100,
                exchange="SMART",
                currency="USD",
                leg_role=role,
                underlying=underlying,
            )

        return [
            _leg("short_call", "C", atm,              "SHORT"),
            _leg("long_call",  "C", long_call_strike, "LONG"),
            _leg("short_put",  "P", atm,              "SHORT"),
            _leg("long_put",   "P", long_put_strike,  "LONG"),
        ]
