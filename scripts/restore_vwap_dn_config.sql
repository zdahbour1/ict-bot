-- Revert VWAP + delta-neutral back to their original defaults after
-- smoke testing. Counterpart to relax_vwap_dn_config.sql.
BEGIN;

-- VWAP defaults (match VWAPStrategy.__init__)
UPDATE settings SET value = '0.001' WHERE key = 'VWAP_TOUCH_THRESHOLD';
UPDATE settings SET value = '35'    WHERE key = 'VWAP_RSI_OVERSOLD';
UPDATE settings SET value = '65'    WHERE key = 'VWAP_RSI_OVERBOUGHT';
UPDATE settings SET value = '20'    WHERE key = 'VWAP_TREND_EMA';

-- Delta-neutral default
UPDATE settings SET value = '0.25' WHERE key = 'DELTA_NEUTRAL_IV_THRESHOLD';

-- Note: added tickers are left in place — disable individually by
--   UPDATE tickers SET is_active = false WHERE symbol = 'X' AND strategy_id = Y;

COMMIT;
