# Active-Strategy Design — One Strategy at a Time, Real Foundation

**Status:** 🟢 Proposed — replaces near-term implementation plan.
**Supersedes (near term):** `docs/multi_strategy_data_model.md` is deferred
as the long-term concurrent-strategy target. This doc is what we actually
build now.

---

## 1. Goal

Let the bot support multiple strategies (ICT, ORB, VWAP, …) but run
**exactly one at a time**. To switch strategies you stop the bot,
pick the new one from a dropdown, and start the bot again. Identical
constraint for backtest: one strategy per run.

**Why:** concurrent execution has hidden complexity (per-strategy risk
caps, exposure, reconciliation attribution, dashboard sprawl). We defer
that. But we build the **data-model foundation** now so when we're
ready for concurrent, most of the schema work is already done.

---

## 2. What changes (and what doesn't)

### ✅ Changes

- New `strategies` table — one row per defined strategy
- New FK `strategy_id` on `trades`, `tickers`, `settings`
- New setting `ACTIVE_STRATEGY` — picks which strategy boots
- Bot start prompts for strategy (default = current active)
- Dashboard shows active strategy in nav; Settings/Tickers tabs
  become strategy-scoped
- Backtest runs pick a `strategy_id` from the strategies table

### ❌ Does NOT change

- ARCH-005 `_atomic_close()` — unchanged
- ARCH-006 "one open per (ticker, conId)" — unchanged (still holds
  because only one strategy opens trades at a time)
- `strategy/exit_manager.py`, `exit_conditions.py`, `reconciliation.py`
  — unchanged logic; they just **record** `strategy_id` alongside
  what they already record
- `broker/ib_client.py` and the mixin modules — unchanged
- The "one Scanner per ticker" rule stays in force

No logic rewrites. Just normalized record-keeping plus a boot-time
strategy selection.

---

## 3. The `strategies` table

```sql
CREATE TABLE strategies (
    strategy_id   SERIAL PRIMARY KEY,           -- the user-facing ID used in FKs
    name          VARCHAR(30) NOT NULL UNIQUE,  -- 'ict', 'orb', 'vwap_revert'
    display_name  VARCHAR(80) NOT NULL,         -- 'Inner Circle Trader'
    description   TEXT,
    class_path    VARCHAR(200) NOT NULL,        -- 'strategy.ict_strategy.ICTStrategy'
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    is_default    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_strategies_default_one
    ON strategies(is_default) WHERE is_default = TRUE;  -- at most one default
```

**Seed data — ICT is strategy_id=1, explicitly (not implied):**

```sql
INSERT INTO strategies (name, display_name, class_path, is_default) VALUES
    ('ict', 'Inner Circle Trader',
     'strategy.ict_strategy.ICTStrategy', TRUE);
```

**Note on `strategy_id`:** You asked for a user-facing sequence column
that FKs reference. That's exactly what `SERIAL PRIMARY KEY` gives us.
Using it as both the PK and the FK target is simpler than a separate
internal/external ID pair, and still gives you stable integer IDs that
survive renames.

---

## 4. Strategy-scoped FKs

### 4.1 `trades` — audit only

```sql
ALTER TABLE trades
    ADD COLUMN strategy_id INT REFERENCES strategies(strategy_id);

CREATE INDEX idx_trades_strategy ON trades(strategy_id);

-- Backfill: every existing trade is ICT
UPDATE trades SET strategy_id = 1 WHERE strategy_id IS NULL;
ALTER TABLE trades ALTER COLUMN strategy_id SET NOT NULL;
```

Every trade is stamped with the strategy that produced it. Pure audit
column — no existing query logic cares, but the dashboard can filter
and analytics can group-by. When we eventually go concurrent, this
column is already there.

### 4.2 `tickers` — strategy-scoped enable list

```sql
ALTER TABLE tickers
    ADD COLUMN strategy_id INT REFERENCES strategies(strategy_id);

CREATE INDEX idx_tickers_strategy ON tickers(strategy_id);

UPDATE tickers SET strategy_id = 1 WHERE strategy_id IS NULL;
ALTER TABLE tickers ALTER COLUMN strategy_id SET NOT NULL;

-- One ticker can appear once per strategy (QQQ can be ICT-enabled
-- and ORB-enabled as separate rows)
ALTER TABLE tickers DROP CONSTRAINT IF EXISTS tickers_symbol_key;
ALTER TABLE tickers ADD CONSTRAINT uniq_ticker_per_strategy
    UNIQUE (symbol, strategy_id);
```

