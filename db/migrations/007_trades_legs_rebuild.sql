-- ============================================================================
-- 007_trades_legs_rebuild.sql
--
-- Multi-strategy v2 — Phase 2a (big-bang trades table rebuild).
--
-- Transforms `trades` from the monolithic single-leg representation into
-- a thin logical-deal envelope, with all per-leg fields moved into a new
-- `trade_legs` child table. Also deletes the obsolete ACTIVE_STRATEGY
-- singleton setting.
--
-- Strategy (per user instruction):
--   1. Rename original `trades` → `trades_pre_legs`, rename all its
--      indexes to `_pre_legs` suffix. Frozen snapshot, retained.
--   2. Drop FKs + triggers referencing trades (will be recreated).
--   3. Create new slim `trades` table with only envelope columns
--      + n_legs (cached) + ib_client_id (for Phase 5 thread-owned close).
--   4. Create `trade_legs` table holding every moved column.
--   5. Backfill: one trades row + one trade_legs row per trades_pre_legs row.
--   6. Restore FK references pointing at new trades.
--   7. Recreate trigger.
--   8. Create convenience views `v_trades_with_first_leg` and
--      `v_trades_aggregate_pnl` for backward-compat reads.
--   9. Delete ACTIVE_STRATEGY singleton setting (strategies.enabled now
--      drives activation).
--
-- This migration is intended to run atomically inside a transaction so
-- any failure rolls back to the original schema cleanly.
-- ============================================================================

BEGIN;

-- ── 1. Drop cross-table FKs that reference trades.id ────────────────────────
-- These will be recreated in step 6 pointing at the new trades table.
ALTER TABLE errors DROP CONSTRAINT IF EXISTS errors_trade_id_fkey;
ALTER TABLE trade_closes DROP CONSTRAINT IF EXISTS trade_closes_trade_id_fkey;
ALTER TABLE trade_commands DROP CONSTRAINT IF EXISTS trade_commands_trade_id_fkey;

-- ── 2. Drop trigger on old trades (will recreate in step 7) ─────────────────
DROP TRIGGER IF EXISTS trg_trades_updated_at ON trades;

-- ── 3. Snapshot: rename `trades` and all its indexes ────────────────────────
ALTER TABLE trades RENAME TO trades_pre_legs;

ALTER INDEX trades_pkey                  RENAME TO trades_pre_legs_pkey;
ALTER INDEX idx_trades_account           RENAME TO idx_trades_pre_legs_account;
ALTER INDEX idx_trades_account_date      RENAME TO idx_trades_pre_legs_account_date;
ALTER INDEX idx_trades_account_status    RENAME TO idx_trades_pre_legs_account_status;
ALTER INDEX idx_trades_client_trade_id   RENAME TO idx_trades_pre_legs_client_trade_id;
ALTER INDEX idx_trades_entry_time        RENAME TO idx_trades_pre_legs_entry_time;
ALTER INDEX idx_trades_ib_con_id         RENAME TO idx_trades_pre_legs_ib_con_id;
ALTER INDEX idx_trades_ib_perm_id        RENAME TO idx_trades_pre_legs_ib_perm_id;
ALTER INDEX idx_trades_status            RENAME TO idx_trades_pre_legs_status;
ALTER INDEX idx_trades_strategy          RENAME TO idx_trades_pre_legs_strategy;
ALTER INDEX idx_trades_ticker            RENAME TO idx_trades_pre_legs_ticker;

-- Rename the check constraints too so the new table can use the original names.
ALTER TABLE trades_pre_legs RENAME CONSTRAINT trades_direction_check   TO trades_pre_legs_direction_check;
ALTER TABLE trades_pre_legs RENAME CONSTRAINT trades_exit_result_check TO trades_pre_legs_exit_result_check;
ALTER TABLE trades_pre_legs RENAME CONSTRAINT trades_status_check      TO trades_pre_legs_status_check;
ALTER TABLE trades_pre_legs RENAME CONSTRAINT trades_strategy_id_fkey  TO trades_pre_legs_strategy_id_fkey;

