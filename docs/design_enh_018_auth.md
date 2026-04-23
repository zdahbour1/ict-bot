# ENH-018 Design — Authentication + Multi-Tenancy (two-phase)

_Status: Draft v2 (supersedes v1 single-operator-only design). Last updated 2026-04-23._

## 1. Problem statement

The bot is unauthenticated and single-operator: FastAPI trusts loopback, the schema treats `strategies` as global, and one IB Gateway serves the lone `main.py`. Staged work:

- **Phase 1 (v0, days):** login + 2FA + JWT + argon2 for the current operator — prereq for cloud move (`docs/cloud_deployment.md`).
- **Phase 2 (v1, weeks-months):** each user runs _their own_ strategies, settings, tickers, trades, and IB account in the same deployment.

Hard constraint: **Phase 1 must not box us into a single-tenant schema or runtime.** We pay a small tax now (`owner_user_id` column, `current_user` dependency everywhere) so Phase 2 is a migration, not a rewrite.

## 2. Threat model

**Phase 1:** stolen laptop / shoulder-surf → login + 2FA. Port exposed to internet → argon2, rate-limited login, short JWT expiry, HSTS. Log exfiltration → never log tokens or TOTP seeds.

**Phase 2:** IDOR between tenants → `Depends(current_user)` + `WHERE owner_user_id` + Postgres RLS defence-in-depth. IB credential leak across tenants → per-tenant bot process with per-tenant env/secrets. Tenant → admin escalation → explicit `role` column, all admin actions audit-logged, no implicit trust for `user_id=1`. Cross-tenant side channels → namespace all cache keys by tenant; tag logs with `user_id`.

## 3. Two-phase architecture

### Phase 1 — single-user auth (ships first)

Ships: `POST /auth/login` (OAuth2 password flow) → short-lived JWT (15 min) + refresh token (7 d, httpOnly cookie); TOTP 2FA via `pyotp` with encrypted `totp_secret`; `argon2-cffi` via `passlib` (argon2id); `Depends(current_user)` on every non-auth route (WebSocket handshakes too); slowapi rate-limit (5/min/IP) + 15-min lockout after 10 bad passwords.

**Foresighted (Phase 2 tax paid now):** `users` table exists day one; every tenant-scoped table gets `owner_user_id BIGINT NOT NULL DEFAULT 1 REFERENCES users(id)`; `user_id=1` seeded; all reads already written `WHERE owner_user_id = :uid` (always `1` in Phase 1); routes already `Depends(get_current_user)`. Phase 2 becomes a schema migration + more users, not an app rewrite.

### Phase 2 — multi-tenant

Users CRUD + invite flow (admin issues signed invite → user completes enrolment incl. 2FA). Per-tenant IB connections (§5): **one bot process per tenant**. Postgres RLS on tenant-scoped tables — `USING (owner_user_id = current_setting('app.current_user_id')::bigint)`; app sets `SET LOCAL app.current_user_id = :id` per request; app-level `WHERE` filters stay (defence in depth). Onboarding: signup → email verify → 2FA enrol → IB credentials → first strategy. (Stripe billing only if commercial.)

## 4. Schema changes

**Phase 1 migration (Alembic `018_auth_phase1`):**
- New `users` (`id`, `email UNIQUE`, `password_hash`, `totp_secret` (nullable, encrypted), `role in (admin,user)`, `is_active`, `created_at`, `last_login_at`).
- New `refresh_tokens` (`jti`, `user_id`, `expires_at`, `revoked_at`).
- New `audit_log` (`user_id`, `action`, `ip`, `ua`, `ts`, `details jsonb`).
- Add `owner_user_id BIGINT NOT NULL DEFAULT 1 REFERENCES users(id)` to: `strategies`, `settings`, `tickers`, `trades`, `trade_legs`, `delta_hedges`, `backtest_runs`, `thread_status`, `bot_state`, `scans`, `alerts`.
- Seed `INSERT INTO users (id, …) VALUES (1, …)` — the bootstrap operator.
- Index every `owner_user_id` column.

