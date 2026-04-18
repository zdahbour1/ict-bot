# Multi-Strategy Concurrent Data Model — Design Doc

**Status:** 🟡 Design under review — no code to be written until this is approved.

**Problem:** The current bot assumes **one scanner per ticker**. To run
ICT + ORB (+ future strategies) concurrently on the same ticker, several
invariants and schema rules need to change. This doc is the plan for
how to do that *without* regressing ARCH-001 / ARCH-002 / ARCH-005 /
ARCH-006.

---

## 1. The invariant we have today

> **"Exactly one open trade per ticker."**

It's enforced in `strategy/exit_manager.py:99-121`:

```python
# ARCH-006: Check for existing open trade on same ticker OR same conId
if con_id:
    existing = session.execute(
        text("SELECT id FROM trades WHERE ib_con_id = :cid AND status = 'open' LIMIT 1"),
        {"cid": con_id}).fetchone()
else:
    existing = session.execute(
        text("SELECT id FROM trades WHERE ticker = :ticker AND status = 'open' LIMIT 1"),
        {"ticker": ticker}).fetchone()
...
if existing:
    log.warning(f"[{ticker}] DUPLICATE GUARD: open trade already exists")
    return
```

This check, plus the "one `Scanner` instance per ticker" in `main.py`,
is the sole reason we never run two strategies on the same symbol.

**Why this breaks for multi-strategy:** if ICT is scanning AAPL while
ORB is also scanning AAPL, and they both fire on the same bar, the
second one silently gets dropped. That's a correctness bug, not a
safety feature — we *want* both trades to happen (different setups,
different expirations possibly, definitely different intents).

---

## 2. The new invariant

> **"Exactly one open trade per `(ticker, strategy_name, ib_con_id)`."**

- Two strategies on the same ticker? ✅ Allowed (`strategy_name` differs).
- Same strategy reopening the same option symbol while a trade is
  still open? ❌ Rejected (same `ib_con_id`).
- Two strategies both deciding to buy the same exact contract
  (same conId) at the same time? ❌ Rejected (second one loses the
  race). This edge case is rare but real — see §6.

This preserves ARCH-006 (one open authority) at a finer granularity
without weakening it.

---

## 3. Schema changes

### 3.1 New table: `scanner_instances`

Every running scanner thread gets a row. A row's identity is
`(ticker, strategy_name, config_hash)` — so a second ICT scanner on
AAPL with a different profit_target is a *different* row.

```sql
CREATE TABLE scanner_instances (
    id              SERIAL PRIMARY KEY,
    ticker          VARCHAR(10) NOT NULL,
    strategy_name   VARCHAR(30) NOT NULL,   -- 'ict', 'orb', 'vwap_revert'
    config_hash     VARCHAR(16) NOT NULL,   -- short hash of strategy params
    config          JSONB NOT NULL DEFAULT '{}',  -- frozen snapshot

    -- Runtime
    status          VARCHAR(20) NOT NULL DEFAULT 'idle',  -- idle|scanning|error|stopped
    pid             INT,
    thread_id       BIGINT,
    last_heartbeat  TIMESTAMPTZ,
    scans_today     INT NOT NULL DEFAULT 0,
    trades_today    INT NOT NULL DEFAULT 0,

    -- Lifecycle
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (ticker, strategy_name, config_hash)
);

CREATE INDEX idx_scanner_inst_ticker   ON scanner_instances(ticker);
CREATE INDEX idx_scanner_inst_strategy ON scanner_instances(strategy_name);
CREATE INDEX idx_scanner_inst_enabled  ON scanner_instances(enabled);
```

**Why `config_hash`?** So the same strategy with different tunings can
run side-by-side — e.g. `ORB(range_minutes=15)` and
`ORB(range_minutes=60)` on SPY — and their trades stay distinguishable
for backtest-style comparison.

### 3.2 Changes to `trades`