COMMENT ON TABLE trades_pre_legs IS
    'Frozen snapshot of the monolithic trades table taken 2026-04-22 '
    'during the multi-strategy v2 migration (db/migrations/007). '
    'Retained read-only so legacy code references fail loudly on the '
    'new slim trades table instead of silently reading wrong data. '
    'Drop this table after a sustained period of live validation.';

-- ── 4. New slim `trades` envelope ──────────────────────────────────────────
CREATE TABLE trades (
    id                     INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    account                VARCHAR(20)  NOT NULL,
    ticker                 VARCHAR(10)  NOT NULL,
    strategy_id            INTEGER      NOT NULL REFERENCES strategies(strategy_id),

    -- Signal metadata (strategy-level — applies to the whole deal)
    signal_type            VARCHAR(40),
    strategy_config        JSONB        NOT NULL DEFAULT '{}'::jsonb,

    -- Aggregated P&L across all legs (cached for fast reads;
    -- v_trades_aggregate_pnl view is authoritative on cache drift).
    pnl_pct                NUMERIC(8,4)  DEFAULT 0,
    pnl_usd                NUMERIC(12,4) DEFAULT 0,
    peak_pnl_pct           NUMERIC(8,4)  DEFAULT 0,
    dynamic_sl_pct         NUMERIC(8,4)  DEFAULT -0.60,

    -- Lifecycle
    entry_time             TIMESTAMPTZ  NOT NULL,
    exit_time              TIMESTAMPTZ,
    status                 VARCHAR(10)  NOT NULL DEFAULT 'open',
    exit_reason            VARCHAR(40),
    exit_result            VARCHAR(10),
    error_message          TEXT,

    -- Observability
    entry_enrichment       JSONB        DEFAULT '{}'::jsonb,
    exit_enrichment        JSONB        DEFAULT '{}'::jsonb,
    notes                  TEXT,

    -- Cross-system correlation
    client_trade_id        VARCHAR(40),
    -- Pool slot that placed the first leg — close flow routes cancels
    -- through this clientId (Phase 5 thread-owned-close). Nullable
    -- because backfilled trades don't have it.
    ib_client_id           SMALLINT,

    -- Cached number of legs (1 for legacy backfilled trades; N for
    -- delta-neutral / multi-leg strategies).
    n_legs                 SMALLINT     NOT NULL DEFAULT 1,

    -- Audit
    created_at             TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT trades_status_check
        CHECK (status IN ('open', 'closed', 'errored')),
    CONSTRAINT trades_exit_result_check
        CHECK (exit_result IS NULL OR exit_result IN ('WIN', 'LOSS', 'SCRATCH'))
);

COMMENT ON TABLE trades IS
    'Logical "deal" envelope. Per-leg IB order details live in trade_legs. '
    'A single-leg strategy (ICT/ORB/VWAP) = 1 legs row. A 4-leg iron '
    'condor = 4 legs rows. See docs/multi_strategy_architecture_v2.md.';

COMMENT ON COLUMN trades.ib_client_id IS
    'Pool slot clientId that placed the first leg of this trade. Used by '
    'the close flow to route cancel/sell through the owning client '
    '(IB cross-client cancel asymmetry — error 10147). See docs/ib_db_correlation.md §11.';

CREATE INDEX idx_trades_account         ON trades(account);
CREATE INDEX idx_trades_account_date    ON trades(account, entry_time DESC);
CREATE INDEX idx_trades_account_status  ON trades(account, status);
CREATE UNIQUE INDEX idx_trades_client_trade_id
    ON trades(client_trade_id) WHERE client_trade_id IS NOT NULL;
CREATE INDEX idx_trades_entry_time      ON trades(entry_time);
CREATE INDEX idx_trades_status          ON trades(status);
CREATE INDEX idx_trades_strategy        ON trades(strategy_id);
CREATE INDEX idx_trades_ticker          ON trades(ticker);
-- Extension of ARCH-006 for multi-strategy: one open slot per
-- (strategy, ticker). Per docs/multi_strategy_architecture_v2.md §3.3
-- this allows multiple strategies to hold concurrent positions on the
-- same ticker (e.g. ICT long SPY + ORB short SPY).
CREATE UNIQUE INDEX idx_trades_open_per_strategy_ticker
    ON trades(strategy_id, ticker) WHERE status = 'open';

