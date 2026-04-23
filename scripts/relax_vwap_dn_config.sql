-- Relax VWAP + delta-neutral for live trading smoke tests.
-- 2026-04-23 — user request: produce more signals to verify code paths.
-- Revert with the companion file restore_vwap_dn_config.sql if needed.
BEGIN;

-- ── VWAP — loosen distance + RSI + trend-EMA so signals fire more often
UPDATE settings SET value = '0.003' WHERE key = 'VWAP_TOUCH_THRESHOLD';   -- was 0.001
UPDATE settings SET value = '45'    WHERE key = 'VWAP_RSI_OVERSOLD';       -- was 35
UPDATE settings SET value = '55'    WHERE key = 'VWAP_RSI_OVERBOUGHT';     -- was 65
UPDATE settings SET value = '10'    WHERE key = 'VWAP_TREND_EMA';          -- was 20

-- ── Delta-neutral — lower IV threshold so iron-condor detects fire more
INSERT INTO settings (category, key, value, data_type, description, strategy_id)
  VALUES ('strategy', 'DELTA_NEUTRAL_IV_THRESHOLD', '0.15', 'float',
          'Annualized IV proxy cutoff — lower = more entries (relaxed 2026-04-23)',
          91)
  ON CONFLICT (key, strategy_id) DO UPDATE SET value = EXCLUDED.value;

-- ── Add missing candidate tickers for VWAP (vwap_revert = strategy_id 90)
INSERT INTO tickers (symbol, name, is_active, contracts, strategy_id, sec_type,
                      multiplier, exchange, currency)
VALUES
  ('AMD',   'Advanced Micro Devices',  true, 2, 90, 'OPT', 100, 'SMART', 'USD'),
  ('META',  'Meta Platforms',           true, 2, 90, 'OPT', 100, 'SMART', 'USD'),
  ('COIN',  'Coinbase Global',          true, 2, 90, 'OPT', 100, 'SMART', 'USD'),
  ('MSTR',  'Strategy (MicroStrategy)', true, 2, 90, 'OPT', 100, 'SMART', 'USD'),
  ('AMZN',  'Amazon.com',               true, 2, 90, 'OPT', 100, 'SMART', 'USD'),
  ('GOOGL', 'Alphabet',                 true, 2, 90, 'OPT', 100, 'SMART', 'USD')
ON CONFLICT (symbol, strategy_id) DO NOTHING;

-- ── Add missing candidate tickers for delta-neutral (strategy_id 91)
-- DN already has AAPL, AMD, AVGO, GOOG, META, NVDA, QQQ, SPY, TSLA.
INSERT INTO tickers (symbol, name, is_active, contracts, strategy_id, sec_type,
                      multiplier, exchange, currency)
VALUES
  ('AMZN', 'Amazon.com',      true, 2, 91, 'OPT', 100, 'SMART', 'USD'),
  ('MSFT', 'Microsoft',       true, 2, 91, 'OPT', 100, 'SMART', 'USD'),
  ('COIN', 'Coinbase Global', true, 2, 91, 'OPT', 100, 'SMART', 'USD')
ON CONFLICT (symbol, strategy_id) DO NOTHING;

COMMIT;

-- Show the applied config
SELECT key, value FROM settings WHERE key LIKE 'VWAP%' OR key LIKE 'DELTA%'
  ORDER BY key;
SELECT s.name, t.symbol FROM tickers t
  JOIN strategies s ON s.strategy_id = t.strategy_id
  WHERE s.name IN ('vwap_revert', 'delta_neutral') AND t.is_active
  ORDER BY s.name, t.symbol;
