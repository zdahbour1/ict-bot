-- ─────────────────────────────────────────────────────────────
-- Enable the ORB strategy (feature/orb-live).
-- See docs/orb_strategy_design.md.
--
-- Apply:
--   docker exec -i ict-bot-postgres-1 psql -U ict_bot -d ict_bot \
--     < db/enable_orb.sql
--
-- Idempotent.
-- ─────────────────────────────────────────────────────────────

BEGIN;

-- Flip orb to enabled
UPDATE strategies
SET enabled = TRUE, updated_at = NOW()
WHERE name = 'orb';

-- Seed ORB-scoped settings. These coexist with ICT's same-key rows
-- because (key, strategy_id) is the unique constraint (NULL-safe for
-- global rows, explicit for strategy-scoped).
INSERT INTO settings
    (category, key, value, data_type, description, is_secret, strategy_id)
SELECT 'strategy', key, value, data_type, description, FALSE, s.strategy_id
FROM (VALUES
    ('ORB_RANGE_MINUTES',     '15',    'int',   'Length of the opening range in minutes (5/15/30/60)'),
    ('ORB_BREAKOUT_BUFFER',   '0.001', 'float', 'Cushion past range high/low to confirm breakout (fraction, 0.001 = 0.1%)'),
    ('ORB_MAX_TRADES_PER_DAY','2',     'int',   'Max trades/day for ORB (one long, one short max)'),
    ('PROFIT_TARGET',         '1.00',  'float', 'TP as fraction of entry price for ORB (100%)'),
    ('STOP_LOSS',             '0.60',  'float', 'SL as fraction of entry price for ORB (60%)'),
    ('COOLDOWN_MINUTES',      '15',    'int',   'Minutes between ORB entries after an exit'),
    ('TRADE_WINDOW_START',    '6',     'int',   'ORB scan window start (PT hour)'),
    ('TRADE_WINDOW_END',      '13',    'int',   'ORB scan window end (PT hour)')
) AS seed_rows(key, value, data_type, description)
CROSS JOIN (SELECT strategy_id FROM strategies WHERE name = 'orb') s
ON CONFLICT (key, strategy_id) DO NOTHING;

-- Verify
DO $$
DECLARE
    orb_sid INT;
    cnt INT;
BEGIN
    SELECT strategy_id INTO orb_sid FROM strategies WHERE name = 'orb' AND enabled = TRUE;
    IF orb_sid IS NULL THEN
        RAISE EXCEPTION 'ORB strategy not enabled';
    END IF;

    SELECT COUNT(*) INTO cnt FROM settings WHERE strategy_id = orb_sid;
    IF cnt < 5 THEN
        RAISE EXCEPTION 'ORB settings not seeded (found %, expected >= 5)', cnt;
    END IF;

    RAISE NOTICE 'ORB enabled (strategy_id=%) with % settings', orb_sid, cnt;
END $$;

COMMIT;