ORB might want to trade QQQ, SPY, IWM only; ICT wants the full 23.
They live as separate rows. The active strategy's rows are what the
bot loads on startup.

### 4.3 `settings` — global with per-strategy overrides

```sql
ALTER TABLE settings
    ADD COLUMN strategy_id INT REFERENCES strategies(strategy_id);

CREATE INDEX idx_settings_strategy ON settings(strategy_id);

-- NULL strategy_id = global (IB_HOST, DATABASE_URL, etc.)
-- non-NULL = strategy-specific (PROFIT_TARGET, STOP_LOSS, ROLL_THRESHOLD, etc.)

-- Drop the old unique(key), add a composite
ALTER TABLE settings DROP CONSTRAINT IF EXISTS settings_key_key;
ALTER TABLE settings ADD CONSTRAINT uniq_setting_per_scope
    UNIQUE (key, strategy_id);  -- NULL strategy_id sorts as distinct
```

**Resolution order at load time** (in `db/settings_loader.py`):

```
For each setting key K:
  1. Look up the row for K with strategy_id = <active_strategy_id>
  2. If not found, fall back to the row for K with strategy_id = NULL (global)
  3. If still not found, fall back to .env, then code default
```

That way ICT and ORB each have their own `PROFIT_TARGET` / `STOP_LOSS` /
`ROLL_THRESHOLD` / `COOLDOWN_MINUTES` etc., but share `IB_HOST`,
`IB_PORT`, `DATABASE_URL`, email settings — the account/infra stuff.

### 4.4 Settings categorization (seed migration)

When the migration runs, existing settings get classified:

| Category | Goes where |
|---|---|
| `TASTYTRADE_*`, `SCHWAB_*`, `IB_*`, `DATABASE_URL`, `PAPER_TRADING`, `USE_IB`, `DRY_RUN` | Global (`strategy_id = NULL`) |
| `EMAIL_*`, notification keys | Global |
| `PROFIT_TARGET`, `STOP_LOSS`, `ROLL_ENABLED`, `ROLL_THRESHOLD`, `TP_TO_TRAIL`, `MAX_ALERTS_PER_DAY`, `TRADE_WINDOW_*`, `COOLDOWN_MINUTES`, `CONTRACTS`, `NEWS_BUFFER_MIN` | Strategy (`strategy_id = 1` = ICT) |

Rule of thumb: if changing the value would behave differently between
ICT and ORB, it's strategy-scoped. Account/broker/infra is global.

---

## 5. The `ACTIVE_STRATEGY` setting

Global row in the settings table:

```sql
INSERT INTO settings (category, key, value, data_type, description, strategy_id)
VALUES ('strategy', 'ACTIVE_STRATEGY', 'ict', 'string',
        'Which strategy the bot runs. Change requires bot restart.',
        NULL);
```

Stored as the strategy `name` (not the id) so it stays stable if we
ever reset SERIALs. On boot, `main.py` does:

```python
name = config.ACTIVE_STRATEGY      # 'ict' or 'orb' or ...
row  = session.query(Strategy).filter_by(name=name, enabled=True).one()
strategy_id = row.strategy_id
strategy    = StrategyRegistry.instantiate(name)
# Load settings where strategy_id = row.strategy_id OR strategy_id IS NULL
# Load tickers where strategy_id = row.strategy_id
# Hand `strategy` to every Scanner
```

**Changing the active strategy while the bot is running has no effect.**
The change only takes at the next bot start. This is intentional —
matches your "stop → pick → start" mental model and avoids mid-session
surprises.

---

## 6. Replicate-from-existing (when user adds a new strategy)

When the user creates "ORB" in the dashboard, they pick a source
strategy to clone settings + tickers from (default: the current ICT).
One SQL transaction:

```sql
-- Step 1: create the strategy row
INSERT INTO strategies (name, display_name, class_path)
VALUES ('orb', 'Opening Range Breakout',
        'strategy.orb_strategy.ORBStrategy')
RETURNING strategy_id;

-- Step 2: copy settings
INSERT INTO settings
    (category, key, value, data_type, description, is_secret, strategy_id)
SELECT category, key, value, data_type, description, is_secret, :new_id
FROM settings WHERE strategy_id = :source_id;

-- Step 3: copy tickers
INSERT INTO tickers
    (symbol, contracts, enabled, <other cols>, strategy_id)
SELECT symbol, contracts, enabled, <other cols>, :new_id
FROM tickers WHERE strategy_id = :source_id;
```