```sql
ALTER TABLE trades
    ADD COLUMN strategy_name   VARCHAR(30) NOT NULL DEFAULT 'ict',
    ADD COLUMN scanner_id      INT REFERENCES scanner_instances(id),
    ADD COLUMN strategy_params JSONB NOT NULL DEFAULT '{}';

CREATE INDEX idx_trades_strategy ON trades(strategy_name);
CREATE INDEX idx_trades_scanner  ON trades(scanner_id);

-- Replaces the old "one open per ticker" rule with the new invariant
CREATE UNIQUE INDEX idx_trades_open_per_strategy_conid
    ON trades(ticker, strategy_name, ib_con_id)
    WHERE status = 'open' AND ib_con_id IS NOT NULL;
```

That **partial unique index** is the new enforcement mechanism — it
makes duplicate opens impossible at the DB level, not just in a Python
check that can race. Two scanners trying to insert the same
`(ticker, strategy, conId)` while it's open will have exactly one win
and the other get a `duplicate key` error, which `add_trade()` can
catch and treat as "already exists."

### 3.3 Changes to `thread_status` → migrate to `scanner_instances`

`thread_status` is already half-doing this job. I'd keep it populated
for general threads (exit_manager, reconciliation, bot-main) but the
**per-scanner** rows move to `scanner_instances` where they belong.
Dashboard Threads tab becomes a UNION of both.

### 3.4 New table: `ib_orders` (audit trail for fills → trade → scanner)

Today we carry `ib_order_id / ib_perm_id / ib_tp_perm_id / ib_sl_perm_id`
as columns on `trades`. That's four IDs jammed into one row and it's
why reconciliation has to do string-based `symbol in local_sym`
matching. For multi-strategy this gets worse — two scanners can have
their own bracket legs on the same option at the same time.

Proposal: a normalized `ib_orders` table.

```sql
CREATE TABLE ib_orders (
    id              SERIAL PRIMARY KEY,
    trade_id        INT NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    scanner_id      INT REFERENCES scanner_instances(id),
    role            VARCHAR(10) NOT NULL,   -- 'entry' | 'tp' | 'sl'
    ib_order_id     INT,
    ib_perm_id      BIGINT,
    con_id          INT NOT NULL,
    symbol          VARCHAR(40) NOT NULL,
    action          VARCHAR(4) NOT NULL,    -- BUY | SELL
    quantity        INT NOT NULL,
    order_type      VARCHAR(10) NOT NULL,   -- MKT | LMT | STP
    status          VARCHAR(20) NOT NULL DEFAULT 'submitted',
    fill_price      NUMERIC(10,4),
    fill_time       TIMESTAMPTZ,
    submitted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_ib_orders_perm_id ON ib_orders(ib_perm_id)
    WHERE ib_perm_id IS NOT NULL;
CREATE INDEX idx_ib_orders_trade  ON ib_orders(trade_id);
CREATE INDEX idx_ib_orders_conid  ON ib_orders(con_id);
```

**Now reconciliation is a pure `JOIN`:** given an IB position (conId +
qty + avgCost) find the `ib_orders` row by `(con_id, role='entry',
status='filled')`, walk to `trades.id`, walk to `scanner_instances.id`.
No more fuzzy matching. No more "whose trade is this?"

---

## 4. Code changes — summary of touch points

These are the exact files that need edits (not doing them now — just
listing so the scope is visible).

| File | Change | Why |
|---|---|---|
| `strategy/scanner.py` | Constructor takes `strategy: BaseStrategy` not `ticker` only; one `Scanner` per `(ticker, strategy)` | Multiple scanners per ticker |
| `main.py` | Loop over enabled strategies × tickers to instantiate scanners | Boot plumbing |
| `strategy/exit_manager.py::add_trade()` | Dedup check becomes `(ticker, strategy_name, ib_con_id)`. Require `strategy_name` on trade dict. | New invariant |
| `db/writer.py::insert_trade()` | Accept + persist `strategy_name`, `scanner_id`, `strategy_params` | Schema alignment |
| `db/writer.py` | New `record_ib_order()` + `update_ib_order_status()` | Populate `ib_orders` |
| `broker/ib_orders.py` (the split mixin) | `_ib_place_bracket()` returns 3 IB order IDs + the caller writes 3 `ib_orders` rows | Audit trail |
| `strategy/reconciliation.py` | Join-based matching via `ib_orders.con_id` → `trades.id` → `scanner_instances.id` | No more fuzzy matching |
| `dashboard/routes/trades.py` | `?strategy=` filter, include `strategy_name` in JSON | Dashboard filtering |
| `dashboard/routes/analytics.py` | Group-by `strategy_name` across every aggregate | Strategy-level P&L |
| `dashboard/frontend/src/components/TradeTable.tsx` | Strategy column + filter dropdown | UX |
| `dashboard/frontend/src/components/AnalyticsTab.tsx` | Strategy breakdown charts (by-strategy P&L, win rate) | UX |

