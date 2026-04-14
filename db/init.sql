-- ============================================================
-- ICT Trading Bot — PostgreSQL Database Schema
-- Version: 1.0
-- Run: psql -U ict_bot -d ict_bot -f init.sql
-- ============================================================

-- ── Trigger function (shared) ────────────────────────────────
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- ══════════════════════════════════════════════════════════════
-- TRADES — Core trade lifecycle table
-- Row INSERT'd at entry, UPDATE'd every 5s, finalized on exit
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS trades (
    id                  INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account             VARCHAR(20)     NOT NULL,
    ticker              VARCHAR(10)     NOT NULL,
    symbol              VARCHAR(40)     NOT NULL,
    direction           VARCHAR(5)      NOT NULL DEFAULT 'LONG'
                        CHECK (direction IN ('LONG', 'SHORT')),

    -- Contract tracking (supports partial closes)
    contracts_entered   INT             NOT NULL,
    contracts_open      INT             NOT NULL,
    contracts_closed    INT             NOT NULL DEFAULT 0,

    -- Pricing
    entry_price         NUMERIC(10,4)   NOT NULL,
    exit_price          NUMERIC(10,4),                          -- set on final close
    current_price       NUMERIC(10,4),                          -- updated every 5s
    ib_fill_price       NUMERIC(10,4),                          -- actual IB execution price
    ib_order_id         INT,
    ib_perm_id          INT,                                    -- IB permanent order ID (survives restarts)
    ib_tp_perm_id       INT,                                    -- TP bracket leg permanent ID
    ib_sl_perm_id       INT,                                    -- SL bracket leg permanent ID
    ib_con_id           INT,                                    -- IB contract ID (unique per option)

    -- P&L (live, updated every 5s)
    pnl_pct             NUMERIC(8,4)    DEFAULT 0,
    pnl_usd             NUMERIC(12,4)   DEFAULT 0,
    peak_pnl_pct        NUMERIC(8,4)    DEFAULT 0,
    dynamic_sl_pct      NUMERIC(8,4)    DEFAULT -0.60,

    -- Exit targets
    profit_target       NUMERIC(10,4)   NOT NULL,
    stop_loss_level     NUMERIC(10,4)   NOT NULL,

    -- ICT signal info
    signal_type         VARCHAR(40),
    ict_entry           NUMERIC(10,4),
    ict_sl              NUMERIC(10,4),
    ict_tp              NUMERIC(10,4),

    -- Timestamps
    entry_time          TIMESTAMPTZ     NOT NULL,
    exit_time           TIMESTAMPTZ,

    -- Status & exit info
    status              VARCHAR(10)     NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'closed', 'errored')),
    exit_reason         VARCHAR(40),
    exit_result         VARCHAR(10)
                        CHECK (exit_result IS NULL OR exit_result IN ('WIN', 'LOSS', 'SCRATCH')),
    error_message       TEXT,

    -- Enrichment data (Greeks, indicators, VIX, stock price)
    entry_enrichment    JSONB           DEFAULT '{}',
    exit_enrichment     JSONB           DEFAULT '{}',

    -- Audit
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_trades_status          ON trades (status);
CREATE INDEX idx_trades_account         ON trades (account);
CREATE INDEX idx_trades_ticker          ON trades (ticker);
CREATE INDEX idx_trades_entry_time      ON trades (entry_time);
CREATE INDEX idx_trades_account_status  ON trades (account, status);
CREATE INDEX idx_trades_account_date    ON trades (account, entry_time DESC);

-- Auto-update updated_at
CREATE TRIGGER trg_trades_updated_at
    BEFORE UPDATE ON trades
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();


