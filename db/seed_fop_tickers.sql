-- ─────────────────────────────────────────────────────────────
-- Seed Futures-Option tickers (feature/futures-options).
-- See docs/futures_options_implementation.md.
--
-- Apply:
--   docker exec -i ict-bot-postgres-1 psql -U ict_bot -d ict_bot \
--     < db/seed_fop_tickers.sql
--
-- Idempotent. All rows is_active=FALSE — nothing trades until a
-- follow-up branch enables them after data provider + strategy tuning.
-- ─────────────────────────────────────────────────────────────

BEGIN;

-- Tickers are strategy-scoped (tickers.strategy_id FK). We seed one
-- row per instrument under strategy_id=1 (ICT) so they appear in the
-- default strategy's instrument list. A later branch that enables FOP
-- trading under a specific strategy can clone these under that
-- strategy's id via the standard replicate-from-source flow.

INSERT INTO tickers
    (symbol, name, is_active, contracts, notes, strategy_id,
     sec_type, multiplier, exchange, currency)
VALUES
    -- Micro E-mini Nasdaq-100 — smallest index futures contract
    ('MNQ', 'Micro E-mini Nasdaq-100', FALSE, 1,
     'Futures option — multiplier $2, GLOBEX, strike interval 25 points. '
     'Disabled until strategy adapts to FOP tick sizing + historical data '
     'source is chosen (see docs/futures_options_implementation.md).',
     1, 'FOP', 2, 'GLOBEX', 'USD'),

    -- Full E-mini Nasdaq-100
    ('NQ',  'E-mini Nasdaq-100',        FALSE, 1,
     'Futures option — multiplier $20, GLOBEX, strike interval 25 points.',
     1, 'FOP', 20, 'GLOBEX', 'USD'),

    -- Micro E-mini S&P 500
    ('MES', 'Micro E-mini S&P 500',     FALSE, 1,
     'Futures option — multiplier $5, GLOBEX, strike interval 5 points.',
     1, 'FOP', 5,  'GLOBEX', 'USD'),

    -- Full E-mini S&P 500
    ('ES',  'E-mini S&P 500',           FALSE, 1,
     'Futures option — multiplier $50, GLOBEX, strike interval 5 points.',
     1, 'FOP', 50, 'GLOBEX', 'USD'),

    -- Gold futures options
    ('GC',  'Gold Futures',              FALSE, 1,
     'Futures option — multiplier $100, NYMEX/COMEX, strike interval 5.',
     1, 'FOP', 100, 'NYMEX', 'USD'),

    -- Crude oil futures options
    ('CL',  'Crude Oil Futures',         FALSE, 1,
     'Futures option — multiplier $1000, NYMEX, strike interval 0.5.',
     1, 'FOP', 1000, 'NYMEX', 'USD')
ON CONFLICT (symbol, strategy_id) DO NOTHING;

DO $$
DECLARE
    cnt INT;
BEGIN
    SELECT COUNT(*) INTO cnt FROM tickers
    WHERE sec_type = 'FOP' AND symbol IN
        ('MNQ', 'NQ', 'MES', 'ES', 'GC', 'CL');
    IF cnt <> 6 THEN
        RAISE EXCEPTION 'Expected 6 FOP tickers seeded, found %', cnt;
    END IF;

    -- Confirm all are is_active=FALSE (safety)
    SELECT COUNT(*) INTO cnt FROM tickers
    WHERE sec_type = 'FOP' AND is_active = TRUE;
    IF cnt <> 0 THEN
        RAISE EXCEPTION 'No FOP ticker should be active yet, found %', cnt;
    END IF;

    RAISE NOTICE 'FOP tickers seeded — 6 inactive placeholders';
END $$;

COMMIT;
