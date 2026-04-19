# Roadmap Schema Extensions — Forward-Compatible Preparation

**Status:** 🟢 Approved Apr 19 2026. Implement now on `feature/enh-024-strategy-plugins`.

## The principle

> **Schema is a shared resource, code is not.**
>
> Git handles divergent code across branches. The database cannot — there is
> exactly one Postgres instance, one set of tables. If every feature branch
> also carries its own DDL, merges become coupled to schema migrations, and
> running branch A's code on a database prepared for branch B breaks.
>
> The fix: **extend the schema once, with defaults that preserve today's
> behavior**, for every roadmap feature we can anticipate. Feature branches
> then carry **only code**, and merging them is always clean.

Database objects *can* be version-controlled (we already do — `db/*.sql`
files are in git). The real constraint is that a single live database can
only hold one schema version at a time. Forward-compatible columns — added
with defaults matching today's behavior — sidestep that constraint entirely.

---

## What's in scope (implemented now)

### On `trades`, `backtest_trades`, `tickers`

Columns supporting non-equity-options trading without touching today's paths:

| Column | Type | Default | Purpose |
|---|---|---|---|
| `sec_type` | VARCHAR(5) NOT NULL | `'OPT'` | OPT (equity option) \| FOP (futures option) \| STK \| FUT \| BAG (combo) |
| `multiplier` | INT NOT NULL | `100` | Contract multiplier (100 for equity options; varies for FOP — MNQ=20, ES=50, etc.) |
| `exchange` | VARCHAR(20) NOT NULL | `'SMART'` | Routing exchange (CME/NYMEX/etc. for FOP) |
| `currency` | VARCHAR(5) NOT NULL | `'USD'` | |
| `underlying` | VARCHAR(20) NULL | `NULL` | Underlying symbol (derived from OCC if NULL) |

### On `trades` and `backtest_trades` only

| Column | Type | Default | Purpose |
|---|---|---|---|
| `strategy_config` | JSONB NOT NULL | `'{}'` | Snapshot of strategy params at trade time — so historical trades preserve the exact tuning that produced them. Distinct from `trades.signal_type` (what fired) and `strategy_id` (which strategy). |

### Pre-seeded strategy rows (all `enabled=FALSE`)

| strategy_id | name | status | class_path |
|---|---|---|---|
| 1 | `ict` | enabled, default | `strategy.ict_strategy.ICTStrategy` (already present) |
| — | `orb` | **disabled** | `strategy.orb_strategy.ORBStrategy` |
| — | `vwap_revert` | **disabled** | `strategy.vwap_strategy.VWAPStrategy` |
| — | `delta_neutral` | **disabled** | `strategy.delta_neutral_strategy.DeltaNeutralStrategy` |

The disabled rows are visible in the Strategies dropdown but cannot be
selected at bot-start or backtest-launch until their code lands and they're
flipped enabled.

### Seed adjustments on `tickers`

Every existing ticker row backfilled explicitly with:
- `sec_type = 'OPT'`
- `multiplier = 100`
- `exchange = 'SMART'`
- `currency = 'USD'`

Exactly the values that were implicit before. Nothing about ticker behavior
changes.

---

## What's explicitly deferred

### Multi-leg strategies (trade_legs table) — NOT in this commit

Delta-neutral iron condors need a 1-to-many trades→legs structure. The
design we want is straightforward:

> Every trade has ≥1 row in `trade_legs`. Single-leg trades (today's ICT,
> future ORB/VWAP) have exactly one leg. Multi-leg trades (iron condor = 4,
> vertical spread = 2) have N legs.

That's option A from the earlier discussion — clean, generic, one-to-many.

**But** we're deferring it because:
1. The weekend already landed 9 commits. One more big schema migration is
   unnecessary right now.
2. Delta-neutral comes after Monday's live validation and after ORB/VWAP
   work — there's no urgency.
3. Backfilling 254 existing trades with a single entry-leg row each is
   non-trivial to get exactly right; worth a focused session with testing.

A follow-up design doc (`docs/multi_leg_trades_design.md`) will spec the
`trade_legs` table, the migration, and the query refactors needed to use
it uniformly. No code changes until that doc is approved.

