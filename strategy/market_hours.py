"""Market-hours gates for exit + entry flow.

Three concepts:

1. **Hard cutoff** (``EOD_HARD_CUTOFF_PT``, default 13:00 PT = US equity
   options market close):  after this time, the exit manager does
   nothing — no close attempts, no retries, no new MKT SELLs parked
   on IB. This eliminates the ``verify_close_fail`` retry-storm that
   piles up orders after market close (observed 2026-04-20 afternoon).

2. **EOD sweep window**  [hard_cutoff - ``EOD_CLOSE_LEAD_MINUTES``,
   hard_cutoff):  during this window the exit manager force-closes
   every open trade with ``exit_reason='EOD'``. The lead minutes
   buffer ensures MKT SELLs fill while the market is still open.
   Default lead = 5 minutes, so closes fire at 12:55 PT.

3. **Entry cutoff**:  scanners stop accepting new entries at
   ``hard_cutoff - EOD_CLOSE_LEAD_MINUTES``  (same time as the EOD
   sweep starts).  Prevents placing a bracket at 12:58 PT that the
   EOD sweep would immediately tear down.

All times in Pacific Time (the bot's canonical timezone).
Configuration is via the ``settings`` table; this module reads and
caches values for the lifetime of a single module-level
``get_market_clock()`` call.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from typing import Optional

import pytz

PT = pytz.timezone("America/Los_Angeles")


# ── Defaults (also written to the settings table by the seed step) ──
DEFAULT_EOD_HARD_CUTOFF_HOUR = 13        # 13:00 PT = options close
DEFAULT_EOD_HARD_CUTOFF_MIN  = 0
DEFAULT_EOD_LEAD_MINUTES     = 5         # sweep starts 5 min before close
DEFAULT_TRADE_WINDOW_START_HOUR = 6
DEFAULT_TRADE_WINDOW_START_MIN  = 30


@dataclass(frozen=True)
class MarketClock:
    """Snapshot of market-window configuration + current time.

    Build one per decision (cheap — it's just a settings read + a
    ``datetime.now``). Treating it as immutable makes the
    ``is_open/past_close/in_eod_window/entry_allowed`` methods pure
    functions of the snapshot.
    """
    now_pt:               datetime
    hard_cutoff_pt:       datetime   # today's market close moment
    eod_window_start_pt:  datetime   # hard_cutoff - lead_minutes
    trade_window_start_pt: datetime

    # Convenience derivations ------------------------------------
    def is_past_close(self) -> bool:
        """True if we're at or after the hard cutoff. Exit manager
        uses this to short-circuit its retry loop — no orders will
        fill after close; attempting to send more is just spam."""
        return self.now_pt >= self.hard_cutoff_pt

    def in_eod_sweep_window(self) -> bool:
        """True if we're in the last N minutes before close.
        Exit manager uses this to force-close every open trade with
        reason='EOD'. Ends when hard cutoff hits."""
        return (self.eod_window_start_pt <= self.now_pt < self.hard_cutoff_pt)

    def entries_allowed(self) -> bool:
        """True if a new entry can still be placed.

        Two gates:
          1. Must be AFTER the daily start window (no pre-market entries).
          2. Must be BEFORE the EOD sweep window (so a fresh bracket
             doesn't get torn down 30 seconds later).
        """
        return (self.trade_window_start_pt <= self.now_pt
                < self.eod_window_start_pt)

    def minutes_until_eod_sweep(self) -> float:
        delta = (self.eod_window_start_pt - self.now_pt).total_seconds() / 60.0
        return max(0.0, delta)

    def minutes_until_close(self) -> float:
        delta = (self.hard_cutoff_pt - self.now_pt).total_seconds() / 60.0
        return max(0.0, delta)


def _load_int_setting(key: str, default: int) -> int:
    """Read an int from settings table, tolerating any error."""
    try:
        from db.connection import get_session
        from sqlalchemy import text
        session = get_session()
        if session is None:
            return default
        row = session.execute(
            text("SELECT value FROM settings "
                 "WHERE key=:k AND strategy_id IS NULL"),
            {"k": key},
        ).fetchone()
        session.close()
        if row and row[0] is not None:
            try:
                return int(row[0])
            except (ValueError, TypeError):
                return default
    except Exception:
        pass
    return default


def get_market_clock(now: Optional[datetime] = None) -> MarketClock:
    """Build a fresh ``MarketClock`` for right now (or for a supplied
    ``now`` — useful for tests)."""
    if now is None:
        now_pt = datetime.now(PT)
    elif now.tzinfo is None:
        now_pt = PT.localize(now)
    else:
        now_pt = now.astimezone(PT)

    hard_hour = _load_int_setting(
        "EOD_HARD_CUTOFF_HOUR_PT", DEFAULT_EOD_HARD_CUTOFF_HOUR
    )
    hard_min = _load_int_setting(
        "EOD_HARD_CUTOFF_MINUTE_PT", DEFAULT_EOD_HARD_CUTOFF_MIN
    )
    lead_min = _load_int_setting(
        "EOD_CLOSE_LEAD_MINUTES", DEFAULT_EOD_LEAD_MINUTES
    )
    start_hour = _load_int_setting(
        "TRADE_WINDOW_START_PT", DEFAULT_TRADE_WINDOW_START_HOUR
    )
    start_min = _load_int_setting(
        "TRADE_WINDOW_START_MIN", DEFAULT_TRADE_WINDOW_START_MIN
    )

    today = now_pt.date()
    hard_cutoff_pt = PT.localize(
        datetime.combine(today, dtime(hour=hard_hour, minute=hard_min))
    )
    eod_window_start_pt = hard_cutoff_pt - timedelta(minutes=lead_min)
    trade_window_start_pt = PT.localize(
        datetime.combine(today, dtime(hour=start_hour, minute=start_min))
    )

    return MarketClock(
        now_pt=now_pt,
        hard_cutoff_pt=hard_cutoff_pt,
        eod_window_start_pt=eod_window_start_pt,
        trade_window_start_pt=trade_window_start_pt,
    )
