"""Human-readable trade correlation IDs — the thing stamped on
IB ``Order.orderRef`` and stored in ``trades.client_trade_id``.

Format: ``<TICKER>-<YYMMDD>-<NN>``

Examples:
  INTC-260421-01   → first INTC trade on 2026-04-21
  INTC-260421-02   → second INTC trade on 2026-04-21
  AAPL-260421-01   → first AAPL trade on 2026-04-21

Why this shape (not a UUID):
  • Self-describing at a glance: you see the ticker + day + ordinal.
  • Sorts chronologically as plain text.
  • Fits IB's orderRef comfortably (max 15 chars for NYSE-listed
    tickers with 5-char symbols).
  • Greppable in TWS ("Order Ref" column), bot.log, audit UI.

Uniqueness is by construction: ticker × date × per-ticker daily
counter. The counter is derived by counting today's same-ticker
rows in the DB and adding 1, then verified against the unique
index as a safety net — on the very rare race where two entries
fire simultaneously, the second insert would raise IntegrityError
and the caller must retry with counter+1.

See docs/ib_db_correlation.md for the design.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

import pytz

log = logging.getLogger(__name__)

PT = pytz.timezone("America/Los_Angeles")

# Parse a ref for display / tooling
_REF_RE = re.compile(r"^([A-Z]+)-(\d{6})-(\d{2,})$")


def generate_trade_ref(ticker: str, now: Optional[datetime] = None) -> str:
    """Build a fresh unique client_trade_id for an entry.

    Queries the DB for how many trades this ticker has opened today
    (PT-local date) and appends the next ordinal. If the DB is
    unreachable we fall back to a timestamp-seeded ref so entry
    flow never blocks on correlation.

    Parameters
    ----------
    ticker : str
        The underlying ticker, e.g. ``"INTC"``. Must be uppercase
        letters (validated by IB contract builders upstream).
    now : datetime | None
        Override current time — used by tests to pin a day.

    Returns
    -------
    str
        A string of form ``TICKER-YYMMDD-NN`` fitting in 15 chars
        for all equity option tickers. NN zero-padded to 2 digits;
        widens to 3+ digits if needed on very active days.
    """
    if now is None:
        now = datetime.now(PT)
    elif now.tzinfo is None:
        now = PT.localize(now)
    else:
        now = now.astimezone(PT)

    ticker = (ticker or "UNK").upper()
    date_part = now.strftime("%y%m%d")
    prefix = f"{ticker}-{date_part}-"

    next_n = _compute_next_ordinal(ticker, now, prefix)
    # Width auto-grows at 100. ``:02d`` renders 1..99 as "01".."99"
    # and 100+ without padding.
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

    Returns dict with ``ticker``, ``date`` (YYMMDD), and ``ordinal``
    as an int — or ``None`` if the string doesn't match the format.
    Useful for log formatting and the Trades-tab Audit modal.
    """
    if not ref:
        return None
    m = _REF_RE.match(ref)
    if not m:
        return None
    return {
        "ticker":  m.group(1),
        "date":    m.group(2),     # YYMMDD
        "ordinal": int(m.group(3)),
    }
