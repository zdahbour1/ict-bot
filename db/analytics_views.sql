-- ============================================================
-- Analytics Views — Pacific Timezone, reusable for charts + drill-down
-- Run: psql -U ict_bot -d ict_bot -f db/analytics_views.sql
-- ============================================================
--
-- TODO Phase 2c-2: rewrite v_trades_analytics (and every view that
-- depends on it) to source the moved columns from trade_legs. After the
-- Phase 2a/2b rebuild the following columns no longer exist on the
-- trades table and this file will fail to install:
--     symbol, direction, contracts_entered, contracts_open,
--     contracts_closed, entry_price, exit_price, current_price,
--     profit_target, stop_loss_level, ict_entry, ict_sl, ict_tp,
--     sec_type, multiplier, exchange, currency, underlying,
--     ib_* (order/perm/con ids + bracket status/price/checked_at).
-- Easiest migration is to replace `FROM trades t` with
-- `FROM v_trades_with_first_leg t` (defined in migration 007) for the
-- single-leg case, then audit the multi-leg callsites separately.
-- Deferred because the running bot does not load this file; it is run
-- manually against the dashboard DB.
--

-- ── Base view: all trade data with PT timestamps + computed fields ──
DROP VIEW IF EXISTS v_pnl_by_exit_reason CASCADE;
DROP VIEW IF EXISTS v_pnl_by_contract_type CASCADE;
DROP VIEW IF EXISTS v_risk_by_hour CASCADE;
DROP VIEW IF EXISTS v_contracts_by_hour CASCADE;
DROP VIEW IF EXISTS v_pnl_by_entry_hour CASCADE;
DROP VIEW IF EXISTS v_pnl_by_exit_hour CASCADE;
DROP VIEW IF EXISTS v_pnl_by_ticker CASCADE;
DROP VIEW IF EXISTS v_trades_analytics CASCADE;

CREATE OR REPLACE VIEW v_trades_analytics AS
SELECT
    t.id,
    t.account,
    t.ticker,
    t.symbol,
    t.direction,
    t.status,
    t.contracts_entered,
    t.contracts_open,
    t.contracts_closed,
    t.entry_price,
    t.exit_price,
    t.current_price,
    t.pnl_pct,
    t.pnl_usd,
    t.peak_pnl_pct,
    t.dynamic_sl_pct,
    t.profit_target,
    t.stop_loss_level,
    t.signal_type,
    t.exit_reason,
    t.exit_result,
    t.error_message,
    t.entry_enrichment,
    t.exit_enrichment,
    -- PT timestamps
    t.entry_time AT TIME ZONE 'America/Los_Angeles' AS entry_time_pt,
    t.exit_time AT TIME ZONE 'America/Los_Angeles' AS exit_time_pt,
    -- PT date and hours
    (t.entry_time AT TIME ZONE 'America/Los_Angeles')::date AS trade_date,
    EXTRACT(HOUR FROM t.entry_time AT TIME ZONE 'America/Los_Angeles')::int AS entry_hour,
    EXTRACT(HOUR FROM t.exit_time AT TIME ZONE 'America/Los_Angeles')::int AS exit_hour,
    -- Contract type
    CASE WHEN t.direction = 'LONG' THEN 'Call' ELSE 'Put' END AS contract_type,
    -- Risk capital (premium paid)
    COALESCE(t.entry_price, 0) * 100 * t.contracts_entered AS risk_capital,
    -- Hold time in minutes
    CASE
        WHEN t.exit_time IS NOT NULL AND t.entry_time IS NOT NULL
        THEN ROUND(EXTRACT(EPOCH FROM (t.exit_time - t.entry_time)) / 60, 1)
        ELSE NULL
    END AS hold_minutes
FROM trades t;


-- ── P&L by ticker ──
CREATE OR REPLACE VIEW v_pnl_by_ticker AS
SELECT
    trade_date,
    ticker,
    COUNT(*) AS total_trades,
    COUNT(*) FILTER (WHERE exit_result = 'WIN') AS wins,
    COUNT(*) FILTER (WHERE exit_result = 'LOSS') AS losses,
    COUNT(*) FILTER (WHERE exit_result = 'SCRATCH') AS scratches,
    ROUND(COALESCE(SUM(pnl_usd), 0)::numeric, 2) AS total_pnl,
    ROUND(AVG(pnl_pct) FILTER (WHERE status = 'closed')::numeric, 4) AS avg_pnl_pct,
    ROUND(AVG(hold_minutes) FILTER (WHERE status = 'closed')::numeric, 1) AS avg_hold_min
FROM v_trades_analytics
GROUP BY trade_date, ticker;


-- ── P&L by exit hour (PT) ──
CREATE OR REPLACE VIEW v_pnl_by_exit_hour AS
SELECT
    trade_date,
    exit_hour AS hour,
    COUNT(*) AS trades,
    ROUND(COALESCE(SUM(pnl_usd), 0)::numeric, 2) AS pnl
FROM v_trades_analytics
WHERE status = 'closed' AND exit_hour IS NOT NULL
GROUP BY trade_date, exit_hour;


-- ── P&L by entry hour (PT) ──
CREATE OR REPLACE VIEW v_pnl_by_entry_hour AS
SELECT
    trade_date,
    entry_hour AS hour,
    COUNT(*) AS trades,
    ROUND(COALESCE(SUM(pnl_usd), 0)::numeric, 2) AS pnl
FROM v_trades_analytics
GROUP BY trade_date, entry_hour;