-- ══════════════════════════════════════════════════════════════
-- TRADE_CLOSES — Partial close audit trail
-- Each partial close event gets a row
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS trade_closes (
    id                  INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trade_id            INT             NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    contracts           INT             NOT NULL CHECK (contracts > 0),
    close_price         NUMERIC(10,4)   NOT NULL,
    pnl_pct             NUMERIC(8,4)    NOT NULL,
    pnl_usd             NUMERIC(12,4)   NOT NULL,
    reason              VARCHAR(40)     NOT NULL,
    ib_order_id         INT,
    ib_fill_price       NUMERIC(10,4),
    closed_at           TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_trade_closes_trade_id  ON trade_closes (trade_id);


-- ══════════════════════════════════════════════════════════════
-- TRADE_COMMANDS — UI → Bot command channel
-- Dashboard writes commands, bot polls every 5s and executes
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS trade_commands (
    id                  INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trade_id            INT             NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    command             VARCHAR(20)     NOT NULL
                        CHECK (command IN ('close', 'close_partial', 'close_all')),
    contracts           INT,                                    -- NULL = close all remaining
    status              VARCHAR(20)     NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'executing', 'executed', 'failed')),
    error               TEXT,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    executed_at         TIMESTAMPTZ
);

CREATE INDEX idx_trade_commands_status   ON trade_commands (status);
CREATE INDEX idx_trade_commands_trade_id ON trade_commands (trade_id);


