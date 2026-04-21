-- Migration 005: add client_trade_id for IB↔DB correlation via orderRef
-- See docs/ib_db_correlation.md
--
-- Format stored: TICKER-YYMMDD-NN  (e.g., "INTC-260421-01")
-- Same string appears in IB Order.orderRef on the parent BUY +
-- TP LMT + SL STP children of the bracket, and in TWS's "Order Ref"
-- column.
--
-- Run with:
--   docker exec ict-bot-postgres-1 psql -U ict_bot -d ict_bot -f /tmp/005.sql
-- OR just paste the body into psql.

ALTER TABLE trades
  ADD COLUMN IF NOT EXISTS client_trade_id VARCHAR(20);

-- Partial unique index: NULL values don't collide (old rows from
-- before this feature stay NULL forever; new rows must be unique).
CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_client_trade_id
  ON trades(client_trade_id)
  WHERE client_trade_id IS NOT NULL;

COMMENT ON COLUMN trades.client_trade_id IS
  'Human-readable correlation ID tagged on IB Order.orderRef for
   every bracket leg. Format TICKER-YYMMDD-NN. Generated at entry
   time; NULL for pre-feature rows.';