**Phase 2 migration (`018_auth_phase2`):**
- `ALTER COLUMN owner_user_id DROP DEFAULT` on every table.
- `ALTER TABLE … ENABLE ROW LEVEL SECURITY` + `CREATE POLICY tenant_isolation …`.
- Add `invites` (`token_hash`, `issued_by`, `email`, `expires_at`, `consumed_at`).
- Add `ib_credentials` (per-tenant encrypted IB username/password/account — see §11).

## 5. Bot-process model for per-tenant IB

**Option A — one bot process per tenant (recommended):** `scripts/spawn_tenant_bot.py <user_id>` launches a `main.py` with `TENANT_USER_ID=N`, API port `8000+N`, IB client id `100+N`, per-tenant log. Systemd template `bot@.service` or a Docker Compose generator ⇒ one container per tenant. Shared FastAPI control-plane proxies tenant-scoped actions to the tenant's bot over an internal socket. Pros: hard process isolation, blast-radius limited to one tenant, per-tenant IB Gateway works naturally. Cons: ~200–400 MB/tenant idle, N × Gateway containers, sprawl past ~100 tenants.

**Option B — one process multiplexing N IB connections:** one `ib_async.IB()` per tenant in a dict, scanner threads keyed by tenant, shared DB pool. Pros: low RAM, one deploy. Cons: any tenant's slow callback blocks the shared loop; a bug in tenant A can crash the process and take down B; per-tenant hot-reload is much harder. Violates our "Assume Nothing" spirit — shared failure domains are risky.

**Decision: Option A for v1.** Revisit B only at >50 tenants under RAM pressure. A also sidesteps the clientId/Gateway-per-account reality of IB.

## 6. Frontend changes

**Phase 1:**
- `/login` page (email + password + TOTP field shown conditionally after first factor).
- `/settings/security` — change password, enrol/disable 2FA with QR.
- Axios/fetch wrapper attaches `Authorization: Bearer <jwt>`, auto-refreshes on 401 via refresh cookie.
- Logout clears tokens + revokes refresh jti.

**Phase 2:**
- Tenant-scoped dashboard — list views filtered server-side; no UI change for normal users.
- Admin-only tenant switcher / impersonation (writes an audit-log row; impersonation JWT carries `act_as` claim).
- `/account/ib` screen for tenant to enter their own IB credentials.

## 7. File-by-file code impact

**Phase 1 (~600 LOC):**
- New: `app/auth.py` (JWT encode/decode, `get_current_user`), `app/models/user.py`, `app/routes/auth.py`, `app/security/passwords.py` (argon2), `app/security/totp.py`, Alembic `018_auth_phase1.py`, `scripts/create_user.py`, `scripts/reset_password.py` (rescue CLI), `frontend/src/pages/Login.tsx`, `frontend/src/pages/Security.tsx`, `frontend/src/auth/client.ts`.
- Modified: every route under `app/routes/*` gains `current_user: User = Depends(get_current_user)`; every DB helper in `db/writer.py` gains an `owner_user_id` filter (always `current_user.id` in Phase 1).

**Phase 2 (~1500 LOC):**
- New: `app/routes/admin.py`, `app/routes/invites.py`, `scripts/spawn_tenant_bot.py`, `deploy/tenant_compose.py.j2`, RLS Alembic migration, `app/tenancy/ib_credentials.py` (encryption at rest), `frontend/src/pages/admin/*`, tenant-isolation integration tests.
- Modified: scanner startup is per-tenant; `main.py` reads `TENANT_USER_ID`; control-plane routes proxy to the tenant bot.

## 8. Testing strategy

