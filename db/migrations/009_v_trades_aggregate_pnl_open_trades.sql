-- 009_v_trades_aggregate_pnl_open_trades.sql
--
-- Fix v_trades_aggregate_pnl so OPEN trades mark-to-market correctly.
--
-- Before: the view computed pnl_usd as
--   SUM(COALESCE(exit_price - entry_price, 0) * contracts * multiplier * sign)
-- For an open trade, exit_price IS NULL → the subtraction is NULL →
-- COALESCE produces 0. Every open trade reported $0 unrealized P&L,
-- even when current_price showed significant drift from entry.
--
-- After: use COALESCE(exit_price, current_price, entry_price) as the
-- "current value" per leg. Closed legs keep their realized P&L. Open
-- legs mark to current_price when available, fall back to entry_price
-- (zero unrealized) when no live quote has been stamped.
--
-- Idempotent — CREATE OR REPLACE on the view.
CREATE OR REPLACE VIEW v_trades_aggregate_pnl AS
SELECT
    t.id,
    t.strategy_id,
    t.ticker,
    t.status,
    COUNT(l.leg_id)                                                    AS leg_count,
    SUM(l.contracts_entered)                                           AS total_contracts,
    SUM(
        (COALESCE(l.exit_price, l.current_price, l.entry_price) - l.entry_price)
        * l.contracts_entered
        * l.multiplier
        * CASE l.direction WHEN 'LONG' THEN 1 ELSE -1 END
    )                                                                  AS pnl_usd,
    MIN(l.entry_time)                                                  AS first_entry_time,
    MAX(l.exit_time)                                                   AS last_exit_time
FROM trades t
LEFT JOIN trade_legs l ON l.trade_id = t.id
GROUP BY t.id, t.strategy_id, t.ticker, t.status;

COMMENT ON VIEW v_trades_aggregate_pnl IS
    'Authoritative per-trade P&L aggregated across every leg. Handles '
    'open trades via mark-to-market: exit_price when closed, '
    'current_price when open with a live quote, entry_price otherwise. '
    'Trades.pnl_usd is a cached copy; compare against this view when '
    'cache drift is suspected.';
