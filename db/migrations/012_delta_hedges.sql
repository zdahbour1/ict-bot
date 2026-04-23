-- Migration 012 — ENH-049 Stage 1+2: Delta-hedge audit + per-trade counter
-- Every stock rebalance done by the delta-hedger thread records a row
-- here so the user can replay the hedging chain for any DN trade.
BEGIN;

-- Track running hedge-shares on the envelope so the bot knows the
-- current stock delta offset without re-summing all hedge events.
-- Positive = long shares; negative = short shares.
ALTER TABLE trades
  ADD COLUMN IF NOT EXISTS hedge_shares INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS delta_hedges (
    id                 INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trade_id           INTEGER NOT NULL
                         REFERENCES trades(id) ON DELETE CASCADE,
    ticker             VARCHAR(10) NOT NULL,
    action             VARCHAR(4)  NOT NULL
                         CHECK (action IN ('BUY','SELL')),
    shares             INTEGER     NOT NULL,
    fill_price         NUMERIC(10,4),
    order_id           INTEGER,
    net_delta_before   NUMERIC(12,4),
    error              TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_delta_hedges_trade
  ON delta_hedges(trade_id, created_at DESC);

-- Seed the flags with sensible defaults so the UI shows them
-- immediately. Bot reads via db.settings_cache.
INSERT INTO settings (category, key, value, data_type, description, strategy_id)
VALUES
  ('strategy', 'DN_DELTA_HEDGE_ENABLED', 'false', 'bool',
   'Enable the 30s delta-hedging loop for delta-neutral trades (ENH-049). When on, buys/sells stock to flatten net option delta.',
   91),
  ('strategy', 'DN_REBALANCE_INTERVAL_SEC', '30', 'int',
   'Seconds between delta-hedge rebalance passes.',
   91),
  ('strategy', 'DN_DELTA_BAND_SHARES', '20', 'int',
   'Rebalance only when absolute net share-equivalent delta exceeds this band. Avoids churn for tiny drifts.',
   91)
ON CONFLICT (key, strategy_id) DO NOTHING;

COMMIT;