User can then edit the ORB-scoped settings without affecting ICT.
Exactly what you asked for.

---

## 7. Dashboard changes (UI)

### Nav bar
A compact badge showing the active strategy:
```
ICT Trading Bot     [Strategy: ICT ▾]    ● Trading   ...
```
Click the dropdown → list of enabled strategies → selecting one either
(a) does nothing if bot is stopped (just cosmetic preview), or (b) warns
"Stop the bot first to switch strategies."

### Start-bot dialog
Currently Start Bot is one click. Becomes:
```
┌─ Start Bot ─────────────────────────┐
│  Strategy: [ ICT ▾ ]                 │
│             ICT                      │
│             ORB                      │
│             VWAP Reversion           │
│  Default: ICT                        │
│                                      │
│  [Cancel]   [Start with ICT]         │
└──────────────────────────────────────┘
```
On confirm, POST `/api/bot/start` with `{strategy: "ict"}`. Sidecar
updates `ACTIVE_STRATEGY` setting then spawns the bot.

### Settings tab
Dropdown at the top: "Viewing settings for [ICT ▾]". Below, the usual
settings grid — but scoped to that strategy. A "Global (infra)" section
shows the strategy_id = NULL rows separately.

### Tickers tab
Same pattern — "Tickers for [ICT ▾]" dropdown. Add/remove buttons
scope to the current strategy.

### Trades tab
New "Strategy" column (small colored chip). Strategy filter in the
table toolbar.

### Analytics tab
Existing charts get a "Strategy:" filter. Optional group-by-strategy
view for later.

### Strategies tab (new)
Full CRUD:
```
┌─ Strategies ───────────────────────────────────────────┐
│ ID │ Name  │ Display Name          │ Default │ Enabled │
│  1 │ ict   │ Inner Circle Trader   │   ✓     │   ✓     │
│  2 │ orb   │ Opening Range Breakout│         │   ✓     │
│  3 │ vwap  │ VWAP Reversion        │         │         │
│                                                        │
│ [+ New Strategy]  (clone from: [ICT ▾])                │
└────────────────────────────────────────────────────────┘
```

---

## 8. Backtest integration

`backtest_runs.strategy_id INT NOT NULL REFERENCES strategies(strategy_id)`.

Run-config form has a strategy dropdown. The engine reads
strategy-specific settings + tickers via the same loader the live bot
uses. Comparison view can filter by strategy. When concurrent
strategies eventually arrive, the same backtest infra still works — a
run just targets one strategy ID.

---

## 9. Migration plan (one SQL script)

`db/migrations/003_active_strategy.sql` — idempotent, reversible.

```
1. CREATE TABLE strategies (...)
2. INSERT ICT as strategy_id=1, is_default=TRUE
3. ALTER TABLE trades   ADD strategy_id (nullable), backfill=1, set NOT NULL
4. ALTER TABLE tickers  ADD strategy_id, drop old unique(symbol),
                         add unique(symbol, strategy_id), backfill=1, NOT NULL
5. ALTER TABLE settings ADD strategy_id (nullable — NULL = global),
                         drop old unique(key),
                         add unique(key, strategy_id),
                         classify existing rows per §4.4
6. INSERT ACTIVE_STRATEGY = 'ict' global setting
```

Rollback: drop FKs and the new column in reverse order; `strategies`
table is last to go. ICT-only state is recoverable at every step.

---

## 10. Rollout commits (no big-bang)

Per the CLAUDE.md "test every feature, regression every unit of work"
principle, each commit ships with its own test batch.

| # | Commit | Tests |
|---|---|---|
| 1 | **DDL + backfill** (`003_active_strategy.sql`, `db/models.py` ORM, seed data) | Schema shape tests; ICT row seeded; FKs resolve |
| 2 | **Settings loader** — strategy-scoped reads with global fallback | Resolution order: strategy > global > env > default |
| 3 | **Tickers loader** — strategy-scoped | Correct ticker set loaded for active strategy |
| 4 | **`main.py` wiring** — read ACTIVE_STRATEGY, instantiate via StrategyRegistry, hand to scanners | End-to-end: start bot with `ACTIVE_STRATEGY=ict`; flip to `orb`; restart; bot uses ORB |
| 5 | **Trade writes** — `insert_trade()` stamps `strategy_id` | DB integration: trade row has correct strategy_id |
| 6 | **Dashboard: Strategies tab + strategy-scoped Settings/Tickers tabs** | Frontend component tests where feasible + API route tests |
| 7 | **Bot-start strategy picker** — dialog + sidecar honors `{strategy}` in `/start` payload | Sidecar integration test |
| 8 | **Backtest integration** — `strategy_id` on `backtest_runs`, engine loads strategy-scoped settings | Backtest run with ORB vs. ICT produces different results on same period |

