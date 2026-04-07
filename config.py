"""
ICT QQQ Options Bot — Configuration
All settings live here. Edit this file or use the .env file.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Broker (Tastytrade) ──────────────────────────────────
TASTYTRADE_USERNAME = os.getenv("TASTYTRADE_USERNAME")
TASTYTRADE_PASSWORD = os.getenv("TASTYTRADE_PASSWORD")
TASTYTRADE_ACCOUNT  = os.getenv("TASTYTRADE_ACCOUNT")
PAPER_TRADING       = os.getenv("PAPER_TRADING", "false").lower() == "true"

# ── Broker (Schwab paperMoney) ───────────────────────────
SCHWAB_APP_KEY        = os.getenv("SCHWAB_APP_KEY")
SCHWAB_APP_SECRET     = os.getenv("SCHWAB_APP_SECRET")
SCHWAB_CALLBACK_URL   = os.getenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1")
SCHWAB_PAPER_ACCOUNT  = os.getenv("SCHWAB_PAPER_ACCOUNT", "")
USE_SCHWAB            = os.getenv("USE_SCHWAB", "false").lower() == "true"

# ── Broker (Alpaca Paper Trading) ────────────────────────
ALPACA_API_KEY        = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY     = os.getenv("ALPACA_SECRET_KEY")
USE_ALPACA            = os.getenv("USE_ALPACA", "false").lower() == "true"

# ── Broker (Interactive Brokers) ─────────────────────────
IB_HOST               = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT               = int(os.getenv("IB_PORT", "7497"))       # 7497=TWS paper, 4002=Gateway paper
IB_CLIENT_ID          = int(os.getenv("IB_CLIENT_ID", "1"))
IB_ACCOUNT            = os.getenv("IB_ACCOUNT", "")
USE_IB                = os.getenv("USE_IB", "false").lower() == "true"

# ── Dry Run (Paper Trading Simulation) ───────────────────
# True  = logs all trades but never places real orders (safe to test)
# False = places REAL trades on Tastytrade
DRY_RUN             = os.getenv("DRY_RUN", "true").lower() == "true"

# ── Instruments ──────────────────────────────────────────
# Load tickers from tickers.txt (one per line, blank lines and # comments ignored)
_TICKERS_FILE = os.path.join(os.path.dirname(__file__), "tickers.txt")
def _load_tickers():
    if os.path.exists(_TICKERS_FILE):
        with open(_TICKERS_FILE, "r") as f:
            tickers = [line.strip().upper() for line in f
                       if line.strip() and not line.strip().startswith("#")]
        if tickers:
            return tickers
    return ["QQQ"]  # fallback default

TICKERS             = _load_tickers()
TICKER              = TICKERS[0]  # backward compat for backtests
CONTRACTS           = 2          # default number of option contracts per trade
CONTRACTS_PER_TICKER = {t: 2 for t in TICKERS}  # default 2 contracts each

# ── Option Exit Rules ────────────────────────────────────
PROFIT_TARGET       = 1.00       # exit when option premium is up 100%
STOP_LOSS           = 0.60       # exit when option premium is down 60%

# ── Trade Window ──────────────────────────────────────────
TRADE_WINDOW_START_PT  = 6       # 6:00 AM PT
TRADE_WINDOW_START_MIN = 30      # 6:30 AM PT start
TRADE_WINDOW_END_PT    = 13      # 1:00 PM PT (temp: extended for testing today)

# ── ICT Strategy Parameters (from PDF) ───────────────────
RAID_THRESHOLD        = 0.05     # min $ penetration below level to qualify as raid
BODY_MULT             = 1.2      # displacement candle body multiplier
DISPLACEMENT_LOOKBACK = 20       # bars back for median body calculation
N_CONFIRM_BARS        = 2        # bars after raid to confirm displacement
FVG_MIN_SIZE          = 0.10     # min FVG size in dollars
OB_MAX_CANDLES        = 3        # max bearish candles for OB
SL_BUFFER             = 0.05     # buffer below raid low for stop loss
TP_LOOKBACK           = 40       # bars back to find swing high TP
MAX_ALERTS_PER_DAY    = 999      # no practical limit

# ── EMA Filter ────────────────────────────────────────────
EMA_PERIOD_1H         = 20       # 1H 20 EMA for trend direction filter

# ── News Filter ───────────────────────────────────────────
NEWS_BUFFER_MIN       = 30       # minutes around major events to block trades

# ── Email Alerts ─────────────────────────────────────────
EMAIL_TO           = os.getenv("EMAIL_TO",           "omardahbour52@gmail.com")
EMAIL_FROM         = os.getenv("EMAIL_FROM",         "omardahbour52@gmail.com")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "")

# ── Webhook Server ───────────────────────────────────────
PORT                  = 5000
WEBHOOK_SECRET        = os.getenv("WEBHOOK_SECRET", "ict-secret-token")

# ── Exit Monitor ─────────────────────────────────────────
MONITOR_INTERVAL      = 5        # check P&L every 5 seconds

# ── Trade Cooldown ───────────────────────────────────
COOLDOWN_MINUTES      = int(os.getenv("COOLDOWN_MINUTES", "15"))  # min wait after trade exit before re-entry (per ticker)
