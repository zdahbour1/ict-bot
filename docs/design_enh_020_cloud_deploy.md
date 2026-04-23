# ENH-020 Design — Cloud Deployment (multi-tenant-ready)

Status: Draft
Date: 2026-04-23
Owner: platform

## 1. Goals

- Run the stack anywhere; remove the hard dependency on the user's Windows host.
- Be multi-tenant ready from day one: each user gets their own IB Gateway + own bot container, backed by a shared dashboard and a shared (RLS-isolated) Postgres.
- Use managed Postgres, managed secrets, managed TLS. No hand-rolled infra primitives.
- Keep per-tenant monthly cost in the $5-15 range for compute so that a small SaaS price point is viable.

Non-goals: multiplexing a single IB account across users (impossible cleanly), Windows containers, building a bespoke orchestrator.

## 2. Current state and where each piece moves

Today (docker-compose on Windows host):
- postgres, api, frontend, pgadmin run in Docker.
- The bot runs on the host (not containerized) so it can reach IB TWS on localhost.
- Single user, no auth.

Target placement:
- DB -> managed Postgres (Neon primary choice; RDS if AWS-committed).
- API + frontend -> stateless app service (Fly Machines, Cloud Run, or ECS Fargate). Shared across all tenants, auth-gated, RLS filters by `tenant_id`.
- Bot + IB Gateway -> **per-tenant container pair** on Fly.io Machines. Pay-per-second billing makes idle tenants cheap.
- Secrets -> Fly secrets (phase 1) with an upgrade path to Vault or AWS Secrets Manager.
- TLS + edge -> Cloudflare in front of Fly.

ARCH-004 CI gap is a dependency: we need push-to-deploy before Phase 2.

## 3. Architecture options

| Option | Per-tenant IB GW | Per-tenant bot | Shared API/FE | DB | Approx $/tenant/mo |
|---|---|---|---|---|---|
| A. Per-tenant full stack | yes | yes | yes (auth) | shared, RLS | $8-12 |
| B. Per-tenant bot, shared dashboard | yes | yes | yes (RLS + WebSocket fan-out) | shared, RLS | $8-12 |
| C. Bot-as-a-service farm | **no** (infeasible) | shared pool | yes | shared | n/a |

Option C is dead on arrival: IB does not offer a clean multi-account multiplex on one Gateway session, and sub-account FA structures require an umbrella account the operator does not own. Ruled out.

A and B are nearly identical. The difference is purely frontend: B commits to one multi-tenant Next.js app with row-level security; A would let us ship isolated dashboards per tenant. B is cheaper to operate and is the pick.

**Decision: Option B.**

## 4. Recommended stack

- **Compute for bot + IB Gateway:** Fly.io Machines, one app per tenant (`bot-<tenantid>`, `ibgw-<tenantid>`) or one app with two Machines. Fly Machines start in ~1s, billed per second, have persistent volumes, and support private IPv6 networking between apps. Hetzner Cloud is the fallback if Fly pricing shifts.
- **Compute for API + FE:** Fly Machines in the same org (simpler networking) or Cloud Run if we want serverless scale-to-zero for the API. Start on Fly, reconsider at ~50 tenants.
- **DB:** Neon. Branching gives us free ephemeral dev DBs; free tier absorbs the first handful of tenants; $19/mo Pro tier is the next step. Row-level security on every tenant-scoped table.
- **Secrets:** Fly secrets for container env injection. IB credentials also stored encrypted (Fernet) in Postgres so the bot can pull per-run TOTP seeds. Long-term: Vault or AWS Secrets Manager.
- **Edge:** Cloudflare for TLS, WAF, DDoS, and WebSocket proxying to the dashboard.
- **IB Gateway image:** `gnzsnz/ib-gateway` (or fork). Runs Xvfb + VNC + IBC for automated login and daily re-login handling. Needs the user's TOTP seed (or manual daily login for live accounts that refuse seeded 2FA).

## 5. Per-tenant cost estimate

Fly.io pricing as of early 2026 (shared-cpu-1x):

| Component | Size | Always-on? | Est $/mo |
|---|---|---|---|
| IB Gateway container | 512 MB, 1 shared vCPU | yes (trading hours) | $3-5 |
| Python bot container | 512 MB, 1 shared vCPU | yes (trading hours) | $3-5 |
| Volume (logs, IBC state) | 1 GB | yes | $0.15 |
| Shared API/FE | amortized across N tenants | - | <$1 at N>=10 |
| Shared Neon DB | free up to ~5 tenants; $19 Pro thereafter | - | $0 - $2 |
| Cloudflare | free tier | - | $0 |
| **Total per tenant** | | | **~$7-13** |