-- ── Risk capital by hour (PT) ──
CREATE OR REPLACE VIEW v_risk_by_hour AS
SELECT
    trade_date,
    entry_hour AS hour,
    ROUND(SUM(risk_capital)::numeric, 2) AS capital,
    SUM(contracts_entered) AS contracts
FROM v_trades_analytics
GROUP BY trade_date, entry_hour;


-- ── Contracts opened by hour (PT) ──
CREATE OR REPLACE VIEW v_contracts_by_hour AS
SELECT
    trade_date,
    entry_hour AS hour,
    SUM(contracts_entered) AS contracts
FROM v_trades_analytics
GROUP BY trade_date, entry_hour;


-- ── P&L by contract type ──
CREATE OR REPLACE VIEW v_pnl_by_contract_type AS
SELECT
    trade_date,
    contract_type,
    COUNT(*) AS trades,
    ROUND(COALESCE(SUM(pnl_usd), 0)::numeric, 2) AS pnl,
    COUNT(*) FILTER (WHERE exit_result = 'WIN') AS wins,
    COUNT(*) FILTER (WHERE exit_result = 'LOSS') AS losses
FROM v_trades_analytics
GROUP BY trade_date, contract_type;


-- ── P&L by exit reason ──
CREATE OR REPLACE VIEW v_pnl_by_exit_reason AS
SELECT
    trade_date,
    exit_reason,
    exit_result,
    COUNT(*) AS trades,
    ROUND(COALESCE(SUM(pnl_usd), 0)::numeric, 2) AS pnl
FROM v_trades_analytics
WHERE status = 'closed'
GROUP BY trade_date, exit_reason, exit_result;


-- ── Daily summary ──
DROP VIEW IF EXISTS v_daily_summary CASCADE;
CREATE OR REPLACE VIEW v_daily_summary AS
SELECT
    trade_date,
    account,
    COUNT(*) AS total_trades,
    COUNT(*) FILTER (WHERE status = 'open') AS open_trades,
    COUNT(*) FILTER (WHERE status = 'closed') AS closed_trades,
    COUNT(*) FILTER (WHERE exit_result = 'WIN') AS wins,
    COUNT(*) FILTER (WHERE exit_result = 'LOSS') AS losses,
    COUNT(*) FILTER (WHERE exit_result = 'SCRATCH') AS scratches,
    ROUND(COALESCE(SUM(pnl_usd) FILTER (WHERE status = 'open'), 0)::numeric, 2) AS open_pnl,
    ROUND(COALESCE(SUM(pnl_usd) FILTER (WHERE status = 'closed'), 0)::numeric, 2) AS closed_pnl,
    ROUND(COALESCE(SUM(pnl_usd), 0)::numeric, 2) AS total_pnl,
    ROUND(
        COUNT(*) FILTER (WHERE exit_result = 'WIN')::numeric /
        NULLIF(COUNT(*) FILTER (WHERE status = 'closed'), 0) * 100,
    1) AS win_rate,
    ROUND(SUM(risk_capital)::numeric, 2) AS total_risk_capital,
    ROUND(AVG(hold_minutes) FILTER (WHERE status = 'closed')::numeric, 1) AS avg_hold_min
FROM v_trades_analytics
GROUP BY trade_date, account;


-- ── P&L by day of week (PT) ──
DROP VIEW IF EXISTS v_pnl_by_day_of_week CASCADE;
CREATE OR REPLACE VIEW v_pnl_by_day_of_week AS
SELECT
    trade_date,
    EXTRACT(ISODOW FROM entry_time_pt)::int AS day_num,  -- 1=Mon, 7=Sun
    to_char(entry_time_pt, 'Dy') AS day_name,
    COUNT(*) AS trades,
    COUNT(*) FILTER (WHERE exit_result = 'WIN') AS wins,
    COUNT(*) FILTER (WHERE exit_result = 'LOSS') AS losses,
    ROUND(COALESCE(SUM(pnl_usd), 0)::numeric, 2) AS total_pnl,
    ROUND(AVG(pnl_usd)::numeric, 2) AS avg_pnl,
    ROUND(
        COUNT(*) FILTER (WHERE exit_result = 'WIN')::numeric /
        NULLIF(COUNT(*) FILTER (WHERE status = 'closed'), 0) * 100,
    1) AS win_rate
FROM v_trades_analytics
WHERE status = 'closed'
GROUP BY trade_date, day_num, day_name;


-- ── P&L by signal type ──
DROP VIEW IF EXISTS v_pnl_by_signal_type CASCADE;
CREATE OR REPLACE VIEW v_pnl_by_signal_type AS
SELECT
    trade_date,
    COALESCE(signal_type, 'unknown') AS signal_type,
    COUNT(*) AS trades,
    COUNT(*) FILTER (WHERE exit_result = 'WIN') AS wins,
    COUNT(*) FILTER (WHERE exit_result = 'LOSS') AS losses,
    ROUND(COALESCE(SUM(pnl_usd), 0)::numeric, 2) AS total_pnl,
    ROUND(AVG(pnl_usd)::numeric, 2) AS avg_pnl,
    ROUND(
        COUNT(*) FILTER (WHERE exit_result = 'WIN')::numeric /
        NULLIF(COUNT(*) FILTER (WHERE status = 'closed'), 0) * 100,
    1) AS win_rate
FROM v_trades_analytics
WHERE status = 'closed'
GROUP BY trade_date, signal_type;


-- ══════════════════════════════════════════════════════════════
-- DONE — All views use Pacific Time consistently
-- ══════════════════════════════════════════════════════════════