### Multi-strategy concurrent execution

Already covered by the deferred `docs/multi_strategy_data_model.md`. No
further schema work here.

---

## The migration

Single idempotent file: `db/roadmap_schema.sql`. Wrapped in one transaction
with a verification block at the end.

```sql
BEGIN;

-- trades.sec_type / multiplier / exchange / currency / underlying
ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS sec_type   VARCHAR(5)  NOT NULL DEFAULT 'OPT',
    ADD COLUMN IF NOT EXISTS multiplier INT         NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS exchange   VARCHAR(20) NOT NULL DEFAULT 'SMART',
    ADD COLUMN IF NOT EXISTS currency   VARCHAR(5)  NOT NULL DEFAULT 'USD',
    ADD COLUMN IF NOT EXISTS underlying VARCHAR(20);

-- trades.strategy_config (snapshot of tuning at trade time)
ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS strategy_config JSONB NOT NULL DEFAULT '{}';

-- Same additions on backtest_trades
ALTER TABLE backtest_trades
    ADD COLUMN IF NOT EXISTS sec_type        VARCHAR(5)  NOT NULL DEFAULT 'OPT',
    ADD COLUMN IF NOT EXISTS multiplier      INT         NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS exchange        VARCHAR(20) NOT NULL DEFAULT 'SMART',
    ADD COLUMN IF NOT EXISTS currency        VARCHAR(5)  NOT NULL DEFAULT 'USD',
    ADD COLUMN IF NOT EXISTS underlying      VARCHAR(20),
    ADD COLUMN IF NOT EXISTS strategy_config JSONB NOT NULL DEFAULT '{}';

-- tickers gets the same security-type columns
ALTER TABLE tickers
    ADD COLUMN IF NOT EXISTS sec_type   VARCHAR(5)  NOT NULL DEFAULT 'OPT',
    ADD COLUMN IF NOT EXISTS multiplier INT         NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS exchange   VARCHAR(20) NOT NULL DEFAULT 'SMART',
    ADD COLUMN IF NOT EXISTS currency   VARCHAR(5)  NOT NULL DEFAULT 'USD';

-- Pre-seed ORB / VWAP / delta-neutral as DISABLED placeholder rows
INSERT INTO strategies (name, display_name, description, class_path, enabled) VALUES
    ('orb',          'Opening Range Breakout',     'Trades breakouts of the first N minutes',
     'strategy.orb_strategy.ORBStrategy',          FALSE),
    ('vwap_revert',  'VWAP Mean Reversion',        'Mean reversion to session VWAP',
     'strategy.vwap_strategy.VWAPStrategy',        FALSE),
    ('delta_neutral','Delta-Neutral Iron Condor',  'Multi-leg iron condor targeting 0.15 delta wings',
     'strategy.delta_neutral_strategy.DeltaNeutralStrategy', FALSE)
ON CONFLICT (name) DO NOTHING;

-- Verification
DO $$
DECLARE
    missing INT;
BEGIN
    SELECT COUNT(*) INTO missing FROM information_schema.columns
    WHERE table_name = 'trades' AND column_name IN
        ('sec_type','multiplier','exchange','currency','underlying','strategy_config');
    IF missing <> 6 THEN
        RAISE EXCEPTION 'trades table missing one of the new columns';
    END IF;
    -- (similar checks for backtest_trades and tickers)
END $$;

COMMIT;
```

---

## Why this preserves current behavior

