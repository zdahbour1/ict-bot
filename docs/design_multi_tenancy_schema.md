# Design — Multi-tenancy schema migration (foundational)

**Status:** Proposed — companion to ENH-018 (auth) and ENH-020 (cloud)
**Blocking item for:** every future tenant-scoped feature

## Purpose

The user's stated direction is multi-tenancy: each user with their
own strategies, trades, settings, and their own IB brokerage account.
The app-layer auth design (ENH-018) assumes a hidden foundation: a
`user_id`-scoped database schema. This doc defines that schema
migration as a **single, reversible, coordinated change** that every
other tenant-aware feature can rely on.

**Ship this before any multi-tenant code lands.** Otherwise every
subsequent migration has to re-hash the same decisions.

## 1. Scope

**In scope:**
- Add `users` table + authentication primitives
- Add `owner_user_id` FK column to every tenant-scoped table,
  defaulting to `1` so existing rows stay readable
- Backfill scripts
- PostgreSQL row-level security (RLS) policies as belt-and-suspenders
  on top of app-layer filtering
- Tenant-aware views / materialized views

**Out of scope:**
- Frontend tenant switcher
- Per-tenant bot process spawning (ENH-020)
- Admin / impersonation flow

## 2. Tenant-scoped tables (audit)

Current tables and the `owner_user_id` decision:

| Table | Scope | Needs FK? | Notes |
|-------|-------|-----------|-------|
| `trades` | per-user | ✅ | Primary tenant data |
| `trade_legs` | joined to trades | ➖ | Inherits via FK to trades |
| `delta_hedges` | joined to trades | ➖ | Same |
| `strategies` | per-user | ✅ | Each user has own strategy set |
| `tickers` | per-user | ✅ | Each user picks their universe |
| `settings` | per-user | ✅ | Already (key, strategy_id); add user_id |
| `backtest_runs` | per-user | ✅ | Each tenant's own backtests |
| `backtest_trades` | joined to runs | ➖ | Inherits |
| `backtest_trade_legs` | joined to trades | ➖ | Same |
| `thread_status` | per-user | ✅ | Per-tenant bot has own threads |
| `system_log` | per-user | ✅ | Scope logs per tenant |
| `errors` | per-user | ✅ | Per-tenant error feed |
| `bot_state` | per-user | ✅ | Each tenant's bot has own state |
| `test_results` / `test_runs` | shared dev infra | ❌ | Stay global |
| `iv_daily` (new, ENH-052) | shared market data | ❌ | Ticker data is not tenant-specific |
| `macro_events` (new, ENH-056) | shared market data | ❌ | Same |
| `ticker_events` (new, ENH-056) | shared market data | ❌ | Same |

## 3. Migration `016_multi_tenancy_foundation.sql`

Large migration. Recommend splitting into:
- `016a_users_table.sql` — add `users` table + seed user_id=1
- `016b_owner_user_id_columns.sql` — add FK columns default 1
- `016c_indexes_and_rls.sql` — add tenant-scoped indexes + RLS

### 016a — users table

```sql
CREATE TABLE IF NOT EXISTS users (
    id                SERIAL PRIMARY KEY,
    email             VARCHAR(255) UNIQUE NOT NULL,
    password_hash     VARCHAR(200) NOT NULL,    -- argon2id
    totp_secret       VARCHAR(64),              -- set on 2FA enroll
    totp_enrolled_at  TIMESTAMPTZ,
    display_name      VARCHAR(100),
    is_admin          BOOLEAN NOT NULL DEFAULT false,
    is_active         BOOLEAN NOT NULL DEFAULT true,
    failed_attempts   INTEGER NOT NULL DEFAULT 0,
    locked_until      TIMESTAMPTZ,
    last_login_at     TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed user_id=1 as the current operator (bootstrap)
INSERT INTO users (id, email, password_hash, display_name, is_admin)
VALUES (1, 'bootstrap@local', '!placeholder-disabled', 'Operator', true)
ON CONFLICT (id) DO NOTHING;

-- Reserve id=1 forever for the single-operator path
SELECT setval('users_id_seq', GREATEST(1, (SELECT MAX(id) FROM users)));
```

### 016b — owner_user_id columns

Pattern repeated for each tenant-scoped table:

```sql
ALTER TABLE trades
  ADD COLUMN IF NOT EXISTS owner_user_id INTEGER NOT NULL DEFAULT 1
    REFERENCES users(id);
CREATE INDEX IF NOT EXISTS idx_trades_owner_status
  ON trades (owner_user_id, status);

-- Same for:
--   strategies, tickers, settings, backtest_runs, thread_status,
--   system_log, errors, bot_state
```

Every existing row inherits `owner_user_id = 1` via the DEFAULT clause.

### 016c — RLS policies (defense in depth)

```sql
ALTER TABLE trades ENABLE ROW LEVEL SECURITY;

-- Policy: each session must set app.current_user_id GUC; rows
-- are visible only when owner_user_id matches.
CREATE POLICY trades_tenant_isolation ON trades
  USING (owner_user_id = current_setting('app.current_user_id',
         true)::INTEGER)
  WITH CHECK (owner_user_id = current_setting('app.current_user_id',
         true)::INTEGER);

-- The bot connects as a `bot_role` that has BYPASSRLS (already
-- filters by user_id in application code; RLS is web-tier protection).
GRANT ALL ON trades TO bot_role;
ALTER ROLE bot_role BYPASSRLS;

-- The web tier connects as `web_role` that obeys RLS.
```