-- ── 5. New `trade_legs` child table ─────────────────────────────────────────
CREATE TABLE trade_legs (
    leg_id                 INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    trade_id               INTEGER NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    leg_index              SMALLINT NOT NULL DEFAULT 0,
    leg_role               VARCHAR(30),       -- e.g. 'short_call', 'long_put', 'hedge_stock'; NULL for legacy single-leg

    -- Instrument definition (NULL strike/right/expiry allowed for STK legs)
    sec_type               VARCHAR(5)  NOT NULL DEFAULT 'OPT',
    symbol                 VARCHAR(40) NOT NULL,      -- OCC for options, plain ticker for stock
    underlying             VARCHAR(20),
    strike                 NUMERIC(10,4),
    "right"                VARCHAR(1),                -- 'C' / 'P' / NULL for non-options (quoted — reserved word)
    expiry                 VARCHAR(8),                -- YYYYMMDD
    multiplier             INTEGER     NOT NULL DEFAULT 100,
    exchange               VARCHAR(20) NOT NULL DEFAULT 'SMART',
    currency               VARCHAR(5)  NOT NULL DEFAULT 'USD',

    -- Position state
    direction              VARCHAR(5)  NOT NULL DEFAULT 'LONG',
    contracts_entered      INTEGER     NOT NULL,
    contracts_open         INTEGER     NOT NULL,
    contracts_closed       INTEGER     NOT NULL DEFAULT 0,

    -- Pricing
    entry_price            NUMERIC(10,4) NOT NULL,
    exit_price             NUMERIC(10,4),
    current_price          NUMERIC(10,4),
    ib_fill_price          NUMERIC(10,4),

    -- Strategy-provided reference levels (carried through from legacy)
    profit_target          NUMERIC(10,4),
    stop_loss_level        NUMERIC(10,4),
    ict_entry              NUMERIC(10,4),
    ict_sl                 NUMERIC(10,4),
    ict_tp                 NUMERIC(10,4),

    -- IB identifiers for this specific leg's parent (entry) order
    ib_order_id            INTEGER,
    ib_perm_id             INTEGER,
    ib_con_id              INTEGER,

    -- Protective brackets attached to this leg
    ib_tp_order_id         INTEGER,
    ib_tp_perm_id          INTEGER,
    ib_tp_status           VARCHAR(20),
    ib_tp_price            NUMERIC(10,4),
    ib_sl_order_id         INTEGER,
    ib_sl_perm_id          INTEGER,
    ib_sl_status           VARCHAR(20),
    ib_sl_price            NUMERIC(10,4),
    ib_brackets_checked_at TIMESTAMPTZ,

    -- Lifecycle (per-leg; usually mirrors the parent trade but legs can
    -- close independently when legging out of a multi-leg position)
    entry_time             TIMESTAMPTZ NOT NULL,
    exit_time              TIMESTAMPTZ,
    leg_status             VARCHAR(10) NOT NULL DEFAULT 'open',

    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT trade_legs_direction_check
        CHECK (direction IN ('LONG', 'SHORT')),
    CONSTRAINT trade_legs_status_check
        CHECK (leg_status IN ('open', 'closed', 'errored')),
    CONSTRAINT trade_legs_right_check
        CHECK ("right" IS NULL OR "right" IN ('C', 'P')),
    CONSTRAINT trade_legs_sec_type_check
        CHECK (sec_type IN ('OPT', 'FOP', 'STK', 'FUT', 'BAG'))
);

COMMENT ON TABLE trade_legs IS
    'Individual IB order legs for a logical trade. Single-leg strategies '
    'have one row per trade. Multi-leg (delta-neutral, spreads) have N rows. '
    'See docs/multi_strategy_architecture_v2.md §2.';

