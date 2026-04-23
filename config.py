"""
ICT Options Bot — Configuration
Loads settings from: 1) PostgreSQL (if DATABASE_URL set), 2) .env file, 3) hardcoded defaults.
Loads tickers from: 1) PostgreSQL tickers table, 2) tickers.txt, 3) ["QQQ"] fallback.
"""
import os
import logging
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

# ── Database URL (required for dashboard mode, optional for standalone) ──
DATABASE_URL = os.getenv("DATABASE_URL")

# ── Try loading settings from DB ────────────────────────────
_db_settings = None
if DATABASE_URL:
    try:
        from db.settings_loader import load_settings_from_db
        _db_settings = load_settings_from_db()
    except Exception as e:
        log.warning(f"Could not load settings from DB: {e}")
        _db_settings = None


def _get(key: str, default=None, cast=None):
    """Get a setting: DB first, then .env, then default. Optional type cast."""
    # 1. Try DB
    if _db_settings and key in _db_settings:
        val = _db_settings[key]
        if cast and not isinstance(val, cast):
            try:
                return cast(val)
            except (ValueError, TypeError):
                pass
        return val
    # 2. Try .env
    env_val = os.getenv(key)
    if env_val is not None:
        if cast == bool:
            return env_val.lower() in ("true", "1", "yes")
        if cast:
            try:
                return cast(env_val)
            except (ValueError, TypeError):
                pass
        return env_val
    # 3. Default
    return default


# ── Broker (Tastytrade) ──────────────────────────────────
TASTYTRADE_USERNAME = _get("TASTYTRADE_USERNAME")
TASTYTRADE_PASSWORD = _get("TASTYTRADE_PASSWORD")
TASTYTRADE_ACCOUNT  = _get("TASTYTRADE_ACCOUNT")
PAPER_TRADING       = _get("PAPER_TRADING", False, bool)

# ── Broker (Schwab paperMoney) ───────────────────────────
SCHWAB_APP_KEY        = _get("SCHWAB_APP_KEY")
SCHWAB_APP_SECRET     = _get("SCHWAB_APP_SECRET")
SCHWAB_CALLBACK_URL   = _get("SCHWAB_CALLBACK_URL", "https://127.0.0.1")
SCHWAB_PAPER_ACCOUNT  = _get("SCHWAB_PAPER_ACCOUNT", "")
USE_SCHWAB            = _get("USE_SCHWAB", False, bool)

# ── Broker (Alpaca Paper Trading) ────────────────────────
ALPACA_API_KEY        = _get("ALPACA_API_KEY")
ALPACA_SECRET_KEY     = _get("ALPACA_SECRET_KEY")
USE_ALPACA            = _get("USE_ALPACA", False, bool)

# ── Broker (Interactive Brokers) ─────────────────────────
IB_HOST               = _get("IB_HOST", "127.0.0.1")
IB_PORT               = _get("IB_PORT", 7497, int)
IB_CLIENT_ID          = _get("IB_CLIENT_ID", 1, int)
IB_ACCOUNT            = _get("IB_ACCOUNT", "")
USE_IB                = _get("USE_IB", False, bool)

# ── Dry Run (Paper Trading Simulation) ───────────────────
DRY_RUN               = _get("DRY_RUN", True, bool)

# ── Instruments ──────────────────────────────────────────
# Load tickers: DB first, then tickers.txt, then fallback
_TICKERS_FILE = os.path.join(os.path.dirname(__file__), "tickers.txt")

def _load_tickers():
    # 1. Try DB
    if DATABASE_URL:
        try:
            from db.settings_loader import load_active_ticker_symbols
            db_tickers = load_active_ticker_symbols()
            if db_tickers:
                log.info(f"Loaded {len(db_tickers)} tickers from database")
                return db_tickers
        except Exception as e:
            log.warning(f"Could not load tickers from DB: {e}")
    # 2. Try tickers.txt
    if os.path.exists(_TICKERS_FILE):
        with open(_TICKERS_FILE, "r") as f:
            tickers = [line.strip().upper() for line in f
                       if line.strip() and not line.strip().startswith("#")]
        if tickers:
            return tickers
    # 3. Fallback
    return ["QQQ"]

def _load_contracts_per_ticker():
    # 1. Try DB
    if DATABASE_URL:
        try:
            from db.settings_loader import load_contracts_per_ticker
            db_contracts = load_contracts_per_ticker()
            if db_contracts:
                return db_contracts
        except Exception:
            pass
    # 2. Default: 2 contracts each
    return {t: CONTRACTS for t in TICKERS}

TICKERS              = _load_tickers()
TICKER               = TICKERS[0]  # backward compat for backtests
CONTRACTS            = _get("CONTRACTS", 2, int)
CONTRACTS_PER_TICKER = _load_contracts_per_ticker()

# ── Option Exit Rules ────────────────────────────────────
PROFIT_TARGET         = _get("PROFIT_TARGET", 1.00, float)
STOP_LOSS             = _get("STOP_LOSS", 0.60, float)