Repeat for every tenant-scoped table. ~15 policies total.

## 4. App-layer plumbing

The RLS GUC is set by a FastAPI dependency:

```python
# dashboard/deps.py
def with_tenant(current_user = Depends(current_user)):
    session = get_session()
    session.execute(text("SELECT set_config('app.current_user_id', :uid, false)"),
                    {"uid": str(current_user.id)})
    try:
        yield session
    finally:
        session.close()

# In every route:
@router.get("/trades")
def list_trades(session = Depends(with_tenant)): ...
```

Bot stays unchanged (runs as `bot_role` with BYPASSRLS and filters
explicitly by `user_id` in its queries — per-tenant bot means per-tenant
`user_id` constant, so every query is `WHERE owner_user_id = :uid`).

## 5. Backfill + validation

Before phase-1 ships:

1. Run migration — every existing row gets `owner_user_id=1`.
2. Verify: `SELECT COUNT(*) FROM trades WHERE owner_user_id IS NULL`
   returns 0 on each tenant-scoped table.
3. Create real user via `scripts/create_user.py` (from ENH-018).
4. Update user_id=1 rows to the real user: `UPDATE … SET
   owner_user_id = :real_id WHERE owner_user_id = 1`.
5. Drop DEFAULT after backfill completes so future rows can't silently
   become user_id=1.

## 6. Breaking changes & how to avoid them

| Change | Breaking? | Mitigation |
|--------|-----------|-----------|
| New `owner_user_id` column | NO (default 1) | All existing queries still work |
| New RLS policies | Yes for web tier | Ship RLS disabled initially; enable per-table after queries tested |
| `strategies.name` no longer unique alone | Possible | Change unique to `(name, owner_user_id)` |
| `settings(key, strategy_id)` unique | Possible | Change to `(key, strategy_id, owner_user_id)` |

Migration carefully sequences these so app downtime stays ≤ 10 s.

## 7. File impact

- `db/migrations/016a/b/c_*.sql` — new
- `db/models.py` — add `owner_user_id` on 8 models
- `db/writer.py` — every insert sets `owner_user_id=ctx.user.id`
- `dashboard/deps.py` — new file: `with_tenant` dependency
- `dashboard/routes/*` — add `Depends(with_tenant)` to every list
  route (auto-fail-safe: if dev forgets, RLS blocks the query)
- `scripts/migrate_users.py` — backfill script

Estimated: **~500 LOC** changes across 30+ files.

## 8. Testing

- `tests/integration/test_multi_tenancy_isolation.py` — two users,
  verify user A can't see user B's trades in any route (15 cases)
- `tests/unit/test_with_tenant_dep.py` — dependency sets GUC correctly
- `tests/unit/test_writer_stamps_owner.py` — every insert captures
  owner_user_id
- Regression: full existing 500-test suite must still pass with
  migration applied

## 9. Rollback

- **Before RLS enabled**: trivial — migrations are additive.
  `ALTER TABLE … DROP COLUMN owner_user_id CASCADE`.
- **After RLS enabled**: drop policies first, then columns.
- **After populated multi-user**: not reversible without data loss.
  Final point-of-no-return is step 4 of §5 (backfill).

## 10. Effort

| Milestone | Effort |
|-----------|--------|
| Write migrations 016a/b/c | 1 day |
| Update ORM + writer paths | 1 day |
| Add `with_tenant` dependency + wire every route | 1 day |
| Integration tests (isolation) | 1 day |
| Run on paper — single-user still, validate no regression | 1 day |
| Eventually: add second real user + validate isolation | 0.5 day |
| **Total** | **5-6 days** |

## 11. Rollout sequence (tied to ENH-018)

1. Ship `016a/b` (schema only, RLS disabled). **No user-facing change.**
2. Ship ENH-018 phase 1 auth (single user). Works on top of new schema.
3. Ship `016c` (RLS policies). Web tier now tenant-isolated at DB level.
4. Ship ENH-020 cloud single-tenant deployment using new schema.
5. Enable user registration / 2nd tenant.
6. Repeat §5 backfill step for the 2nd tenant.

## 12. Open questions

1. **Shared market-data tables** (`iv_daily`, `macro_events`,
   `ticker_events`): keep global. But should the read API be
   authenticated anyway? — Yes, just not tenant-filtered.
2. **Cross-tenant analytics** for the admin: needs a
   `SET LOCAL row_security = off` escape hatch on the admin role.
3. **Schema-per-tenant vs row-per-tenant**: we chose row-per-tenant
   for ops simplicity. Schema-per-tenant would isolate more strongly
   but is painful to migrate (run every migration N times).
   Revisit if we hit Postgres row-level bottlenecks ~10k tenants.
4. **Backtest data sharing**: if users want to share a backtest
   run publicly (read-only), need a `public_runs` view.
