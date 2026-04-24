-- ZDN (zero-delta-neutral) family — register 4 new strategies on top
-- of the existing DN variants + clone V5's ticker roster.
--
-- Idempotent: ON CONFLICT on name skips re-insert. Ticker rows are
-- only cloned on INSERT (first-run); re-running will not duplicate.
--
-- 2026-04-24 — ENH: ZDN butterfly gamma-scalping variants.

BEGIN;

-- 1) Register the four ZDN strategies.
INSERT INTO strategies (name, display_name, description, class_path, enabled)
VALUES
  ('zdn_0dte',
   'ZDN 0DTE — Iron Butterfly (same-day)',
   'Zero-delta-neutral gamma-scalp: ATM iron butterfly opened ≥10:00 ET, '
   '±10-share hedge band, 50% TP / 25% SL, close 15min before EOD. 0-DTE expiry.',
   'strategy.delta_neutral_variant_strategy.DNVariantStrategyZDN0DTE',
   TRUE),
  ('zdn_weekly',
   'ZDN Weekly — Iron Butterfly (next Friday)',
   'Zero-delta-neutral gamma-scalp: ATM iron butterfly opened ≥10:00 ET, '
   '±10-share hedge band, 50% TP / 25% SL. Next-Friday expiry.',
   'strategy.delta_neutral_variant_strategy.DNVariantStrategyZDNWeekly',
   TRUE),
  ('zdn_monthly',
   'ZDN Monthly — Iron Butterfly (3rd Fri this month)',
   'Zero-delta-neutral gamma-scalp: ATM iron butterfly opened ≥10:00 ET, '
   '±10-share hedge band, 50% TP / 25% SL. 3rd-Friday-this-month expiry.',
   'strategy.delta_neutral_variant_strategy.DNVariantStrategyZDNMonthly',
   TRUE),
  ('zdn_next_month',
   'ZDN NextMonth — Iron Butterfly (3rd Fri next month)',
   'Zero-delta-neutral gamma-scalp: ATM iron butterfly opened ≥10:00 ET, '
   '±10-share hedge band, 50% TP / 25% SL. 3rd-Friday-NEXT-month expiry.',
   'strategy.delta_neutral_variant_strategy.DNVariantStrategyZDNNextMonth',
   TRUE)
ON CONFLICT (name) DO NOTHING;

-- 2) Clone V5's ticker roster for each ZDN strategy.
--    Only fires when the target has no existing ticker rows yet — safe
--    to re-run without dup'ing.
INSERT INTO tickers (symbol, name, is_active, contracts, notes, strategy_id)
SELECT t.symbol, t.name, t.is_active, t.contracts, t.notes, s.strategy_id
  FROM tickers t
  JOIN strategies src ON src.name = 'v5_hedged'
  JOIN strategies s
    ON s.name IN ('zdn_0dte', 'zdn_weekly', 'zdn_monthly', 'zdn_next_month')
 WHERE t.strategy_id = src.strategy_id
   AND NOT EXISTS (
     SELECT 1 FROM tickers t2
      WHERE t2.strategy_id = s.strategy_id AND t2.symbol = t.symbol
   );

COMMIT;

-- Report: rows landed
SELECT s.strategy_id, s.name, s.enabled,
       (SELECT COUNT(*) FROM tickers WHERE strategy_id = s.strategy_id) AS tickers
  FROM strategies s
 WHERE s.name LIKE 'zdn_%'
 ORDER BY s.strategy_id;
