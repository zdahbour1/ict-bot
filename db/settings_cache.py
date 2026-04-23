"""Live, short-TTL cache of the ``settings`` table.

Replaces hard-coded ``config.X`` reads for bot-behavior flags the user
wants to toggle at runtime without editing code. Examples:

    from db.settings_cache import get_bool
    if get_bool("USE_COMBO_ORDERS_FOR_MULTI_LEG", default=False):
        ...

Cache TTL is intentionally short (5 seconds) so dashboard changes land
in the bot's next scan cycle without needing a restart, while still
cheap-ing out 1000s of reads per second when many scanners ask the
same question.

Globals (``strategy_id IS NULL``) and per-strategy overrides are both
supported via ``strategy_id=`` — the caller passes the strategy ID
when asking for a strategy-scoped knob.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

_TTL_SEC = 5.0
_CACHE: dict[tuple[str, Optional[int]], tuple[float, Optional[str]]] = {}
_LOCK = threading.Lock()


def _fetch_raw(key: str, strategy_id: Optional[int]) -> Optional[str]:
    """One DB hit — looks up settings.value by (key, strategy_id).

    Resolution order for a bot-wide/global caller (strategy_id=None):
      1. Global row (strategy_id IS NULL)
      2. ANY strategy-scoped row with that key — picks the lowest
         strategy_id for determinism.

    The fallback at step 2 means a caller that doesn't know the
    strategy_id can still read a key that exists only at strategy
    scope (e.g. DN_* flags seeded at strategy_id=91). Without it
    those keys were invisible to bot-wide consumers and defaulted
    silently, which masked the delta-hedger's enable flag being on.

    For strategy-scoped callers (strategy_id given), the standard
    prefer-scoped-then-global order applies.
    """
    try:
        from db.connection import get_session
        from sqlalchemy import text
        session = get_session()
        if session is None:
            return None
        try:
            if strategy_id is None:
                row = session.execute(text(
                    "SELECT value FROM settings "
                    "WHERE key=:k AND strategy_id IS NULL"
                ), {"k": key}).fetchone()
                if row is None:
                    # Fall back to any strategy-scoped row if the key
                    # only exists under a specific strategy.
                    row = session.execute(text(
                        "SELECT value FROM settings "
                        "WHERE key=:k AND strategy_id IS NOT NULL "
                        "ORDER BY strategy_id LIMIT 1"
                    ), {"k": key}).fetchone()
            else:
                # Prefer strategy-scoped; fall back to global.
                row = session.execute(text(
                    "SELECT value FROM settings "
                    "WHERE key=:k AND strategy_id=:sid"
                ), {"k": key, "sid": strategy_id}).fetchone()
                if row is None:
                    row = session.execute(text(
                        "SELECT value FROM settings "
                        "WHERE key=:k AND strategy_id IS NULL"
                    ), {"k": key}).fetchone()
        finally:
            session.close()
        return row[0] if row else None
    except Exception as e:
        log.debug(f"settings_cache fetch failed for {key!r}: {e}")
        return None


def get_raw(key: str, strategy_id: Optional[int] = None) -> Optional[str]:
    """String-level getter with TTL cache. Returns ``None`` if the row
    doesn't exist. Callers should prefer ``get_bool``/``get_int``/
    ``get_float`` which layer type coercion + defaults on top."""
    now = time.time()
    cache_key = (key, strategy_id)
    with _LOCK:
        cached = _CACHE.get(cache_key)
        if cached and (now - cached[0]) < _TTL_SEC:
            return cached[1]
    value = _fetch_raw(key, strategy_id)
    with _LOCK:
        _CACHE[cache_key] = (now, value)
    return value


def get_bool(key: str, default: bool = False,
             strategy_id: Optional[int] = None) -> bool:
    raw = get_raw(key, strategy_id)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "t", "yes", "y", "on")


def get_int(key: str, default: int = 0,
            strategy_id: Optional[int] = None) -> int:
    raw = get_raw(key, strategy_id)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def get_float(key: str, default: float = 0.0,
              strategy_id: Optional[int] = None) -> float:
    raw = get_raw(key, strategy_id)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def invalidate() -> None:
    """Force next read to hit the DB. Useful in tests and after a
    known write."""
    with _LOCK:
        _CACHE.clear()
