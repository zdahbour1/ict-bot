-- Migration 013 — ENH-050: track the SOURCE of each leg's entry_price
-- so dashboards + audits can surface when a price is an estimate.
--
-- Allowed values:
--   'exec'         — per-leg price came from IB Executions (best)
--   'quote'        — filled in via post-fill mid-quote fallback
--   'proportional' — distributed from the combo net_fill_price
--   'mkt_single'   — single-leg trade, no combo concerns
--   NULL           — unknown / legacy row
BEGIN;

ALTER TABLE trade_legs
  ADD COLUMN IF NOT EXISTS price_source VARCHAR(20);

ALTER TABLE backtest_trade_legs
  ADD COLUMN IF NOT EXISTS price_source VARCHAR(20);

COMMIT;
