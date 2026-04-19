-- ─────────────────────────────────────────────────────────────
-- Backtest Framework schema (ENH-019 + aligned with ENH-024 rollout #1)
-- See docs/backtest_framework.md and docs/active_strategy_design.md
--
-- Apply:
--   docker exec -i ict-bot-postgres-1 psql -U ict_bot -d ict_bot \
--     < db/backtest_schema.sql
--
-- Depends on: db/active_strategy_schema.sql (the `strategies` table)
-- Idempotent — safe to re-run.
-- ─────────────────────────────────────────────────────────────

BEGIN;

-- ── backtest_runs ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS backtest_runs (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100),
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',
                    -- pending | running | completed | failed

    -- Strategy attribution (one strategy per run, per active_strategy_design)
    strategy_id     INT NOT NULL REFERENCES strategies(strategy_id),

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

CREATE INDEX IF NOT EXISTS idx_backtest_runs_status   ON backtest_runs(status);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_strategy ON backtest_runs(strategy_id);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_created  ON backtest_runs(created_at DESC);


-- ── backtest_trades ────────────────────────────────────────
-- One row per simulated trade. Rich JSONB columns store all the
-- indicators + context we want for downstream data-science analysis.

CREATE TABLE IF NOT EXISTS backtest_trades (
    id              SERIAL PRIMARY KEY,
    run_id          INT NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    strategy_id     INT NOT NULL REFERENCES strategies(strategy_id),

    -- Trade details (mirrors live trades table)
    ticker          VARCHAR(10) NOT NULL,
    symbol          VARCHAR(40),
    direction       VARCHAR(5) NOT NULL,      -- LONG | SHORT
    contracts       INT NOT NULL DEFAULT 2,

    -- Pricing
    entry_price     NUMERIC(10,4) NOT NULL,
    exit_price      NUMERIC(10,4),
    pnl_pct         NUMERIC(8,4) DEFAULT 0,
    pnl_usd         NUMERIC(12,4) DEFAULT 0,
    peak_pnl_pct    NUMERIC(8,4) DEFAULT 0,
    slippage_paid   NUMERIC(10,4) DEFAULT 0,
    commission      NUMERIC(10,4) DEFAULT 0,

    -- Timing
    entry_time      TIMESTAMPTZ NOT NULL,
    exit_time       TIMESTAMPTZ,
    hold_minutes    NUMERIC(8,1),

    -- Signal info
    signal_type     VARCHAR(40),
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

    -- Rich enrichment (for data-science feature analysis)
    -- entry_indicators: RSI, VWAP, SMA20/50/200, ATR, VIX, volume_ratio, ...
    -- exit_indicators : same shape, at exit bar
    -- entry_context   : bars_since_high, bars_since_low, day_of_week,
    --                   session_phase, price_vs_vwap, etc.
    -- signal_details  : raw signal dict (raid info, confirmation, FVG/OB)
    entry_indicators JSONB DEFAULT '{}',
    exit_indicators  JSONB DEFAULT '{}',
    entry_context    JSONB DEFAULT '{}',
    signal_details   JSONB DEFAULT '{}',

    -- Metadata
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_backtest_trades_run      ON backtest_trades(run_id);
CREATE INDEX IF NOT EXISTS idx_backtest_trades_strategy ON backtest_trades(strategy_id);
CREATE INDEX IF NOT EXISTS idx_backtest_trades_ticker   ON backtest_trades(ticker);
CREATE INDEX IF NOT EXISTS idx_backtest_trades_result   ON backtest_trades(exit_result);
CREATE INDEX IF NOT EXISTS idx_backtest_trades_signal   ON backtest_trades(signal_type);

-- Partial index: GIN on entry_indicators for fast feature-analysis queries
-- (e.g. "all trades where RSI < 30") — requires jsonb_path_ops for operator class
CREATE INDEX IF NOT EXISTS idx_backtest_trades_entry_ind
    ON backtest_trades USING GIN (entry_indicators jsonb_path_ops);

COMMIT;