**Phase 1 (`tests/unit/test_auth.py`):**
- Happy path: login → JWT → protected route → 200.
- Wrong password, unknown user, disabled user, expired JWT, tampered JWT → 401.
- TOTP: correct code, wrong code, replay inside the same 30 s window rejected.
- Rate-limit triggers after 5 bad attempts.
- Refresh-token rotation + revocation (stolen-refresh scenario).
- `create_user.py` bootstrap idempotency.

**Phase 2 (`tests/integration/test_tenant_isolation.py`):**
- Two users; each creates strategies, trades, tickers.
- User A calls every GET endpoint with User B's resource ids → 404 (never 403, to avoid id enumeration).
- RLS enforcement: with RLS on, a raw SQL query under user A's GUC cannot SELECT user B's rows even if app-level filters are bypassed.
- Spawn two tenant bots; verify IB client ids don't collide; kill tenant A's bot and confirm tenant B keeps running.

Per CLAUDE.md: every new feature ships with tests and the full `tests/unit/` suite must stay green before we call it done.

## 9. Rollout / migration plan

1. Deploy Phase 1 migration with `AUTH_REQUIRED=false` feature flag — app runs unchanged.
2. `python scripts/create_user.py <email> <pw>` — seeds `user_id=1`, enrols 2FA.
3. Backfill: `UPDATE <table> SET owner_user_id = 1 WHERE owner_user_id IS NULL` (belt-and-suspenders over the DEFAULT).
4. Flip `AUTH_REQUIRED=true`, restart, log in.
5. Monitor one release cycle.
6. When Phase 2 is greenlit: run `018_auth_phase2`, enable RLS, deploy admin UI, announce invites.

## 10. Effort

- **Phase 1:** 2–4 engineer-days — auth plumbing is well-trodden; the long pole is threading `current_user` through every route and re-verifying every test.
- **Phase 2:** 2–3 engineer-weeks — RLS policies, per-tenant spawn, admin UI, IB credential vaulting, isolation test suite.

## 11. Dependencies & risks

**New deps:** `python-jose[cryptography]`, `passlib[argon2]`, `argon2-cffi`, `pyotp`, `python-multipart`, `slowapi` (rate-limit). Frontend: hand-rolled token client is fine.

**Risks:**
- **Operator lockout.** If TOTP device is lost and password forgotten, the app is bricked. Mitigation: `scripts/reset_password.py` runs from host shell with DB creds, bypasses auth, prints recovery codes. Document this prominently.
- **Phase 2 data migration weight.** 10k+ trades backfill is fast, but dropping the `DEFAULT 1` after multi-user writes have begun needs a short write-lock window. Plan a maintenance window.
- **IB credentials at rest.** Storing brokerage creds in the DB is a liability. Encrypt with a KMS-backed key (AWS KMS, age/sops); never log; never return from API.
- **JWT secret rotation.** Plan from day one — use a `kid` header and support two active secrets during rotation.
- **Clock skew breaking TOTP.** Server must NTP-sync; accept ±1 30 s window.

## 12. Open questions

1. **Shared vs per-tenant bot containers at scale?** Recommendation: per-tenant (Option A). Revisit at >50 tenants.
2. **IB credential storage — encrypted DB column vs external secrets manager (Vault / AWS Secrets Manager)?** Lean secrets manager for hosted prod; DB column acceptable for self-hosted. Decision needed before Phase 2 ships.
3. **Admin impersonation — always-on or break-glass?** Recommendation: break-glass, mandatory audit-log reason field.
4. **Commercial / SaaS pricing model?** Out of scope for engineering but shapes billing schema; if likely, reserve a `subscriptions` namespace now.
5. **Single shared Postgres vs schema-per-tenant vs DB-per-tenant?** Phase 2 assumes shared DB + RLS. DB-per-tenant is a later option for enterprise.
6. **WebSocket auth for the live-trade feed — JWT in query string (logged!) or subprotocol header?** Recommendation: subprotocol header with a short-lived ticket fetched via authenticated HTTP.
