"""
Configuration loader — reads from .env file.
All strategy parameters live here with safe defaults.
"""
import os
from dotenv import load_dotenv

load_dotenv()

def _get(key: str, default):
    val = os.getenv(key)
    if val is None:
        return default
    # Cast to same type as default
    if isinstance(default, bool):
        return val.lower() in ("1", "true", "yes")
    if isinstance(default, float):
        return float(val)
    if isinstance(default, int):
        return int(val)
    return val

# ── Instrument ────────────────────────────────────────────────────────────────
SYMBOL                   = _get("SYMBOL", "QQQ")
TIMEZONE                 = _get("TIMEZONE", "America/Los_Angeles")
TRADE_WINDOW_START_PT    = _get("TRADE_WINDOW_START_PT", "07:00")
TRADE_WINDOW_END_PT      = _get("TRADE_WINDOW_END_PT", "09:00")

# ── Data Provider ─────────────────────────────────────────────────────────────
PROVIDER                 = _get("PROVIDER", "yfinance")
PROVIDER_ENV             = _get("PROVIDER_ENV", "sandbox")
PROVIDER_TOKEN           = _get("PROVIDER_TOKEN", "")

# ── Email ─────────────────────────────────────────────────────────────────────
EMAIL_TO                 = _get("EMAIL_TO", "")
EMAIL_FROM               = _get("EMAIL_FROM", "")
EMAIL_SMTP_HOST          = _get("EMAIL_SMTP_HOST", "smtp.gmail.com")
EMAIL_SMTP_PORT          = _get("EMAIL_SMTP_PORT", 587)
EMAIL_APP_PASSWORD       = _get("EMAIL_APP_PASSWORD", "")

# ── Strategy Parameters ───────────────────────────────────────────────────────
RAID_THRESHOLD           = _get("RAID_THRESHOLD", 0.05)
DISPLACEMENT_BODY_MULT   = _get("DISPLACEMENT_BODY_MULT", 1.2)
DISPLACEMENT_LOOKBACK    = _get("DISPLACEMENT_LOOKBACK", 20)
N_CONFIRM_BARS           = _get("N_CONFIRM_BARS", 2)
FVG_MIN_SIZE             = _get("FVG_MIN_SIZE", 0.10)
OB_MAX_CANDLES           = _get("OB_MAX_CANDLES", 3)
IFVG_CONFIRM_MODE        = _get("IFVG_CONFIRM_MODE", "mid")
OB_TRIGGER_MODE          = _get("OB_TRIGGER_MODE", "touch")
MAX_ALERTS_PER_DAY       = _get("MAX_ALERTS_PER_DAY", 6)
LOOKBACK_HOURS           = _get("LOOKBACK_HOURS", 120)

# ── Risk ──────────────────────────────────────────────────────────────────────
MAX_DAILY_LOSS           = _get("MAX_DAILY_LOSS", 200.0)
SL_BUFFER                = _get("SL_BUFFER", 0.05)
TP_LOOKBACK              = _get("TP_LOOKBACK", 40)