CREATE INDEX idx_trade_legs_trade_id    ON trade_legs(trade_id);
CREATE INDEX idx_trade_legs_ib_perm_id  ON trade_legs(ib_perm_id) WHERE ib_perm_id IS NOT NULL;
CREATE INDEX idx_trade_legs_ib_con_id   ON trade_legs(ib_con_id) WHERE ib_con_id IS NOT NULL;
CREATE INDEX idx_trade_legs_symbol      ON trade_legs(symbol);
CREATE INDEX idx_trade_legs_leg_status  ON trade_legs(leg_status) WHERE leg_status = 'open';
-- IB orderId is only unique per (clientId, session) — across the pool
-- and across bot restarts the SAME orderId legitimately recurs. Index
-- kept for lookup speed but NOT unique. Uniqueness would need to be
-- over (ib_client_id, ib_order_id) and gated on currently-active legs
-- only; deferred to when the close flow actually indexes by that tuple.
CREATE INDEX idx_trade_legs_ib_order_id
    ON trade_legs(ib_order_id) WHERE ib_order_id IS NOT NULL;

-- ── 6. Backfill from snapshot ──────────────────────────────────────────────
-- Step 6a: INSERT envelope rows (preserving id so FK references stay valid).
-- We have to set the identity sequence to continue from the max existing id.
INSERT INTO trades (
    id, account, ticker, strategy_id,
    signal_type, strategy_config,
    pnl_pct, pnl_usd, peak_pnl_pct, dynamic_sl_pct,
    entry_time, exit_time, status, exit_reason, exit_result, error_message,
    entry_enrichment, exit_enrichment, notes,
    client_trade_id, n_legs,
    created_at, updated_at
)
OVERRIDING SYSTEM VALUE
SELECT
    id, account, ticker, strategy_id,
    signal_type, COALESCE(strategy_config, '{}'::jsonb),
    COALESCE(pnl_pct, 0), COALESCE(pnl_usd, 0),
    COALESCE(peak_pnl_pct, 0), COALESCE(dynamic_sl_pct, -0.60),
    entry_time, exit_time, status, exit_reason, exit_result, error_message,
    COALESCE(entry_enrichment, '{}'::jsonb),
    COALESCE(exit_enrichment, '{}'::jsonb),
    notes,
    client_trade_id,
    1,  -- legacy trades are all single-leg
    created_at, updated_at
FROM trades_pre_legs
ORDER BY id;

-- Align the identity sequence so new inserts continue past the backfilled ids.
SELECT setval(
    pg_get_serial_sequence('trades', 'id'),
    COALESCE((SELECT MAX(id) FROM trades), 1),
    TRUE
);

-- Step 6b: INSERT one leg per backfilled trade
INSERT INTO trade_legs (
    trade_id, leg_index, leg_role,
    sec_type, symbol, underlying, strike, "right", expiry, multiplier, exchange, currency,
    direction, contracts_entered, contracts_open, contracts_closed,
    entry_price, exit_price, current_price, ib_fill_price,
    profit_target, stop_loss_level, ict_entry, ict_sl, ict_tp,
    ib_order_id, ib_perm_id, ib_con_id,
    ib_tp_order_id, ib_tp_perm_id, ib_tp_status, ib_tp_price,
    ib_sl_order_id, ib_sl_perm_id, ib_sl_status, ib_sl_price,
    ib_brackets_checked_at,
    entry_time, exit_time, leg_status,
    created_at, updated_at
)
SELECT
    id, 0, NULL,
    sec_type, symbol, underlying, NULL, NULL, NULL, multiplier, exchange, currency,
    direction, contracts_entered, contracts_open, contracts_closed,
    entry_price, exit_price, current_price, ib_fill_price,
    profit_target, stop_loss_level, ict_entry, ict_sl, ict_tp,
    ib_order_id, ib_perm_id, ib_con_id,
    ib_tp_order_id, ib_tp_perm_id, ib_tp_status, ib_tp_price,
    ib_sl_order_id, ib_sl_perm_id, ib_sl_status, ib_sl_price,
    ib_brackets_checked_at,
    entry_time, exit_time, status,
    created_at, updated_at
FROM trades_pre_legs
ORDER BY id;

-- ── 7. Recreate FK references pointing at new trades ───────────────────────
ALTER TABLE errors
    ADD CONSTRAINT errors_trade_id_fkey
    FOREIGN KEY (trade_id) REFERENCES trades(id) ON DELETE SET NULL;

ALTER TABLE trade_closes
    ADD CONSTRAINT trade_closes_trade_id_fkey
    FOREIGN KEY (trade_id) REFERENCES trades(id) ON DELETE CASCADE;

