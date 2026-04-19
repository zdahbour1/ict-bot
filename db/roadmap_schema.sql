-- ─────────────────────────────────────────────────────────────
-- Roadmap Schema Extensions
-- See docs/roadmap_schema_extensions.md for the full design.
--
-- Apply:
--   docker exec -i ict-bot-postgres-1 psql -U ict_bot -d ict_bot \
--     < db/roadmap_schema.sql
--
-- Idempotent — safe to re-run. Every change uses IF NOT EXISTS /
-- ON CONFLICT.
--
-- Zero behavior change on its own. All new columns have defaults
-- matching today's implicit assumptions, and the pre-seeded strategy
-- rows are disabled.
-- ─────────────────────────────────────────────────────────────

BEGIN;

-- ── 1. Security-type columns on trades ──────────────────────

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS sec_type   VARCHAR(5)  NOT NULL DEFAULT 'OPT',
    ADD COLUMN IF NOT EXISTS multiplier INT         NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS exchange   VARCHAR(20) NOT NULL DEFAULT 'SMART',
    ADD COLUMN IF NOT EXISTS currency   VARCHAR(5)  NOT NULL DEFAULT 'USD',
    ADD COLUMN IF NOT EXISTS underlying VARCHAR(20);

-- Strategy config snapshot at trade time (distinct from strategy_id
-- and signal_type — this captures the exact tuning parameters that
-- produced the trade, so historical analysis can distinguish trades
-- taken with SL=0.5 vs SL=0.6 of the same strategy.)
ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS strategy_config JSONB NOT NULL DEFAULT '{}';


-- ── 2. Same columns on backtest_trades ──────────────────────

ALTER TABLE backtest_trades
    ADD COLUMN IF NOT EXISTS sec_type        VARCHAR(5)  NOT NULL DEFAULT 'OPT',
    ADD COLUMN IF NOT EXISTS multiplier      INT         NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS exchange        VARCHAR(20) NOT NULL DEFAULT 'SMART',
    ADD COLUMN IF NOT EXISTS currency        VARCHAR(5)  NOT NULL DEFAULT 'USD',
    ADD COLUMN IF NOT EXISTS underlying      VARCHAR(20),
    ADD COLUMN IF NOT EXISTS strategy_config JSONB       NOT NULL DEFAULT '{}';


-- ── 3. Security-type columns on tickers ─────────────────────

ALTER TABLE tickers
    ADD COLUMN IF NOT EXISTS sec_type   VARCHAR(5)  NOT NULL DEFAULT 'OPT',
    ADD COLUMN IF NOT EXISTS multiplier INT         NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS exchange   VARCHAR(20) NOT NULL DEFAULT 'SMART',
    ADD COLUMN IF NOT EXISTS currency   VARCHAR(5)  NOT NULL DEFAULT 'USD';


-- ── 4. Pre-seed placeholder strategy rows (all DISABLED) ────
-- These become visible in the dashboard Strategies dropdown but cannot
-- be selected at bot-start or backtest launch until their code lands
-- and they're explicitly flipped enabled.

INSERT INTO strategies (name, display_name, description, class_path, enabled, is_default)
VALUES
    ('orb',
     'Opening Range Breakout',
     'Trades the breakout of the first N minutes of the session. '
     'Configurable range window (5/15/30/60 min), breakout buffer, and '
     'risk:reward ratio.',
     'strategy.orb_strategy.ORBStrategy',
     FALSE, FALSE),

    ('vwap_revert',
     'VWAP Mean Reversion',
     'Mean reversion to session VWAP. Buys pullbacks to VWAP in uptrends, '
     'sells rallies to VWAP in downtrends. Uses RSI oversold/overbought '
     'as confirmation.',
     'strategy.vwap_strategy.VWAPStrategy',
     FALSE, FALSE),

    ('delta_neutral',
     'Delta-Neutral Iron Condor',
     'Multi-leg iron condor targeting roughly 0.15-delta wings. Profits '
     'from theta decay while the underlying stays inside the body. '
     'Requires trade_legs table (pending separate DDL).',
     'strategy.delta_neutral_strategy.DeltaNeutralStrategy',
     FALSE, FALSE)
ON CONFLICT (name) DO NOTHING;


-- ── 5. Verification (aborts the transaction if anything is off) ──

DO $$
DECLARE
    cnt INT;
BEGIN
    -- trades column count
    SELECT COUNT(*) INTO cnt FROM information_schema.columns
    WHERE table_name = 'trades' AND column_name IN
        ('sec_type','multiplier','exchange','currency','underlying','strategy_config');
    IF cnt <> 6 THEN
        RAISE EXCEPTION 'trades table missing one of the new columns (found %, expected 6)', cnt;
    END IF;

    -- backtest_trades column count
    SELECT COUNT(*) INTO cnt FROM information_schema.columns
    WHERE table_name = 'backtest_trades' AND column_name IN
        ('sec_type','multiplier','exchange','currency','underlying','strategy_config');
    IF cnt <> 6 THEN
        RAISE EXCEPTION 'backtest_trades table missing one of the new columns (found %, expected 6)', cnt;
    END IF;

    -- tickers column count
    SELECT COUNT(*) INTO cnt FROM information_schema.columns
    WHERE table_name = 'tickers' AND column_name IN
        ('sec_type','multiplier','exchange','currency');
    IF cnt <> 4 THEN
        RAISE EXCEPTION 'tickers table missing one of the new columns (found %, expected 4)', cnt;
    END IF;

    -- 4 strategies present (ict + orb + vwap_revert + delta_neutral)
    SELECT COUNT(*) INTO cnt FROM strategies
    WHERE name IN ('ict', 'orb', 'vwap_revert', 'delta_neutral');
    IF cnt <> 4 THEN
        RAISE EXCEPTION 'expected 4 strategy rows, found %', cnt;
    END IF;

    -- Only ict is enabled
    SELECT COUNT(*) INTO cnt FROM strategies WHERE enabled = TRUE;
    IF cnt <> 1 THEN
        RAISE EXCEPTION 'expected exactly 1 enabled strategy (ict), found %', cnt;
    END IF;

    RAISE NOTICE 'Roadmap schema extensions applied OK';
END $$;

COMMIT;
