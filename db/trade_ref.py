"""Human-readable trade correlation IDs — the thing stamped on
IB ``Order.orderRef`` and stored in ``trades.client_trade_id``.

Format: ``<strategy>-<TICKER>-<YYMMDD>-<NN>``  (with strategy prefix)
    or: ``<TICKER>-<YYMMDD>-<NN>``              (legacy, no strategy)

Examples:
  ict-SPY-260421-01       → first ICT SPY trade on 2026-04-21
  ict-SPY-260421-02       → second ICT SPY trade on 2026-04-21
  orb-AAPL-260421-01      → first ORB AAPL trade on 2026-04-21
  vwap_revert-NVDA-260421-05 → fifth VWAP NVDA trade on 2026-04-21

The strategy prefix makes it unambiguous which strategy owns a ref
when multiple run concurrently. See docs/ib_db_correlation.md.

Why this shape (not a UUID):
  • Self-describing at a glance: strategy + ticker + day + ordinal.
  • Sorts by strategy, then chronologically as plain text.
  • Fits IB's orderRef comfortably (max ~40 chars with longest
    strategy name + 5-char ticker + 4-digit ordinal).
  • Greppable in TWS ("Order Ref" column), bot.log, audit UI.

Uniqueness is by construction: strategy × ticker × date × daily
counter (per strategy+ticker). The counter is derived by MAX()
across existing same-prefix rows in the DB. On the very rare race
where two entries fire simultaneously, the unique index catches
the collision and the caller must retry with counter+1.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

import pytz

log = logging.getLogger(__name__)

PT = pytz.timezone("America/Los_Angeles")

# Parse a ref for display / tooling. Captures optional strategy prefix,
# ticker, date, ordinal. Strategy name allowed chars: lowercase letters,
# digits, underscore (matches strategies.name pattern).
_REF_RE = re.compile(
    r"^(?:([a-z][a-z0-9_]{0,19})-)?([A-Z]+)-(\d{6})-(\d{2,})$"
)


def generate_trade_ref(
    ticker: str,
    now: Optional[datetime] = None,
    strategy_name: Optional[str] = None,
) -> str:
    """Build a fresh unique client_trade_id for an entry.

    Queries the DB for how many trades this strategy+ticker has opened
    today (PT-local date) and appends the next ordinal. If the DB is
    unreachable we fall back to a timestamp-seeded ref so entry
    flow never blocks on correlation.

    Parameters
    ----------
    ticker : str
        The underlying ticker, e.g. ``"INTC"``. Uppercased for output.
    now : datetime | None
        Override current time — used by tests to pin a day.
    strategy_name : str | None
        Short name of the strategy producing this trade
        (e.g. ``'ict'``, ``'orb'``, ``'vwap_revert'``). If omitted,
        the active strategy is resolved from the DB (settings table
        ACTIVE_STRATEGY, falling back to is_default=True). If that
        also fails, no prefix is emitted (legacy format).

    Returns
    -------
    str
        ``<strategy>-TICKER-YYMMDD-NN`` if strategy resolvable, else
        the legacy ``TICKER-YYMMDD-NN``. NN zero-padded to 2 digits;
        widens to 3+ digits on very active days.
    """
    if now is None:
        now = datetime.now(PT)
    elif now.tzinfo is None:
        now = PT.localize(now)
    else:
        now = now.astimezone(PT)

    ticker = (ticker or "UNK").upper()

    # Resolve strategy name if not supplied. Best-effort: never blocks
    # ref generation.
    if strategy_name is None:
        try:
            from db.strategy_writer import get_active_strategy_name
            strategy_name = get_active_strategy_name()
        except Exception as e:
            log.debug(f"[trade_ref] could not resolve strategy: {e}")
            strategy_name = None

    date_part = now.strftime("%y%m%d")
    if strategy_name:
        prefix = f"{strategy_name}-{ticker}-{date_part}-"
    else:
        prefix = f"{ticker}-{date_part}-"

    next_n = _compute_next_ordinal(ticker, now, prefix)
    width = max(2, len(str(next_n)))
    return f"{prefix}{next_n:0{width}d}"


def _compute_next_ordinal(ticker: str, now: datetime, prefix: str) -> int:
    """Find the next daily ordinal for this ticker.

    Strategy: MAX(suffix) from existing rows matching the prefix
    today; this handles retries + gaps + clock skew better than
    COUNT(*). Returns 1 if there are no prior entries.
    """
    try:
        from db.connection import get_session
        from sqlalchemy import text
        session = get_session()
        if session is None:
            return _fallback_ordinal(now)
        # Extract numeric suffix after the prefix; MAX → next is +1.
        row = session.execute(
            text(
                "SELECT COALESCE(MAX("
                "  CAST(substring(client_trade_id FROM :patt) AS INTEGER)"
                "), 0) AS max_n "
                "FROM trades "
                "WHERE client_trade_id LIKE :like_prefix"
            ),
            {
                "patt": f"^{re.escape(prefix)}(\\d+)$",
                "like_prefix": prefix + "%",
            },
        ).fetchone()
        session.close()
        if row and row[0] is not None:
            return int(row[0]) + 1
    except Exception as e:
        log.warning(f"[trade_ref] DB lookup failed for {ticker}: {e} — using fallback")
    return _fallback_ordinal(now)


def _fallback_ordinal(now: datetime) -> int:
    """When the DB is unreachable, fall back to a timestamp-based
    ordinal so we never block on correlation. Uses minute-of-day
    (0..1439) + current second so two fallback refs on the same
    second won't collide 99.9% of the time. The unique index
    catches anything that slips through."""
    return (now.hour * 60 + now.minute) * 60 + now.second


def parse_trade_ref(ref: str) -> Optional[dict]:
    """Extract structured info from a client_trade_id.

    Returns dict with ``strategy`` (None if legacy format), ``ticker``,
    ``date`` (YYMMDD), and ``ordinal`` as an int — or ``None`` if the
    string doesn't match the format. Useful for log formatting and the
    Trades-tab details modal.
    """
    if not ref:
        return None
    m = _REF_RE.match(ref)
    if not m:
        return None
    return {
        "strategy": m.group(1),       # None if legacy format
        "ticker":   m.group(2),
        "date":     m.group(3),       # YYMMDD
        "ordinal":  int(m.group(4)),
    }
