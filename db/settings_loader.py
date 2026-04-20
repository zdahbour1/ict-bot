"""
Settings and ticker loader — reads configuration from PostgreSQL.

ENH-024 rollouts #2 + #3: settings and tickers are scoped to a strategy.
The "active" strategy is the one whose `name` matches the global
`ACTIVE_STRATEGY` setting (strategy_id IS NULL). Strategy-scoped settings
override globals. Strategy-scoped tickers are the list the bot trades.

Falls back gracefully if database is not available.
"""
import logging
from sqlalchemy import text

from db.connection import get_session, db_available

log = logging.getLogger(__name__)


# ── Active-strategy resolution ────────────────────────────────

def get_active_strategy_id() -> int | None:
    """Resolve ACTIVE_STRATEGY (global setting) → strategy_id.

    Returns the strategy_id whose `name` matches the ACTIVE_STRATEGY value,
    provided that strategy is enabled. Returns None on any lookup failure
    — callers should fall back to the default strategy in that case.
    """
    if not db_available():
        return None
    try:
        session = get_session()
        if not session:
            return None
        row = session.execute(text(
            "SELECT s.strategy_id "
            "FROM settings cfg "
            "JOIN strategies s ON s.name = cfg.value "
            "WHERE cfg.key = 'ACTIVE_STRATEGY' AND cfg.strategy_id IS NULL "
            "  AND s.enabled = TRUE "
            "LIMIT 1"
        )).fetchone()
        session.close()
        return int(row[0]) if row else None
    except Exception as e:
        log.warning(f"get_active_strategy_id failed: {e}")
        return None


def get_default_strategy_id() -> int | None:
    """Fallback when ACTIVE_STRATEGY is missing or points at a disabled row."""
    if not db_available():
        return None
    try:
        session = get_session()
        if not session:
            return None
        row = session.execute(text(
            "SELECT strategy_id FROM strategies WHERE is_default = TRUE LIMIT 1"
        )).fetchone()
        session.close()
        return int(row[0]) if row else None
    except Exception as e:
        log.warning(f"get_default_strategy_id failed: {e}")
        return None


def resolve_strategy_id() -> int | None:
    """Active → default → None. One-call helper for other loaders."""
    sid = get_active_strategy_id()
    if sid is not None:
        return sid
    return get_default_strategy_id()


# ── Ticker loader (strategy-scoped) ───────────────────────────

def load_tickers_from_db(strategy_id: int | None = None) -> list[dict] | None:
    """Load active tickers for a strategy.

    If `strategy_id` is None we resolve the active strategy via
    resolve_strategy_id(). Falls back to the default strategy if that's
    not set.

    Returns list of {"symbol": "QQQ", "contracts": 2, ...} or None on
    DB unavailable.
    """
    if not db_available():
        return None
    try:
        from db.models import Ticker
        session = get_session()
        if not session:
            return None

        if strategy_id is None:
            strategy_id = resolve_strategy_id()

        q = session.query(Ticker).filter(Ticker.is_active == True)
        if strategy_id is not None:
            q = q.filter(Ticker.strategy_id == strategy_id)
        tickers = q.order_by(Ticker.id).all()

        result = [
            {
                "symbol": t.symbol,
                "contracts": t.contracts,
                "name": t.name,
                "sec_type": getattr(t, "sec_type", "OPT"),
                "multiplier": getattr(t, "multiplier", 100),
                "exchange": getattr(t, "exchange", "SMART"),
                "currency": getattr(t, "currency", "USD"),
            }
            for t in tickers
        ]
        session.close()
        if result:
            log.info(f"Loaded {len(result)} active tickers "
                     f"(strategy_id={strategy_id}) from database")
            return result
        return None
    except Exception as e:
        log.warning(f"Failed to load tickers from DB: {e}")
        return None


def load_active_ticker_symbols(strategy_id: int | None = None) -> list[str] | None:
    """Just the symbol list for the given (or active) strategy."""
    tickers = load_tickers_from_db(strategy_id=strategy_id)
    if tickers is None:
        return None
    return [t["symbol"] for t in tickers]


def load_contracts_per_ticker(strategy_id: int | None = None) -> dict | None:
    """{symbol: contracts} map for the given (or active) strategy."""
    tickers = load_tickers_from_db(strategy_id=strategy_id)
    if tickers is None:
        return None
    return {t["symbol"]: t["contracts"] for t in tickers}


# ── Settings loader (strategy-scoped + global overlay) ────────

def _cast_value(raw: str, data_type: str):
    """Cast a settings.value string per its data_type column."""
    if data_type == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return raw
    if data_type == "float":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return raw
    if data_type == "bool":
        return str(raw).lower() in ("true", "1", "yes", "on")
    return raw


def load_settings_from_db(strategy_id: int | None = None) -> dict | None:
    """Load settings with strategy-overlay-global precedence.

    If `strategy_id` is None we resolve the active strategy. Resolution
    order for each setting key:
      1. Strategy-scoped row (strategy_id = <resolved>)
      2. Global row (strategy_id IS NULL)
    Strategy-scoped wins. Keys present in both appear only once in the
    returned dict, carrying the strategy-scoped value.

    Returns flat {"KEY": <casted_value>, ...} or None if DB unavailable.
    """
    if not db_available():
        return None
    try:
        from db.models import Setting
        session = get_session()
        if not session:
            return None

        if strategy_id is None:
            strategy_id = resolve_strategy_id()

        # Load all candidate rows in one query
        if strategy_id is not None:
            rows = session.execute(text(
                "SELECT key, value, data_type, strategy_id "
                "FROM settings "
                "WHERE strategy_id = :sid OR strategy_id IS NULL"
            ), {"sid": strategy_id}).fetchall()
        else:
            rows = session.execute(text(
                "SELECT key, value, data_type, strategy_id FROM settings "
                "WHERE strategy_id IS NULL"
            )).fetchall()
        session.close()

        # Build with overlay: strategy-scoped beats global
        globals_: dict = {}
        scoped: dict = {}
        for key, value, data_type, sid in rows:
            casted = _cast_value(value, data_type)
            if sid is None:
                globals_[key] = casted
            else:
                scoped[key] = casted

        result = {**globals_, **scoped}  # scoped overrides globals
        if result:
            log.info(f"Loaded {len(result)} settings (strategy_id={strategy_id}, "
                     f"{len(scoped)} strategy-scoped overrides, "
                     f"{len(globals_)} globals)")
            return result
        return None
    except Exception as e:
        log.warning(f"Failed to load settings from DB: {e}")
        return None


def get_setting(key: str, default=None, strategy_id: int | None = None):
    """Get a single setting value with overlay semantics.

    Useful for code that only needs one key and doesn't want to pay for
    a full dict load.
    """
    settings = load_settings_from_db(strategy_id=strategy_id)
    if settings is None:
        return default
    return settings.get(key, default)