# ── Close flow mode ──────────────────────────────────────
# True (new default): "sell-first" — skip blocking bracket-cancel-verify,
#   fire MKT SELL immediately, then verify brackets auto-cancelled by IB
#   on position flat. Works around IB's cross-client cancel asymmetry.
#   Design: docs/ib_db_correlation.md (Cross-client section).
# False: "cancel-first" (legacy) — strict terminal-state cancel BEFORE sell.
#   Keep as escape hatch if sell-first shows issues in the wild.
CLOSE_MODE_SELL_FIRST = _get("CLOSE_MODE_SELL_FIRST", True, bool)
# Seconds to wait for IB to auto-cancel brackets after position flattens.
POST_SELL_BRACKET_TIMEOUT = _get("POST_SELL_BRACKET_TIMEOUT", 5.0, float)

# ── FOP (Futures Options) live selection rules ───────────
# Liquidity-first gate per docs/fop_live_trading_design.md.
# If no candidate contract passes these, the FOP scanner SKIPS the
# trade entirely — better no-trade than a thin trade.
FOP_MAX_DTE             = _get("FOP_MAX_DTE", 60, int)
FOP_MIN_OPEN_INTEREST   = _get("FOP_MIN_OPEN_INTEREST", 500, int)
FOP_MIN_VOLUME          = _get("FOP_MIN_VOLUME", 100, int)
FOP_MAX_SPREAD_PCT      = _get("FOP_MAX_SPREAD_PCT", 0.15, float)
# Preference order, comma-separated: quarterly|monthly|weekly|daily
FOP_EXPIRY_PREF         = _get("FOP_EXPIRY_PREF",
                                "quarterly,monthly,weekly", str)

# ── Trade Window ──────────────────────────────────────────
TRADE_WINDOW_START_PT  = _get("TRADE_WINDOW_START_PT", 6, int)
TRADE_WINDOW_START_MIN = _get("TRADE_WINDOW_START_MIN", 30, int)
TRADE_WINDOW_END_PT    = _get("TRADE_WINDOW_END_PT", 13, int)

# ── ICT Strategy Parameters ─────────────────────────────
RAID_THRESHOLD        = _get("RAID_THRESHOLD", 0.05, float)
BODY_MULT             = _get("BODY_MULT", 1.2, float)
DISPLACEMENT_LOOKBACK = _get("DISPLACEMENT_LOOKBACK", 20, int)
N_CONFIRM_BARS        = _get("N_CONFIRM_BARS", 2, int)
FVG_MIN_SIZE          = _get("FVG_MIN_SIZE", 0.10, float)
OB_MAX_CANDLES        = _get("OB_MAX_CANDLES", 3, int)
SL_BUFFER             = _get("SL_BUFFER", 0.05, float)
TP_LOOKBACK           = _get("TP_LOOKBACK", 40, int)
MAX_ALERTS_PER_DAY    = _get("MAX_ALERTS_PER_DAY", 999, int)

# ── EMA Filter ────────────────────────────────────────────
EMA_PERIOD_1H         = _get("EMA_PERIOD_1H", 20, int)

# ── News Filter ───────────────────────────────────────────
NEWS_BUFFER_MIN       = _get("NEWS_BUFFER_MIN", 30, int)

# ── Email Alerts ─────────────────────────────────────────
EMAIL_TO              = _get("EMAIL_TO", "")
EMAIL_FROM            = _get("EMAIL_FROM", "")
EMAIL_APP_PASSWORD    = _get("EMAIL_APP_PASSWORD", "")

# ── Webhook Server ───────────────────────────────────────
PORT                  = _get("PORT", 5000, int)
WEBHOOK_SECRET        = _get("WEBHOOK_SECRET", "ict-secret-token")

# ── Exit Monitor ─────────────────────────────────────────
MONITOR_INTERVAL      = _get("MONITOR_INTERVAL", 5, int)

# ── Trade Cooldown ───────────────────────────────────
COOLDOWN_MINUTES      = _get("COOLDOWN_MINUTES", 15, int)

# ── Bracket Orders (IB server-side TP/SL) ────────────
USE_BRACKET_ORDERS    = _get("USE_BRACKET_ORDERS", True, bool)

# ── Option Rolling ───────────────────────────────────
ROLL_ENABLED          = _get("ROLL_ENABLED", True, bool)
ROLL_THRESHOLD        = _get("ROLL_THRESHOLD", 0.70, float)  # roll at 70% of TP

# ── TP → Trailing Stop ──────────────────────────────
# At TP level, instead of hard exit, move SL to TP and let trade run
TP_TO_TRAIL           = _get("TP_TO_TRAIL", True, bool)

# ── IB Reconciliation ────────────────────────────────
RECONCILIATION_INTERVAL_MIN = _get("RECONCILIATION_INTERVAL_MIN", 2, int)

# ── Multi-leg entry routing (ENH-046) ────────────────
# When True, multi-leg strategies (iron condor, spread, hedged) submit
# the entry as ONE IB Bag/combo order instead of N independent orders.
# All legs fill atomically at a net price; one combo conId per IB order.
# Default True as of 2026-04-23 (user enabled for paper testing —
# see ENH-046). Override via env var or settings table to toggle.
USE_COMBO_ORDERS_FOR_MULTI_LEG = _get("USE_COMBO_ORDERS_FOR_MULTI_LEG", True, bool)