Each commit is independently revertable. The bot keeps trading ICT
correctly at every step — we don't flip to a new strategy until commit
#4 lands and only if `ACTIVE_STRATEGY` is changed.

---

## 11. Testing plan (concrete list)

**Unit tests** (pure, no DB):
- `test_strategy_registry_resolves_by_name` — already exists
- `test_strategy_instantiate_missing_name_returns_none` — already exists
- `test_strategy_scoped_settings_overlay_globals` — new
- `test_active_strategy_setting_parses_cleanly` — new

**Integration tests** (Postgres):
- `test_strategies_table_seeded_with_ict` — new
- `test_insert_trade_stamps_strategy_id` — new
- `test_tickers_scoped_to_active_strategy` — new
- `test_settings_lookup_strategy_then_global` — new
- `test_clone_strategy_copies_settings_and_tickers` — new
- `test_foreign_keys_reject_invalid_strategy_id` — new

**Concurrency tests**:
- `test_clone_strategy_is_transactional` — partial clone failure must
  roll back both settings and tickers inserts
- `test_settings_update_during_clone_no_leak` — an UPDATE on the source
  strategy's settings must not leak into an in-progress clone

**Regression gate:** full `pytest tests/unit/ tests/integration/` run
after each of the 8 rollout commits. Zero tolerance for red.

---

## 12. What this preserves vs. unlocks

### Preserved (zero risk to the live bot)
- Every ARCH-00x invariant
- The entire close flow
- Reconciliation (just gets a free `strategy_id` audit column)
- IB connection pool sharding
- One-open-per-ticker rule (vacuously, since only one strategy runs)

### Unlocked immediately
- Run ORB for a week, ICT the next → compare 1:1 in analytics
- Per-strategy tickers (ORB only on liquid index ETFs, etc.)
- Per-strategy tuning (ORB can have 0.5 SL while ICT keeps 0.6)
- Backtest one strategy at a time with the same config surface
- Clean Strategies CRUD page in the UI

### Foundation for future concurrent work
- `strategies` table → already exists
- `strategy_id` FK on trades, tickers, settings → already exists
- Strategy-scoped settings loader → already exists
- Backtest strategy_id → already exists

When concurrent mode arrives later, the work is:
- Relax "one open per ticker" to "one open per (ticker, strategy_id)"
  via the partial unique index from the deferred doc
- Add `scanner_instances` table for runtime tracking
- Add `ib_orders` table for normalized attribution (optional; can
  keep `ib_*_id` columns on trades for a while longer)

None of that is urgent and none of it fights the design we ship now.

---

## 13. Answers to your specific asks (reconciled)

| You asked | My answer |
|---|---|
| `strategies` table with user-defined `strategy_id` | ✅ `strategy_id SERIAL PRIMARY KEY` — both user-facing and FK target |
| FK reference on `trades.strategy_id` | ✅ §4.1 |
| `strategy_id` on `tickers` and `settings` | ✅ §4.2 + §4.3 |
| Default settings/tickers replicate from ICT for new strategies | ✅ §6 (one transaction, atomic) |
| ICT becomes explicit, not implied | ✅ §3 seed row |
| Existing logic doesn't change, just records `strategy_id` | ✅ §2 "Does NOT change" list |
| Bot start prompts for strategy, default defined | ✅ §7 start-bot dialog |
| Backtest references strategies table | ✅ §8 |
| Global `ACTIVE_STRATEGY`, not per-account (for now) | ✅ §5 |

---

## 14. What I need from you to start

Sign off on the above and I'll:
1. Write `db/migrations/003_active_strategy.sql`
2. Add the ORM models in `db/models.py`
3. Run the migration against your local Postgres
4. Verify ICT is seeded + every existing trade/ticker/setting is
   re-homed under `strategy_id = 1`
5. Regression suite green
6. Commit & push as **rollout commit #1**

No UI or scanner wiring yet — just the foundation. You review, we
proceed to commits #2–#8 one at a time the same way we've been working.

**One last question:** Should the initial DDL also add `strategy_id`
to the new `backtest_runs` / `backtest_trades` tables I proposed on
the ENH-019 branch (currently they carry `strategy_name` VARCHAR)?
My vote: yes, consolidate on the FK. One less thing to migrate later.
