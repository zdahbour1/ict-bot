"""
Settings and ticker loader — reads configuration from PostgreSQL.
Falls back gracefully if database is not available.
"""
import logging
from db.connection import get_session, db_available

log = logging.getLogger(__name__)


def load_tickers_from_db() -> list[dict] | None:
    """
    Load active tickers from the database.
    Returns list of {"symbol": "QQQ", "contracts": 2, ...} or None if DB unavailable.
    """
    if not db_available():
        return None
    try:
        from db.models import Ticker
        session = get_session()
        if not session:
            return None
        tickers = session.query(Ticker).filter(Ticker.is_active == True).order_by(Ticker.id).all()
        result = [
            {
                "symbol": t.symbol,
                "contracts": t.contracts,
                "name": t.name,
            }
            for t in tickers
        ]
        session.close()
        if result:
            log.info(f"Loaded {len(result)} active tickers from database")
            return result
        return None
    except Exception as e:
        log.warning(f"Failed to load tickers from DB: {e}")
        return None


def load_active_ticker_symbols() -> list[str] | None:
    """Load just the symbol list of active tickers. Returns None if DB unavailable."""
    tickers = load_tickers_from_db()
    if tickers is None:
        return None
    return [t["symbol"] for t in tickers]


def load_contracts_per_ticker() -> dict | None:
    """Load {symbol: contracts} mapping. Returns None if DB unavailable."""
    tickers = load_tickers_from_db()
    if tickers is None:
        return None
    return {t["symbol"]: t["contracts"] for t in tickers}


def load_settings_from_db() -> dict | None:
    """
    Load all settings from the database as a flat dict: {"KEY": "value", ...}.
    Returns None if DB unavailable.
    """
    if not db_available():
        return None
    try:
        from db.models import Setting
        session = get_session()
        if not session:
            return None
        settings = session.query(Setting).all()
        result = {}
        for s in settings:
            # Cast value based on data_type
            if s.data_type == "int":
                try:
                    result[s.key] = int(s.value)
                except ValueError:
                    result[s.key] = s.value
            elif s.data_type == "float":
                try:
                    result[s.key] = float(s.value)
                except ValueError:
                    result[s.key] = s.value
            elif s.data_type == "bool":
                result[s.key] = s.value.lower() in ("true", "1", "yes")
            else:
                result[s.key] = s.value
        session.close()
        if result:
            log.info(f"Loaded {len(result)} settings from database")
            return result
        return None
    except Exception as e:
        log.warning(f"Failed to load settings from DB: {e}")
        return None


def get_setting(key: str, default=None):
    """Get a single setting value from DB. Returns default if not found."""
    settings = load_settings_from_db()
    if settings is None:
        return default
    return settings.get(key, default)