Every new column has a default matching today's implicit assumption:
- All existing trades are equity options → `sec_type='OPT'`, `multiplier=100`
- All existing IB orders route SMART → `exchange='SMART'`
- All existing trades are USD → `currency='USD'`
- All existing trades were produced with the live config → `strategy_config={}` (empty snapshot is fine; we'll populate it going forward)

The live bot keeps running. The existing 164 tests pass unchanged. No query
anywhere in the codebase currently reads these columns, so there's nothing
to update.

---

## Code changes that ride along

Minimal — the migration is the heavy lift. Code deltas:

1. **ORM models (`db/models.py`):** add the new columns to `Trade`, `Ticker`,
   `BacktestTrade` so `session.add()` roundtrips them.
2. **`db/writer.py::insert_trade`:** pass `strategy_config` through if the
   caller provides it. All other columns rely on DB defaults.
3. **New tests in `tests/integration/test_roadmap_schema.py`:** schema-shape
   checks, default-value checks, pre-seeded strategy rows present, round-trip
   of a trade with sec_type='FOP' to prove the columns are usable when the
   caller actually cares.

No changes to live scanner, exit manager, reconciliation, or any dashboard
route — those stay ignorant of the new columns until a feature branch
wires them in.

---

## Per-feature branch workflow after this ships

| Branch | Starts from | Purpose | Schema changes? |
|---|---|---|---|
| `feature/orb-live` | enh-024 + this commit | Wire ORB plugin into scanner; enable 'orb' row; backtest & paper-trade | None — just `UPDATE strategies SET enabled=TRUE WHERE name='orb'` |
| `feature/vwap-revert` | enh-024 + this commit | Implement `strategy/vwap_strategy.py`; enable 'vwap_revert' row | Same — one flag flip |
| `feature/futures-options` | enh-024 + this commit | Extend `data_provider.py` for yfinance futures; extend `broker/ib_client.py` for FOP qualification; insert FOP ticker rows with correct multipliers | No DDL — uses columns we added here |
| `feature/delta-neutral` | After multi-leg design doc + DDL | Implement iron condor + populate `trade_legs` | Separate DDL commit (the deferred one) |

Each of the first three is **code + tests + data seeds only**. Zero DDL at
merge time.

---

## Testing plan (per CLAUDE.md principle)

New file `tests/integration/test_roadmap_schema.py`:

**Schema shape tests**
- Each new column exists with correct type + nullability + default
- Pre-seeded strategies (orb / vwap_revert / delta_neutral) present, all `enabled=FALSE`
- The ICT row is still `enabled=TRUE, is_default=TRUE`

**Default-value tests**
- A new trade inserted without specifying sec_type/multiplier/exchange gets
  'OPT' / 100 / 'SMART'
- strategy_config defaults to `{}` and accepts arbitrary JSONB

**FOP round-trip**
- Insert a trade with `sec_type='FOP'`, `multiplier=20`, `exchange='CME'`,
  `underlying='MNQ'` and verify every field round-trips correctly. Proves
  the columns are actually usable, not just shape-correct.

**Ticker seeding**
- Every existing ticker got `sec_type='OPT'`, `multiplier=100`, `exchange='SMART'`, `currency='USD'` on the backfill

**Regression gate**
- Full `pytest tests/` must stay green. Expected: 164 prior + new tests (~8
  new), all passing.

---

## Rollback

Reversible in one SQL script:

```sql
ALTER TABLE trades
    DROP COLUMN IF EXISTS sec_type,
    DROP COLUMN IF EXISTS multiplier,
    DROP COLUMN IF EXISTS exchange,
    DROP COLUMN IF EXISTS currency,
    DROP COLUMN IF EXISTS underlying,
    DROP COLUMN IF EXISTS strategy_config;
-- (same for backtest_trades and tickers)

DELETE FROM strategies WHERE name IN ('orb', 'vwap_revert', 'delta_neutral');
```

Preserved because the columns are additive and nothing reads them yet.

---

## What this unblocks

After this commit lands:

✅ Any feature branch can populate `sec_type`, `multiplier`, `exchange`,
   `currency`, `underlying` on trades without DDL

✅ Any feature branch can capture its tuning via `strategy_config` JSONB

✅ Any feature branch can flip a strategy's `enabled` flag and start
   backtesting/live-trading that strategy (ORB / VWAP / delta-neutral)

✅ Monday's merges stay safe — the schema preparation is strictly additive

❌ Multi-leg strategies still need their own future DDL commit (deferred
   by design)

---

## Sequencing

1. **Right now** — this commit. DDL applied to local Postgres + ORM +
   insert_trade + tests + regression. Commit + push on enh-024.
2. **Tonight/tomorrow** — branch off for ORB / VWAP / futures-options work.
3. **Monday** — validate arch-003 live. Merge arch-003 → dashboard.
4. **Post-Monday** — rebase enh-024 on dashboard, merge. Then merge the
   per-strategy branches one at a time as each validates.
