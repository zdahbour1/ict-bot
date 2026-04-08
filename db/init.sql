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