---

## 5. Boot sequence (what `main.py` actually does after the change)

```python
# Pseudocode
enabled_strategies = load_enabled_strategies_from_settings()  # [ICTStrategy, ORBStrategy, ...]
tickers = load_tickers_from_settings()                        # ['QQQ', 'SPY', 'AAPL', ...]

scanners = []
for strategy_cls in enabled_strategies:
    for ticker in tickers:
        strategy = strategy_cls(ticker=ticker)
        strategy.configure(load_strategy_settings(strategy.name))
        scanner_row = upsert_scanner_instance(
            ticker=ticker,
            strategy_name=strategy.name,
            config_hash=hash_of(strategy.config),
            config=strategy.config,
        )
        scanners.append(Scanner(
            client=ib_client,
            exit_manager=em,
            ticker=ticker,
            strategy=strategy,
            scanner_id=scanner_row.id,
            scan_offset=stagger_seconds,
        ))

for s in scanners:
    s.start()
```

If ICT + ORB are both enabled on 23 tickers, that's 46 scanner threads.
The existing IB connection pool (3 scanner connections) handles the
load via its sharding (`ticker → connection`); we just need to verify
the pool's submit queue doesn't become a bottleneck (it's a thread-
safe `queue.Queue`, so it won't deadlock — but if latency matters we
bump to 5 scanner connections).

---

## 6. Race conditions and their new answers

### 6.1 Two strategies fire on same ticker, same bar, same contract

Before: silent drop (old dedup rule).
After: both try to insert; the partial unique index makes exactly one
win. The loser's `add_trade()` catches the `UniqueViolation` and logs
`DUPLICATE GUARD: another scanner (strategy=X) already opened this
contract`. No code asymmetry — whichever scanner's INSERT commits
first owns the trade.

### 6.2 Reconciliation finds an IB position with no matching `ib_orders` row

Could happen if:
- You placed a trade manually in TWS (now unmatched)
- The bot crashed mid-place and never wrote the `ib_orders` row

The existing "Pass 2: adopt unmatched IB positions" flow stays, but
the adoption has to pick a `scanner_id`. Options:
- **Attribute to a synthetic `scanner_instance` named `manual-adopt`**
  so it shows up in the UI under its own bucket. My preferred choice.
- Fail loud and require human resolution. Safer but slower.

I'll default to the first and make the `manual-adopt` scanner row
always-present (seeded).

### 6.3 Two scanners for SAME strategy, same ticker (different configs)

Allowed by the schema (`config_hash` differs). Both could fire
simultaneously. Different option chains expire at different strikes so
the conIds would differ — independent trades. If they happen to pick
the same option, the unique-index race (§6.1) handles it.

### 6.4 Close flow

ARCH-005 (`lock_trade_for_close`) is already trade-id based, so it
doesn't need to change. One small refinement: the exit manager
currently iterates "open trades by ticker"; after this change it
iterates "open trades by (ticker, strategy)" so each strategy's monitor
loop is independent. That matters for TP-to-trail and rolls which are
strategy-specific.

---

## 7. Migration plan

The migration has to work with the current production DB. Steps:

```
1.  Apply new DDL (idempotent CREATE TABLE / ALTER TABLE).
2.  Backfill: every existing open trade gets strategy_name='ict'
    (every current trade is ICT).
3.  Seed one scanner_instances row per existing (ticker) with
    strategy='ict', backfill trades.scanner_id.
4.  Seed the 'manual-adopt' scanner_instance.
5.  Deploy code that WRITES the new columns but still reads both ways
    (transitional).
6.  Backfill ib_orders from existing trades' ib_*_id columns.
7.  Switch reconciliation to JOIN-based matching.
8.  Delete the old fuzzy-match path.
```

