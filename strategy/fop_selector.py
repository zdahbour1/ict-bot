"""Liquidity-aware FOP (Futures Options) contract selection.

Picks the most liquid listed contract on a futures-options chain that
matches the strategy's directional signal. Hard-rejects any contract
below configured liquidity gates — the scanner will then SKIP the
trade rather than enter on a thin contract.

See docs/fop_live_trading_design.md (approved 2026-04-22) and the
user's explicit direction: "Need improve the chance to exit trades
when needed" — selection favors quarterly > monthly > weekly, ATM
or slightly OTM, with OI / volume / bid-ask spread gates.

Pure helpers (expiry classification, strike rounding, rejection
filter) are separable from the IB API calls to keep unit tests
deterministic.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

import config
from broker.ib_contracts import FOP_SPECS

log = logging.getLogger(__name__)


# ─── Pure helpers (unit-testable, no IB calls) ─────────────

def classify_expiry(expiry: str, today: Optional[date] = None) -> str:
    """Return one of: 'quarterly' | 'monthly' | 'weekly' | 'daily' | 'past'.

    Convention (CME options on CME index futures):
      - Quarterly options expire the Thursday before the underlying
        quarterly future (T-1 to the 3rd-Friday of Mar/Jun/Sep/Dec).
        Example: Jun-2026 quarterly FOP = 20260618 (Thu), underlying
        future expires 20260619 (Fri).
      - Monthly options expire the 3rd Thursday of non-quarterly months.
      - Weekly options expire on Fridays that aren't 3rd-Fridays.
      - Daily options expire any weekday (Mon/Tue/Wed/Thu that isn't
        already classified above).

    expiry: 'YYYYMMDD' string as IB returns it.
    today: for deterministic tests — defaults to datetime.now().date().
    """
    today = today or datetime.now().date()
    try:
        d = datetime.strptime(expiry, "%Y%m%d").date()
    except ValueError:
        return "daily"   # unparseable — conservative bucket

    if d < today:
        return "past"

    # 3rd-Friday of the month (the quarterly/monthly underlying future
    # expiry). Options expire the Thursday before.
    first_day = d.replace(day=1)
    # Friday = weekday 4
    offset = (4 - first_day.weekday()) % 7
    third_friday = first_day + timedelta(days=offset + 14)
    third_thursday = third_friday - timedelta(days=1)

    if d == third_thursday:
        # quarterly if month is Mar/Jun/Sep/Dec, else monthly
        if d.month in (3, 6, 9, 12):
            return "quarterly"
        return "monthly"
    # Any other Friday → weekly. Mon-Thu (not 3rd-Thu) → daily.
    if d.weekday() == 4:
        return "weekly"
    return "daily"


def round_to_grid(price: float, interval: float) -> float:
    """Round price to the nearest strike interval."""
    if interval <= 0:
        return price
    return round(price / interval) * interval


def passes_liquidity_gate(
    quote: dict,
    *,
    min_open_interest: int,
    min_volume: int,
    max_spread_pct: float,
) -> tuple[bool, Optional[str]]:
    """Return (True, None) if quote passes all gates, else (False, reason).

    quote is a dict with keys: bid, ask, volume, open_interest.
    Any missing/zero value counts as a rejection (we don't guess on
    stale data).
    """
    oi = int(quote.get("open_interest") or 0)
    if oi < min_open_interest:
        return False, f"OI too low: {oi} < {min_open_interest}"

    vol = int(quote.get("volume") or 0)
    if vol < min_volume:
        return False, f"volume too low: {vol} < {min_volume}"

    bid = float(quote.get("bid") or 0)
    ask = float(quote.get("ask") or 0)
    if bid <= 0 or ask <= 0 or ask <= bid:
        return False, f"bad quote: bid={bid} ask={ask}"
    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid if mid > 0 else 1.0
    if spread_pct > max_spread_pct:
        return False, f"spread too wide: {spread_pct:.1%} > {max_spread_pct:.0%}"

    return True, None


def candidate_strikes(
    underlying_price: float, strike_interval: float, direction: str,
    *, depth: int = 5,
) -> list[float]:
    """Return a list of candidate strikes to probe, ordered by preference.

    ATM first, then ±1 interval, ±2, ..., up to ``depth`` rungs out.
    For LONG (call-buying), we slightly prefer OTM calls (strike > spot).
    For SHORT (put-buying), we slightly prefer OTM puts (strike < spot).
    Both directions include ATM + ITM candidates as fallback.
    """
    if strike_interval <= 0:
        return [underlying_price]
    atm = round_to_grid(underlying_price, strike_interval)
    candidates = [atm]
    for i in range(1, depth + 1):
        up = atm + i * strike_interval
        dn = atm - i * strike_interval
        if direction.upper() == "LONG":
            candidates += [up, dn]   # OTM call first (higher strike)
        else:
            candidates += [dn, up]   # OTM put first (lower strike)
    return candidates


def prefer_order() -> list[str]:
    """Parse config.FOP_EXPIRY_PREF into an ordered list."""
    raw = getattr(config, "FOP_EXPIRY_PREF",
                   "quarterly,monthly,weekly") or ""
    seen = set()
    out = []
    for p in raw.split(","):
        p = p.strip().lower()
        if p in ("quarterly", "monthly", "weekly", "daily") and p not in seen:
            out.append(p)
            seen.add(p)
    return out or ["quarterly", "monthly", "weekly"]


# ─── Selection result ─────────────────────────────────────

@dataclass
class FOPSelection:
    """The contract the selector picked, ready for place_bracket_order_fop."""
    symbol: str                 # underlying (MES, ES, NQ, ...)
    exchange: str               # CME, COMEX, NYMEX
    currency: str
    multiplier: int
    expiry: str                 # YYYYMMDD
    strike: float
    right: str                  # 'C' | 'P'
    con_id: Optional[int]       # populated after qualification
    expiry_type: str            # quarterly / monthly / weekly / daily
    # Snapshot at selection — logged to the trade for post-mortem analysis
    bid: float
    ask: float
    volume: int
    open_interest: int
    mid_price: float


# ─── Main selection (IB-touching) ─────────────────────────

def select_liquid_fop_contract(
    chain_probe,            # fn(underlying, exchange) -> list[dict{expiry, strike, right, ...}]
    quote_probe,            # fn(symbol, exchange, expiry, strike, right, multiplier) -> dict{bid,ask,volume,open_interest}
    *,
    underlying: str,
    direction: str,
    underlying_price: float,
    today: Optional[date] = None,
) -> Optional[FOPSelection]:
    """Pick the most-liquid FOP contract matching the signal.

    Arguments ``chain_probe`` and ``quote_probe`` are injected so this
    function is fully unit-testable without an IB connection. Live
    callers wire them to `IBClient.reqContractDetails` and
    `IBClient.reqMktData` equivalents.

    Returns FOPSelection on success, None if no contract passes the
    liquidity gates. On None, caller must NOT place any order.
    """
    spec = FOP_SPECS.get(underlying.upper())
    if spec is None:
        log.warning(f"[FOP] no FOP_SPECS entry for {underlying} — cannot select")
        return None

    exchange = spec["exchange"]
    multiplier = int(spec["multiplier"])
    strike_interval = float(spec["strike_interval"])
    currency = spec.get("currency", "USD")

    # Pull full chain from IB.
    try:
        raw_chain = chain_probe(underlying, exchange) or []
    except Exception as e:
        log.warning(f"[FOP] chain_probe failed for {underlying}: {e}")
        return None
    if not raw_chain:
        log.info(f"[FOP] {underlying}: empty chain")
        return None

    right = "C" if direction.upper() == "LONG" else "P"

    # Classify + filter expiries.
    max_dte = int(getattr(config, "FOP_MAX_DTE", 60))
    today_ = today or datetime.now().date()
    cutoff = today_ + timedelta(days=max_dte)

    expiries_by_type: dict[str, list[str]] = {
        "quarterly": [], "monthly": [], "weekly": [], "daily": [],
    }
    seen_exp: set[str] = set()
    for row in raw_chain:
        exp = str(row.get("expiry") or row.get("last_trade_date") or "")
        if not exp or exp in seen_exp:
            continue
        seen_exp.add(exp)
        try:
            d = datetime.strptime(exp, "%Y%m%d").date()
        except ValueError:
            continue
        if d < today_ or d > cutoff:
            continue
        bucket = classify_expiry(exp, today=today_)
        if bucket in expiries_by_type:
            expiries_by_type[bucket].append(exp)
    # Sort each bucket soonest-first
    for buck in expiries_by_type.values():
        buck.sort()

    # Liquidity gates
    min_oi = int(getattr(config, "FOP_MIN_OPEN_INTEREST", 500))
    min_vol = int(getattr(config, "FOP_MIN_VOLUME", 100))
    max_spread = float(getattr(config, "FOP_MAX_SPREAD_PCT", 0.15))

    strikes = candidate_strikes(underlying_price, strike_interval,
                                 direction, depth=5)

    # Walk the preference list: quarterly → monthly → weekly → daily.
    for expiry_type in prefer_order():
        for expiry in expiries_by_type.get(expiry_type, []):
            for strike in strikes:
                try:
                    quote = quote_probe(
                        underlying, exchange, expiry, strike, right, multiplier
                    ) or {}
                except Exception as e:
                    log.debug(f"[FOP] quote_probe failed {underlying} "
                              f"{expiry} {strike}{right}: {e}")
                    continue
                ok, reason = passes_liquidity_gate(
                    quote,
                    min_open_interest=min_oi, min_volume=min_vol,
                    max_spread_pct=max_spread,
                )
                if not ok:
                    log.info(f"[FOP] {underlying} {expiry} {strike}{right} "
                             f"rejected — {reason}")
                    continue
                # Winner.
                bid = float(quote.get("bid") or 0)
                ask = float(quote.get("ask") or 0)
                mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else 0
                log.info(f"[FOP] SELECTED {underlying} {expiry} {strike}{right} "
                         f"({expiry_type}): bid={bid:.2f} ask={ask:.2f} "
                         f"vol={quote.get('volume')} OI={quote.get('open_interest')} "
                         f"mid=${mid:.2f}")
                return FOPSelection(
                    symbol=underlying.upper(),
                    exchange=exchange,
                    currency=currency,
                    multiplier=multiplier,
                    expiry=expiry,
                    strike=float(strike),
                    right=right,
                    con_id=quote.get("con_id"),
                    expiry_type=expiry_type,
                    bid=bid, ask=ask,
                    volume=int(quote.get("volume") or 0),
                    open_interest=int(quote.get("open_interest") or 0),
                    mid_price=mid,
                )

    log.warning(f"[FOP] {underlying} {direction}: no contract passed "
                f"liquidity gates across {sum(len(v) for v in expiries_by_type.values())} "
                f"candidate expiries — SKIPPING trade")
    return None
