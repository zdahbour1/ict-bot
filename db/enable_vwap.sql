-- ─────────────────────────────────────────────────────────────
-- Enable the VWAP Reversion strategy (feature/vwap-revert).
-- See docs/vwap_strategy_design.md.
--
-- Apply:
--   docker exec -i ict-bot-postgres-1 psql -U ict_bot -d ict_bot \
--     < db/enable_vwap.sql
--
-- Idempotent.
-- ─────────────────────────────────────────────────────────────

BEGIN;

UPDATE strategies
SET enabled = TRUE, updated_at = NOW()
WHERE name = 'vwap_revert';

-- Seed VWAP-scoped settings (coexist with ICT/ORB same-key rows via the
-- (key, strategy_id) unique constraint)
INSERT INTO settings
    (category, key, value, data_type, description, is_secret, strategy_id)
SELECT 'strategy', key, value, data_type, description, FALSE, s.strategy_id
FROM (VALUES
    ('VWAP_TOUCH_THRESHOLD',  '0.001', 'float', 'Within this fraction of VWAP counts as a touch (0.001 = 0.1%)'),
    ('VWAP_TREND_EMA',        '20',    'int',   '1h EMA period for trend filter'),
    ('VWAP_RSI_PERIOD',       '14',    'int',   'RSI lookback on base interval'),
    ('VWAP_RSI_OVERSOLD',     '35',    'int',   'LONG fires only when RSI drops below this'),
    ('VWAP_RSI_OVERBOUGHT',   '65',    'int',   'SHORT fires only when RSI rises above this'),
    ('VWAP_ATR_PERIOD',       '14',    'int',   'ATR lookback for TP/SL sizing'),
    ('VWAP_TP_ATR_MULT',      '2.0',   'float', 'TP = entry +/- this multiple of ATR'),
    ('VWAP_SL_ATR_MULT',      '1.0',   'float', 'SL = entry +/- this multiple of ATR'),
    ('COOLDOWN_MINUTES',      '15',    'int',   'Minutes between VWAP entries after an exit'),
    ('PROFIT_TARGET',         '1.00',  'float', 'Fallback option TP% (ATR stops override)'),
    ('STOP_LOSS',             '0.60',  'float', 'Fallback option SL% (ATR stops override)'),
    ('TRADE_WINDOW_START',    '6',     'int',   'VWAP scan window start (PT hour)'),
    ('TRADE_WINDOW_END',      '13',    'int',   'VWAP scan window end (PT hour)')
) AS seed_rows(key, value, data_type, description)
CROSS JOIN (SELECT strategy_id FROM strategies WHERE name = 'vwap_revert') s
ON CONFLICT (key, strategy_id) DO NOTHING;

DO $$
DECLARE
    sid INT;
    cnt INT;
BEGIN
    SELECT strategy_id INTO sid FROM strategies
    WHERE name = 'vwap_revert' AND enabled = TRUE;
    IF sid IS NULL THEN
        RAISE EXCEPTION 'VWAP strategy not enabled';
    END IF;

    SELECT COUNT(*) INTO cnt FROM settings WHERE strategy_id = sid;
    IF cnt < 8 THEN
        RAISE EXCEPTION 'VWAP settings not seeded (found %, expected >= 8)', cnt;
    END IF;

    RAISE NOTICE 'VWAP enabled (strategy_id=%) with % settings', sid, cnt;
END $$;

COMMIT;