ALTER TABLE trade_commands
    ADD CONSTRAINT trade_commands_trade_id_fkey
    FOREIGN KEY (trade_id) REFERENCES trades(id) ON DELETE CASCADE;

-- ── 8. Recreate updated_at trigger on new trades ───────────────────────────
CREATE TRIGGER trg_trades_updated_at
    BEFORE UPDATE ON trades
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_trade_legs_updated_at
    BEFORE UPDATE ON trade_legs
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ── 9. Convenience views for backward-compat reads ─────────────────────────

-- v_trades_with_first_leg: flattens trade + first leg for single-leg reads.
-- Most UI pages should continue to work against this view until they're
-- refactored to expand multi-leg trades inline.
CREATE OR REPLACE VIEW v_trades_with_first_leg AS
SELECT
    t.id, t.account, t.ticker, t.strategy_id,
    t.signal_type, t.strategy_config,
    t.pnl_pct, t.pnl_usd, t.peak_pnl_pct, t.dynamic_sl_pct,
    t.entry_time, t.exit_time, t.status, t.exit_reason, t.exit_result,
    t.error_message, t.entry_enrichment, t.exit_enrichment, t.notes,
    t.client_trade_id, t.ib_client_id, t.n_legs,
    t.created_at, t.updated_at,
    -- First leg's fields surfaced at trade level
    l.leg_id AS first_leg_id,
    l.sec_type, l.symbol, l.underlying, l.strike, l."right", l.expiry,
    l.multiplier, l.exchange, l.currency,
    l.direction,
    l.contracts_entered, l.contracts_open, l.contracts_closed,
    l.entry_price, l.exit_price, l.current_price, l.ib_fill_price,
    l.profit_target, l.stop_loss_level,
    l.ict_entry, l.ict_sl, l.ict_tp,
    l.ib_order_id, l.ib_perm_id, l.ib_con_id,
    l.ib_tp_order_id, l.ib_tp_perm_id, l.ib_tp_status, l.ib_tp_price,
    l.ib_sl_order_id, l.ib_sl_perm_id, l.ib_sl_status, l.ib_sl_price,
    l.ib_brackets_checked_at
FROM trades t
LEFT JOIN LATERAL (
    SELECT * FROM trade_legs WHERE trade_id = t.id
    ORDER BY leg_index ASC LIMIT 1
) l ON TRUE;

COMMENT ON VIEW v_trades_with_first_leg IS
    'Flattened single-leg view. One row per trade, surfacing the first legs '
    'fields at trade level for backward-compat with legacy reads. Multi-leg '
    'trades show only the first leg — use v_trades_aggregate_pnl for totals '
    'and query trade_legs directly for per-leg detail.';

-- v_trades_aggregate_pnl: true P&L across all legs, for summary/analytics.
CREATE OR REPLACE VIEW v_trades_aggregate_pnl AS
SELECT
    t.id,
    t.strategy_id,
    t.ticker,
    t.status,
    COUNT(l.leg_id)                                                   AS leg_count,
    SUM(l.contracts_entered)                                          AS total_contracts,
    SUM(
        COALESCE(l.exit_price - l.entry_price, 0)
        * l.contracts_entered
        * l.multiplier
        * CASE l.direction WHEN 'LONG' THEN 1 ELSE -1 END
    )                                                                 AS pnl_usd,
    MIN(l.entry_time)                                                 AS first_entry_time,
    MAX(l.exit_time)                                                  AS last_exit_time
FROM trades t
LEFT JOIN trade_legs l ON l.trade_id = t.id
GROUP BY t.id, t.strategy_id, t.ticker, t.status;

COMMENT ON VIEW v_trades_aggregate_pnl IS
    'Authoritative per-trade P&L aggregated across every leg. Trades.pnl_usd '
    'is a cached copy; compare against this view when cache drift is '
    'suspected.';

-- ── 10. Retire ACTIVE_STRATEGY singleton setting ───────────────────────────
-- strategies.enabled is now the sole activation signal per the v2 design.
DELETE FROM settings
 WHERE key = 'ACTIVE_STRATEGY'
   AND strategy_id IS NULL;

COMMIT;
