-- 010_strategy_supported_ticker_types.sql
--
-- New table `strategy_supported_ticker_types` — declares which
-- instrument types each strategy can trade. Used by the Tickers tab's
-- Add-Ticker dropdown so users can only pick a sec_type the selected
-- strategy actually supports. Also used for API-level validation on
-- ticker insert to reject shape mismatches early.
--
-- Seed data per docs/multi_strategy_architecture_v2.md + each
-- strategy's current code paths:
--   ICT          — OPT, FOP     (equity options + futures options)
--   ORB          — OPT, FOP
--   VWAP         — OPT, FOP
--   delta_neutral — OPT         (iron condors on equities; FOP support
--                                 requires multi-leg FOP which isn't wired)

BEGIN;

CREATE TABLE IF NOT EXISTS strategy_supported_ticker_types (
    id            INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    strategy_id   INTEGER NOT NULL
                   REFERENCES strategies(strategy_id) ON DELETE CASCADE,
    sec_type      VARCHAR(5) NOT NULL
                   CHECK (sec_type IN ('OPT', 'FOP', 'STK', 'FUT', 'BAG')),
    notes         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (strategy_id, sec_type)
);

CREATE INDEX IF NOT EXISTS idx_strategy_supported_types
    ON strategy_supported_ticker_types(strategy_id);

COMMENT ON TABLE strategy_supported_ticker_types IS
    'Allow-list of instrument types (OPT, FOP, STK, FUT, BAG) each '
    'strategy can trade. Tickers tab UI restricts the Add-Ticker '
    'sec_type dropdown to these rows. API rejects ticker inserts for '
    'unsupported types.';

-- Seed: resolve each strategy by short name (robust to strategy_id churn).
INSERT INTO strategy_supported_ticker_types (strategy_id, sec_type, notes)
SELECT s.strategy_id, 'OPT',
       'Equity options — ATM 0DTE calls/puts via option_selector.'
  FROM strategies s
 WHERE s.name IN ('ict', 'orb', 'vwap_revert', 'delta_neutral')
ON CONFLICT (strategy_id, sec_type) DO NOTHING;

INSERT INTO strategy_supported_ticker_types (strategy_id, sec_type, notes)
SELECT s.strategy_id, 'FOP',
       'Futures options via fop_selector (ENH-034). Liquidity-gated '
       'quarterly/monthly/weekly preference.'
  FROM strategies s
 WHERE s.name IN ('ict', 'orb', 'vwap_revert')
ON CONFLICT (strategy_id, sec_type) DO NOTHING;

-- delta_neutral gets OPT only for now; FOP iron-condor wiring deferred.
-- (Row for OPT already inserted above via the first INSERT.)

COMMIT;
