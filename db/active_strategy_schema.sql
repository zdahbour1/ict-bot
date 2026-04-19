-- ─────────────────────────────────────────────────────────────
-- Active-Strategy Foundation (ENH-024 rollout #1)
-- See docs/active_strategy_design.md for the full design.
--
-- Apply:
--   docker exec -i ict-bot-postgres-1 psql -U ict_bot -d ict_bot \
--     < db/active_strategy_schema.sql
--
-- Idempotent — safe to re-run. Every step guarded with IF [NOT] EXISTS
-- where Postgres supports it; otherwise wrapped in DO blocks.
--
-- Zero behavior change on its own. ICT remains the only strategy, every
-- existing trade/ticker/setting lands under strategy_id = 1.
-- ─────────────────────────────────────────────────────────────

BEGIN;

-- ── 1. strategies table ────────────────────────────────────

CREATE TABLE IF NOT EXISTS strategies (
    strategy_id   SERIAL PRIMARY KEY,
    name          VARCHAR(30)  NOT NULL UNIQUE,
    display_name  VARCHAR(80)  NOT NULL,
    description   TEXT,
    class_path    VARCHAR(200) NOT NULL,
    enabled       BOOLEAN      NOT NULL DEFAULT TRUE,
    is_default    BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- At most one default strategy
CREATE UNIQUE INDEX IF NOT EXISTS idx_strategies_default_one
    ON strategies (is_default) WHERE is_default = TRUE;

-- Reuse the existing updated_at trigger fn if present
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'update_updated_at') THEN
        IF NOT EXISTS (
            SELECT 1 FROM pg_trigger WHERE tgname = 'trg_strategies_updated_at'
        ) THEN
            CREATE TRIGGER trg_strategies_updated_at
                BEFORE UPDATE ON strategies
                FOR EACH ROW EXECUTE FUNCTION update_updated_at();
        END IF;
    END IF;
END $$;

-- Seed ICT — must exist before any FK backfill below
INSERT INTO strategies (name, display_name, description, class_path, is_default)
VALUES
    ('ict',
     'Inner Circle Trader',
     'Raid + displacement + iFVG/OB with multi-timeframe confirmation',
     'strategy.ict_strategy.ICTStrategy',
     TRUE)
ON CONFLICT (name) DO NOTHING;


-- ── 2. trades.strategy_id ──────────────────────────────────

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS strategy_id INT
    REFERENCES strategies(strategy_id);

-- Backfill every existing trade to ICT
UPDATE trades SET strategy_id = (SELECT strategy_id FROM strategies WHERE name = 'ict')
WHERE strategy_id IS NULL;

-- Now lock it down
ALTER TABLE trades ALTER COLUMN strategy_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy_id);


-- ── 3. tickers.strategy_id ────────────────────────────────

ALTER TABLE tickers
    ADD COLUMN IF NOT EXISTS strategy_id INT
    REFERENCES strategies(strategy_id);

UPDATE tickers SET strategy_id = (SELECT strategy_id FROM strategies WHERE name = 'ict')
WHERE strategy_id IS NULL;

ALTER TABLE tickers ALTER COLUMN strategy_id SET NOT NULL;

-- Replace the old unique(symbol) with unique(symbol, strategy_id)
ALTER TABLE tickers DROP CONSTRAINT IF EXISTS tickers_symbol_key;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uniq_ticker_per_strategy'
    ) THEN
        ALTER TABLE tickers
            ADD CONSTRAINT uniq_ticker_per_strategy UNIQUE (symbol, strategy_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_tickers_strategy ON tickers(strategy_id);


-- ── 4. settings.strategy_id ───────────────────────────────

-- NULL = global (infra/account/broker), non-NULL = per-strategy override
ALTER TABLE settings
    ADD COLUMN IF NOT EXISTS strategy_id INT
    REFERENCES strategies(strategy_id);

-- Classify existing rows into strategy-scoped vs. global.
-- Rule: anything that changes trading behaviour per-strategy is ICT-scoped.
-- Everything else (account, broker, infra) stays global (NULL).
UPDATE settings
SET strategy_id = (SELECT strategy_id FROM strategies WHERE name = 'ict')
WHERE strategy_id IS NULL
  AND key IN (
      'PROFIT_TARGET',
      'STOP_LOSS',
      'ROLL_ENABLED',
      'ROLL_THRESHOLD',
      'TP_TO_TRAIL',
      'MAX_ALERTS_PER_DAY',
      'TRADE_WINDOW_START',
      'TRADE_WINDOW_END',
      'COOLDOWN_MINUTES',
      'CONTRACTS',
      'NEWS_BUFFER_MIN',
      'USE_SHORT_STRATEGY'
  );

-- Replace the old unique(key) with unique(key, strategy_id).
-- NULL-s are treated as distinct so the global row and the ICT row can
-- coexist for the same key — exactly the overlay behaviour we want.
ALTER TABLE settings DROP CONSTRAINT IF EXISTS settings_key_key;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uniq_setting_per_scope'
    ) THEN
        ALTER TABLE settings
            ADD CONSTRAINT uniq_setting_per_scope UNIQUE (key, strategy_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_settings_strategy ON settings(strategy_id);


-- ── 5. ACTIVE_STRATEGY global setting ─────────────────────

INSERT INTO settings
    (category, key, value, data_type, description, is_secret, strategy_id)
VALUES
    ('strategy', 'ACTIVE_STRATEGY', 'ict', 'string',
     'Which strategy the bot runs. Change requires bot restart.',
     FALSE, NULL)
ON CONFLICT (key, strategy_id) DO NOTHING;


-- ── 6. Verification (fails the transaction if anything is off) ──

DO $$
DECLARE
    ict_id  INT;
    cnt_no  INT;
    cnt_ok  INT;
BEGIN
    SELECT strategy_id INTO ict_id FROM strategies WHERE name = 'ict';
    IF ict_id IS NULL THEN
        RAISE EXCEPTION 'ICT strategy row not present after migration';
    END IF;

    SELECT COUNT(*) INTO cnt_no FROM trades WHERE strategy_id IS NULL;
    IF cnt_no > 0 THEN
        RAISE EXCEPTION '% trades rows still have NULL strategy_id', cnt_no;
    END IF;

    SELECT COUNT(*) INTO cnt_no FROM tickers WHERE strategy_id IS NULL;
    IF cnt_no > 0 THEN
        RAISE EXCEPTION '% tickers rows still have NULL strategy_id', cnt_no;
    END IF;

    -- Settings allows NULL (global), so just verify the ACTIVE_STRATEGY key exists
    SELECT COUNT(*) INTO cnt_ok FROM settings WHERE key = 'ACTIVE_STRATEGY';
    IF cnt_ok <> 1 THEN
        RAISE EXCEPTION 'ACTIVE_STRATEGY setting row is missing or duplicated';
    END IF;

    RAISE NOTICE 'Active-strategy migration OK — ICT is strategy_id=%', ict_id;
END $$;

COMMIT;