Scheduled stop during off-hours (weekends, overnight) trims ~40% if we accept that the bot is not monitoring overnight positions. The "24/7 Gateway mode" paid IB feature is the alternative.

## 6. Migration plan

1. **Dockerize the bot.** Dockerfile.bot + socket to a sidecar `ib-gateway` container locally. Removes the Windows-host coupling. *Blocker for everything else.*
2. **Single-tenant Fly POC.** One Fly app running the bot + IB Gateway pair, plus the existing API/FE pointed at a Neon dev branch. Paper account only.
3. **Managed Postgres migration.** Dump/restore to Neon main. Switch API to Neon. Keep local compose DB for dev.
4. **Second tenant.** Spin up `bot-tenant2` + `ibgw-tenant2` by hand. Prove isolation: strategies, trades, logs, IB creds. Validate RLS with an adversarial test.
5. **Signup -> provision.** Tenant signup flow calls Fly Machines API to create the two Machines, writes secrets, records `tenant_id -> machine_id` mapping. Requires ENH-018 auth already landed.
6. **Billing.** Stripe + metering hook if going commercial. Tie plan tier to Machine size.
7. **Decommission local host.** Author-user becomes tenant #1. Windows host kept only for development.

Phases 1-2 are prerequisite to everything. Phase 5 is the hard one.

## 7. Security model

- TLS terminated at Cloudflare; origin is Fly with its own cert. HSTS on.
- Private networking between API and per-tenant bot over Fly's 6PN. mTLS on that channel using short-lived certs minted per tenant.
- Auth: session cookies for the browser, signed short-lived JWTs for API -> bot calls that carry `tenant_id`.
- Secrets never in plain env files. IB credentials at rest: Fernet-encrypted column with KMS-backed key (option: AWS KMS, Fly doesn't have native KMS yet). TOTP seed stored the same way.
- RLS on every tenant-scoped table; policy asserts `tenant_id = current_setting('app.tenant_id')::uuid`. Set per request in the API.
- Audit log table, append-only, for trade actions and auth events.
- Network egress: outbound to IB from a known Fly region; live accounts may need a static egress IP — Fly dedicated IPv4 covers that.

## 8. Dev-prod parity

- `docker-compose.yml` stays the local dev entry point. A new compose profile starts a local `gnzsnz/ib-gateway` container so devs don't need TWS installed.
- Optional: Tilt config for a k8s-lite local experience if we ever move off Fly.
- CI (GitHub Actions) builds both images on PR, deploys to a Fly staging app on merge to main, runs smoke tests against paper IB. Production promotion is manual, tagged releases.

## 9. File changes estimate

- `Dockerfile.bot` — new. Python 3.12 slim, installs ib_insync and project deps.
- `docker/ib-gateway/` — new. Fork or vendor a `gnzsnz/ib-gateway` config with our IBC overrides.
- `fly.toml` per service (api, frontend, bot, ibgw) — new.
- `scripts/deploy_tenant.sh` — new. Wraps `flyctl machine run` with our defaults.
- `infra/terraform/` — optional, Phase 6. Covers Neon project, Cloudflare zone, Fly org bootstrap.
- `api/tenancy/` — new package. RLS helpers, tenant resolver middleware, Fly Machines provisioning client.
- DB migration — add `tenant_id` to every user-scoped table, backfill, enable RLS policies.

## 10. Effort

- Phase 1-2 (single-tenant cloud POC): **3-5 days**
- Phase 3-4 (second tenant, prove isolation): **1-2 weeks**
- Phase 5-7 (commercial-ready signup + billing): **4-8 weeks**

## 11. Open questions

- **Bring-your-own-IB vs managed sub-accounts.** BYO-IB is far simpler legally and operationally; assume BYO for v1.
- **24/7 IB Gateway licensing.** Needed only if we want overnight position monitoring / exits. Defer.
- **Paper vs live policy.** Paper only during beta? Explicit opt-in for live with a signed waiver.
- **Region strategy.** IB data centers are US/EU/HK; we should pin each tenant's Fly Machine to the closest region to minimize order latency.
- **Log retention and PII.** Trade logs contain user financial data; need retention policy and export/delete endpoints for GDPR.
- **Fly.io vendor lock-in.** Mitigated by keeping everything containerized and IaC-described; swap target would be Hetzner + Nomad or ECS Fargate.