Steps 1-4 are a single SQL migration script. Steps 5-8 are code
commits, each small and independently revertable.

---

## 8. Dashboard changes

### Trades tab
- New **Strategy** column (chip colored by strategy: blue=ICT, green=ORB, yellow=VWAP)
- Strategy filter dropdown at the top

### Analytics tab
- Every chart gets a strategy-breakdown variant
- New card: per-strategy win rate + P&L for the last 30 days
- Compare-strategies view (side-by-side like backtest comparison)

### Threads tab
- Currently 1 row per ticker scanner. After change: 1 row per scanner_instance, grouped by ticker (sub-rows when multiple strategies active)

### Settings tab
- Per-strategy enable/disable toggle (ICT ✓ / ORB ✗ / VWAP ✗)
- Per-strategy config form (ORB range minutes, VWAP RSI period, etc.)

---

## 9. Testing plan (per CLAUDE.md principle)

Every slice ships with tests. Concrete list:

**Unit tests (pure, no DB):**
- `test_scanner_instantiation_with_strategy` — Scanner accepts a BaseStrategy
- `test_scanner_dispatches_to_strategy_detect` — detect() called with right args
- `test_config_hash_stable_for_same_params` — hash is deterministic
- `test_config_hash_differs_for_different_params`

**Integration tests (need Postgres):**
- `test_unique_index_rejects_duplicate_open` — two INSERTs, only one wins
- `test_two_strategies_same_ticker_both_open` — ICT + ORB open on AAPL simultaneously → both rows in DB
- `test_insert_trade_persists_strategy_name` — round-trip
- `test_reconciliation_joins_ib_orders_to_scanner` — pass-1 closure path with strategy attribution
- `test_reconciliation_adopts_orphan_to_manual_scanner` — pass-2 adoption path

**Concurrency tests (threaded + DB):**
- `test_16_threads_race_for_ticker_strategy_conid` — exactly one winner
- `test_two_scanners_race_but_different_conid_both_win` — no cross-interference

---

## 10. Rollout order (proposed, ~4 incremental commits)

1. **DDL-only commit** on a new branch: schema changes + backfill SQL.
   No code changes. Deploy to DB, verify shape, roll back easy.
2. **Write-path commit:** `add_trade`, `insert_trade`, `record_ib_order`
   now write the new columns. Reads still single-strategy. Scanner still
   takes ticker only. Everything keeps working — we're just duplicating
   writes.
3. **Scanner multi-strategy commit:** `Scanner(..., strategy=...)`.
   `main.py` loops strategies × tickers. Enable ORB for 1 ticker in
   settings as the canary.
4. **Reconciliation + dashboard commit:** JOIN-based reconciliation,
   dashboard strategy filters/columns. Retire fuzzy matching.

Each commit gets its own test suite per the CLAUDE.md principle.
Each commit runs the full regression before push.

---

## 11. Open questions for you

1. **Per-strategy position sizing?** ICT uses 2 contracts today. Should
   ORB have its own `contracts` setting? (I assume yes — different
   confidence levels, different sizes.)

2. **Concurrent same-ticker exposure cap?** If ICT and ORB both buy
   AAPL calls, we've got 4 contracts of directional exposure. Want a
   per-ticker cap that sums across strategies?

3. **`manual-adopt` default strategy_name:** OK to use the literal
   string `"manual-adopt"` or prefer `"manual"` / `"unknown"`?

4. **Config hash length:** 16 hex chars (first 8 bytes of SHA-256).
   Plenty of entropy and compact. Want shorter/longer?

5. **Backtest integration (Branch 4):** the `backtest_trades` table
   already has `strategy_name` (I added it in my ENH-019 DDL). Good to
   keep that aligned — same column names across live and backtest.

Answer these four in any order and I'll finalize the design + start
on the DDL commit.
