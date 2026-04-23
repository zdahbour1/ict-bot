-- 011_backtest_trade_legs.sql
--
-- Extend the backtest engine to support multi-leg strategies
-- (delta-neutral iron condors, spreads, hedged positions). Mirrors the
-- live trades + trade_legs split from Phase 2a but scoped to the
-- backtest side so existing single-leg backtests are unaffected.
--
-- Schema changes:
--   1. ALTER backtest_trades ADD COLUMN n_legs SMALLINT NOT NULL DEFAULT 1
--      - cached leg count (1 for legacy, N for multi-leg)
--   2. CREATE TABLE backtest_trade_legs
--      - per-leg instrument + pricing + P&L
--      - FK to backtest_trades with ON DELETE CASCADE (run-level purge works)
--
-- Backward compat:
--   - Existing backtest_trades rows remain untouched.
--   - Single-leg backtests keep writing per-leg fields to backtest_trades
--     AND (new) write one corresponding leg row. UI that reads
--     backtest_trades.symbol / entry_price continues working.
--   - New multi-leg backtests set backtest_trades.symbol to the first leg's
--     symbol (same convention as live v_trades_with_first_leg) with n_legs=N.
--
-- Non-goals (deferred):
--   - Analytics views joining legs (`v_backtest_aggregate_pnl`) — out of scope
--     for this migration, add when a consumer shows up.
--   - Full per-leg fill model — the engine approximates with price-at-bar
--     for now. Per-leg greeks + slippage can come later.
--
-- Ref: docs/multi_strategy_architecture_v2.md §8 deferred item
-- "Delta-neutral backtest support" (ENH-038).

BEGIN;

ALTER TABLE backtest_trades
    ADD COLUMN IF NOT EXISTS n_legs SMALLINT NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS backtest_trade_legs (
    leg_id             INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    backtest_trade_id  INTEGER NOT NULL
                        REFERENCES backtest_trades(id) ON DELETE CASCADE,
    leg_index          SMALLINT NOT NULL DEFAULT 0,
    leg_role           VARCHAR(30),         -- 'short_call', 'hedge_stock', etc.

    -- Instrument definition (NULL strike/right/expiry allowed for STK legs)
    sec_type           VARCHAR(5) NOT NULL DEFAULT 'OPT',
    symbol             VARCHAR(40) NOT NULL,
    underlying         VARCHAR(20),
    strike             NUMERIC(10, 4),
    "right"            VARCHAR(1),          -- 'C' | 'P' | NULL
    expiry             VARCHAR(8),          -- YYYYMMDD
    multiplier         INTEGER NOT NULL DEFAULT 100,

    -- Position state
    direction          VARCHAR(5) NOT NULL DEFAULT 'LONG',
    contracts          INTEGER NOT NULL,

    -- Pricing
    entry_price        NUMERIC(10, 4) NOT NULL,
    exit_price         NUMERIC(10, 4),

    -- Lifecycle
    entry_time         TIMESTAMPTZ NOT NULL,
    exit_time          TIMESTAMPTZ,

    -- Per-leg P&L (= (exit - entry) * contracts * multiplier * sign)
    pnl_usd            NUMERIC(12, 4),

    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT btl_direction_check
        CHECK (direction IN ('LONG', 'SHORT')),
    CONSTRAINT btl_right_check
        CHECK ("right" IS NULL OR "right" IN ('C', 'P')),
    CONSTRAINT btl_sec_type_check
        CHECK (sec_type IN ('OPT', 'FOP', 'STK', 'FUT', 'BAG'))
);

CREATE INDEX IF NOT EXISTS idx_btl_backtest_trade_id
    ON backtest_trade_legs(backtest_trade_id);
CREATE INDEX IF NOT EXISTS idx_btl_symbol
    ON backtest_trade_legs(symbol);

COMMENT ON TABLE backtest_trade_legs IS
    'Per-leg detail for multi-leg backtest trades (delta-neutral, '
    'spreads). Single-leg backtests get one row per trade (leg_index=0). '
    'backtest_trades.n_legs caches the count.';

COMMIT;