-- ══════════════════════════════════════════════════════════════
-- THREAD_STATUS — Scanner thread monitoring
-- Each thread UPSERTs its row on every scan cycle
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS thread_status (
    id                  INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    thread_name         VARCHAR(30)     NOT NULL UNIQUE,
    ticker              VARCHAR(10),
    status              VARCHAR(20)     NOT NULL DEFAULT 'idle'
                        CHECK (status IN ('starting', 'running', 'scanning', 'idle', 'error', 'stopped')),
    last_scan_time      TIMESTAMPTZ,
    last_message        TEXT,
    pid                 INT,                                    -- OS process ID
    thread_id           BIGINT,                                 -- Python thread ident
    scans_today         INT             DEFAULT 0,
    trades_today        INT             DEFAULT 0,
    alerts_today        INT             DEFAULT 0,
    error_count         INT             DEFAULT 0,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- Auto-update updated_at
CREATE TRIGGER trg_thread_status_updated_at
    BEFORE UPDATE ON thread_status
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();


-- ══════════════════════════════════════════════════════════════
-- BOT_STATE — Singleton tracking bot process status
-- Enforced single row via CHECK constraint
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS bot_state (
    id                  INT             PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    status              VARCHAR(20)     NOT NULL DEFAULT 'stopped'
                        CHECK (status IN ('running', 'stopped', 'starting', 'stopping')),
    account             VARCHAR(20),
    pid                 INT,
    total_tickers       INT             DEFAULT 0,
    started_at          TIMESTAMPTZ,
    stopped_at          TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- Auto-update updated_at
CREATE TRIGGER trg_bot_state_updated_at
    BEFORE UPDATE ON bot_state
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Insert the singleton row
INSERT INTO bot_state (id, status) VALUES (1, 'stopped')
ON CONFLICT (id) DO NOTHING;


-- ══════════════════════════════════════════════════════════════
-- ERRORS — Structured error log
-- Queryable by dashboard, supplements bot.log
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS errors (
    id                  INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    thread_name         VARCHAR(30),
    ticker              VARCHAR(10),
    trade_id            INT             REFERENCES trades(id) ON DELETE SET NULL,
    error_type          VARCHAR(50)     NOT NULL,
    message             TEXT            NOT NULL,
    traceback           TEXT,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_errors_created_at      ON errors (created_at DESC);
CREATE INDEX idx_errors_trade_id        ON errors (trade_id);
CREATE INDEX idx_errors_ticker          ON errors (ticker);


-- ══════════════════════════════════════════════════════════════
-- TICKERS — Master list of tradeable instruments
-- Replaces tickers.txt. Bot reads active tickers on startup.
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS tickers (
    id                  INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol              VARCHAR(10)     NOT NULL UNIQUE,
    name                VARCHAR(100),
    is_active           BOOLEAN         NOT NULL DEFAULT TRUE,
    contracts           INT             NOT NULL DEFAULT 2 CHECK (contracts > 0),
    notes               TEXT,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_tickers_active ON tickers (is_active) WHERE is_active = TRUE;

CREATE TRIGGER trg_tickers_updated_at
    BEFORE UPDATE ON tickers
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();


-- ══════════════════════════════════════════════════════════════
-- SETTINGS — Key-value config store
-- Replaces .env and config.py hardcoded values.
-- Bot reads on startup; UI can edit; bot hot-reloads on demand.
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS settings (
    id                  INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category            VARCHAR(30)     NOT NULL,
    key                 VARCHAR(50)     NOT NULL UNIQUE,
    value               TEXT            NOT NULL,
    data_type           VARCHAR(20)     NOT NULL DEFAULT 'string'
                        CHECK (data_type IN ('string', 'int', 'float', 'bool')),
    description         TEXT,
    is_secret           BOOLEAN         NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_settings_category ON settings (category);

CREATE TRIGGER trg_settings_updated_at
    BEFORE UPDATE ON settings
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();


-- ══════════════════════════════════════════════════════════════
-- SEED DATA — Tickers (from current tickers.txt)
-- ══════════════════════════════════════════════════════════════
INSERT INTO tickers (symbol, name, contracts) VALUES
    ('QQQ',   'Invesco QQQ Trust',                  2),
    ('SPY',   'SPDR S&P 500 ETF',                   2),
    ('AAPL',  'Apple Inc.',                          2),
    ('NVDA',  'NVIDIA Corporation',                  2),
    ('TSLA',  'Tesla Inc.',                          2),
    ('IWM',   'iShares Russell 2000 ETF',            2),
    ('AMD',   'Advanced Micro Devices',              2),
    ('AMZN',  'Amazon.com Inc.',                     2),
    ('META',  'Meta Platforms Inc.',                  2),
    ('MSFT',  'Microsoft Corporation',               2),
    ('GOOGL', 'Alphabet Inc.',                       2),
    ('NFLX',  'Netflix Inc.',                        2),
    ('PLTR',  'Palantir Technologies',               2),
    ('SLV',   'iShares Silver Trust',                2),
    ('XLF',   'Financial Select Sector SPDR',        2),
    ('MU',    'Micron Technology',                    2),
    ('INTC',  'Intel Corporation',                   2),
    ('TQQQ',  'ProShares UltraPro QQQ',              2),
    ('SSO',   'ProShares Ultra S&P500',              2)
ON CONFLICT (symbol) DO NOTHING;


-- ══════════════════════════════════════════════════════════════
-- SEED DATA — Settings (from current config.py / .env)
-- ══════════════════════════════════════════════════════════════
INSERT INTO settings (category, key, value, data_type, description, is_secret) VALUES
    -- Broker: Interactive Brokers
    ('broker', 'USE_IB',               'true',         'bool',   'Use Interactive Brokers as the broker',       FALSE),
    ('broker', 'IB_HOST',              '127.0.0.1',    'string', 'IB Gateway/TWS host address',                 FALSE),
    ('broker', 'IB_PORT',              '7497',         'int',    'IB Gateway/TWS port (7497=TWS paper, 4002=Gateway paper)', FALSE),
    ('broker', 'IB_CLIENT_ID',         '1',            'int',    'IB API client ID',                            FALSE),
    ('broker', 'IB_ACCOUNT',           '',             'string', 'IB account number (e.g. DU1566080)',          FALSE),
    ('broker', 'DRY_RUN',              'false',        'bool',   'If true, log trades but do not place real orders', FALSE),
    ('broker', 'PAPER_TRADING',        'true',         'bool',   'Use paper trading mode',                      FALSE),
    -- Broker: Tastytrade
    ('broker', 'USE_TASTYTRADE',       'false',        'bool',   'Use Tastytrade as the broker',                FALSE),
    ('broker', 'TASTYTRADE_USERNAME',  '',             'string', 'Tastytrade login email',                      TRUE),
    ('broker', 'TASTYTRADE_PASSWORD',  '',             'string', 'Tastytrade login password',                   TRUE),
    ('broker', 'TASTYTRADE_ACCOUNT',   '',             'string', 'Tastytrade account number',                   FALSE),
    -- Broker: Schwab
    ('broker', 'USE_SCHWAB',           'false',        'bool',   'Use Schwab as the broker',                    FALSE),
    ('broker', 'SCHWAB_APP_KEY',       '',             'string', 'Schwab OAuth app key',                        TRUE),
    ('broker', 'SCHWAB_APP_SECRET',    '',             'string', 'Schwab OAuth app secret',                     TRUE),
    ('broker', 'SCHWAB_CALLBACK_URL',  'https://127.0.0.1', 'string', 'Schwab OAuth callback URL',             FALSE),
    ('broker', 'SCHWAB_PAPER_ACCOUNT', '',             'string', 'Schwab paper account number',                 FALSE),
    -- Broker: Alpaca
    ('broker', 'USE_ALPACA',           'false',        'bool',   'Use Alpaca as the broker',                    FALSE),
    ('broker', 'ALPACA_API_KEY',       '',             'string', 'Alpaca API key',                              TRUE),
    ('broker', 'ALPACA_SECRET_KEY',    '',             'string', 'Alpaca secret key',                           TRUE),
    -- Strategy: ICT Parameters
    ('strategy', 'RAID_THRESHOLD',        '0.05',  'float', 'Min $ penetration below level to qualify as raid',     FALSE),
    ('strategy', 'BODY_MULT',             '1.2',   'float', 'Displacement candle body multiplier',                  FALSE),
    ('strategy', 'DISPLACEMENT_LOOKBACK', '20',    'int',   'Bars back for median body calculation',                FALSE),
    ('strategy', 'N_CONFIRM_BARS',        '2',     'int',   'Bars after raid to confirm displacement',              FALSE),
    ('strategy', 'FVG_MIN_SIZE',          '0.10',  'float', 'Minimum FVG size in dollars',                          FALSE),
    ('strategy', 'OB_MAX_CANDLES',        '3',     'int',   'Max bearish candles for order block',                  FALSE),
    ('strategy', 'SL_BUFFER',             '0.05',  'float', 'Buffer below raid low for stop loss ($)',               FALSE),
    ('strategy', 'TP_LOOKBACK',           '40',    'int',   'Bars back to find swing high take profit',             FALSE),
    ('strategy', 'MAX_ALERTS_PER_DAY',    '999',   'int',   'Max signal alerts per day (no practical limit)',       FALSE),
    ('strategy', 'EMA_PERIOD_1H',         '20',    'int',   '1H EMA period for trend direction filter',            FALSE),
    ('strategy', 'NEWS_BUFFER_MIN',       '30',    'int',   'Minutes around major news events to block trades',    FALSE),
    -- Exit Rules
    ('exit_rules', 'PROFIT_TARGET',    '1.00',  'float', 'Exit when option premium is up this % (1.00 = 100%)',    FALSE),
    ('exit_rules', 'STOP_LOSS',        '0.60',  'float', 'Exit when option premium is down this % (0.60 = 60%)',   FALSE),
    ('exit_rules', 'COOLDOWN_MINUTES', '15',    'int',   'Min minutes after trade exit before re-entry per ticker', FALSE),
    -- Trade Window
    ('trade_window', 'TRADE_WINDOW_START_PT',  '6',   'int', 'Trade window start hour (PT)',                       FALSE),
    ('trade_window', 'TRADE_WINDOW_START_MIN', '30',  'int', 'Trade window start minute (PT)',                     FALSE),
    ('trade_window', 'TRADE_WINDOW_END_PT',    '13',  'int', 'Trade window end hour (PT)',                         FALSE),
    -- General
    ('general', 'CONTRACTS',         '2',     'int',   'Default number of option contracts per trade',              FALSE),
    ('general', 'MONITOR_INTERVAL',  '5',     'int',   'Seconds between exit monitor checks',                      FALSE),
    -- Email
    ('email', 'EMAIL_TO',            '',      'string', 'Email address for trade alerts',                           FALSE),
    ('email', 'EMAIL_FROM',          '',      'string', 'Sender email address (Gmail)',                             FALSE),
    ('email', 'EMAIL_APP_PASSWORD',  '',      'string', 'Gmail app-specific password',                              TRUE),
    -- Webhook
    ('webhook', 'PORT',              '5000',  'int',    'Webhook server port',                                      FALSE),
    ('webhook', 'WEBHOOK_SECRET',    'ict-secret-token', 'string', 'Secret token for webhook authentication',       TRUE)
ON CONFLICT (key) DO NOTHING;


-- ══════════════════════════════════════════════════════════════
-- VIEWS — Aggregated queries for the dashboard
-- ══════════════════════════════════════════════════════════════

-- Daily summary per account
CREATE OR REPLACE VIEW v_daily_summary AS
SELECT
    account,
    entry_time::date                                            AS trade_date,
    COUNT(*)                                                    AS total_trades,
    COUNT(*) FILTER (WHERE status = 'open')                     AS open_trades,
    COUNT(*) FILTER (WHERE status = 'closed')                   AS closed_trades,
    COUNT(*) FILTER (WHERE status = 'errored')                  AS errored_trades,
    COUNT(*) FILTER (WHERE exit_result = 'WIN')                 AS wins,
    COUNT(*) FILTER (WHERE exit_result = 'LOSS')                AS losses,
    COUNT(*) FILTER (WHERE exit_result = 'SCRATCH')             AS scratches,
    COALESCE(SUM(pnl_usd) FILTER (WHERE status = 'open'), 0)   AS open_pnl,
    COALESCE(SUM(pnl_usd) FILTER (WHERE status = 'closed'), 0) AS closed_pnl,
    COALESCE(SUM(pnl_usd), 0)                                  AS total_pnl,
    ROUND(
        COUNT(*) FILTER (WHERE exit_result = 'WIN')::numeric /
        NULLIF(COUNT(*) FILTER (WHERE status = 'closed'), 0) * 100,
    1)                                                          AS win_rate
FROM trades
GROUP BY account, entry_time::date;


-- Ticker performance (all time)
CREATE OR REPLACE VIEW v_ticker_performance AS
SELECT
    ticker,
    COUNT(*)                                                    AS total_trades,
    COUNT(*) FILTER (WHERE exit_result = 'WIN')                 AS wins,
    COUNT(*) FILTER (WHERE exit_result = 'LOSS')                AS losses,
    ROUND(AVG(pnl_pct) FILTER (WHERE status = 'closed'), 2)    AS avg_pnl_pct,
    COALESCE(SUM(pnl_usd) FILTER (WHERE status = 'closed'), 0) AS total_pnl,
    ROUND(
        COUNT(*) FILTER (WHERE exit_result = 'WIN')::numeric /
        NULLIF(COUNT(*) FILTER (WHERE status = 'closed'), 0) * 100,
    1)                                                          AS win_rate,
    MAX(entry_time)                                             AS last_trade
FROM trades
GROUP BY ticker
ORDER BY total_pnl DESC;


-- Active commands (pending/executing)
CREATE OR REPLACE VIEW v_pending_commands AS
SELECT
    tc.id AS command_id,
    tc.trade_id,
    t.ticker,
    t.symbol,
    tc.command,
    tc.contracts,
    tc.status,
    tc.created_at,
    tc.error
FROM trade_commands tc
JOIN trades t ON t.id = tc.trade_id
WHERE tc.status IN ('pending', 'executing')
ORDER BY tc.created_at;


-- ══════════════════════════════════════════════════════════════
-- GRANTS (for application user if separate from owner)
-- ══════════════════════════════════════════════════════════════
-- GRANT ALL ON ALL TABLES IN SCHEMA public TO ict_bot;
-- GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO ict_bot;


-- ══════════════════════════════════════════════════════════════
-- DONE
-- ══════════════════════════════════════════════════════════════
