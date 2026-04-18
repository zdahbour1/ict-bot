-- Backtest Framework tables (ENH-019)
-- See docs/backtest_framework.md for design notes.
-- Apply once per DB:
--   docker exec -i ict-bot-postgres-1 psql -U ict_bot -d ict_bot < db/backtest_schema.sql

CREATE TABLE IF NOT EXISTS backtest_runs (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100),
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',
                    -- pending | running | completed | failed

    -- Configuration snapshot (frozen at run time)
    tickers         TEXT[] NOT NULL,
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    config          JSONB NOT NULL DEFAULT '{}',

    -- Results (populated on completion)
    total_trades    INT DEFAULT 0,
    wins            INT DEFAULT 0,
    losses          INT DEFAULT 0,
    scratches       INT DEFAULT 0,
    total_pnl       NUMERIC(12,2) DEFAULT 0,
    win_rate        NUMERIC(5,2) DEFAULT 0,
    avg_win         NUMERIC(12,2) DEFAULT 0,
    avg_loss        NUMERIC(12,2) DEFAULT 0,
    max_drawdown    NUMERIC(12,2) DEFAULT 0,
    sharpe_ratio    NUMERIC(8,4),
    profit_factor   NUMERIC(8,4),
    avg_hold_min    NUMERIC(8,1),
    max_win_streak  INT DEFAULT 0,
    max_loss_streak INT DEFAULT 0,

    -- Metadata
    error_message   TEXT,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    duration_sec    NUMERIC(10,2),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_status  ON backtest_runs(status);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_created ON backtest_runs(created_at DESC);


CREATE TABLE IF NOT EXISTS backtest_trades (
    id              SERIAL PRIMARY KEY,
    run_id          INT NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,

    -- Trade details (mirrors live trades table)
    ticker          VARCHAR(10) NOT NULL,
    symbol          VARCHAR(40),
    direction       VARCHAR(5) NOT NULL,
    contracts       INT NOT NULL DEFAULT 2,

    -- Pricing
    entry_price     NUMERIC(10,4) NOT NULL,
    exit_price      NUMERIC(10,4),
    pnl_pct         NUMERIC(8,4) DEFAULT 0,
    pnl_usd         NUMERIC(12,4) DEFAULT 0,
    peak_pnl_pct    NUMERIC(8,4) DEFAULT 0,

    -- Timing
    entry_time      TIMESTAMPTZ NOT NULL,
    exit_time       TIMESTAMPTZ,
    hold_minutes    NUMERIC(8,1),

    -- Signal info
    signal_type     VARCHAR(40),
    strategy_name   VARCHAR(30) DEFAULT 'ict',
    entry_bar_idx   INT,

    -- Exit info
    exit_reason     VARCHAR(20),   -- TP | SL | TRAIL_STOP | ROLL | TIME_EXIT | EOD_EXIT
    exit_result     VARCHAR(10),   -- WIN | LOSS | SCRATCH

    -- Strategy details
    tp_level        NUMERIC(10,4),
    sl_level        NUMERIC(10,4),
    dynamic_sl_pct  NUMERIC(8,4),
    tp_trailed      BOOLEAN DEFAULT FALSE,
    rolled          BOOLEAN DEFAULT FALSE,

    -- Enrichment (RSI, VWAP, SMAs at entry)
    entry_indicators JSONB DEFAULT '{}',

    -- Metadata
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_backtest_trades_run    ON backtest_trades(run_id);
CREATE INDEX IF NOT EXISTS idx_backtest_trades_ticker ON backtest_trades(ticker);
CREATE INDEX IF NOT EXISTS idx_backtest_trades_result ON backtest_trades(exit_result);
