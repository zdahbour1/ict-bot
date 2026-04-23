# Design — ARCH-004 residual: GitHub Actions CI

**Status:** Proposed — non-urgent while single-contributor
**Audit source:** 2026-04-23 ARCH audit identified this as the only
residual gap on the 6 ARCH principles. All others shipped.

## Purpose

Regression suite (500 tests) is enforced by manual discipline today.
Once a second contributor is onboarded or cloud deployment starts
gating on merge, CI becomes blocking. This doc defines a minimal CI
pipeline that fails PRs on broken tests and surfaces regressions.

## 1. Scope

**In scope:**
- `tests/unit/` run on every push + PR (500 tests, ~20 s)
- Lint + format check (basic, no custom rules)
- Docker image build smoke test

**Out of scope for v1:**
- Full `tests/integration/` — some require Postgres + IB. Defer to
  scheduled nightly run.
- Frontend `vitest` / `playwright` — separate workflow if/when we
  add UI regression tests.
- Security scanning / SAST — add in Phase 3 before cloud.

## 2. Files

Single workflow at `.github/workflows/ci.yml`:

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_USER: ict_bot
          POSTGRES_PASSWORD: ict_bot_dev
          POSTGRES_DB: ict_bot
        ports: [5432:5432]
        options: >-
          --health-cmd pg_isready --health-interval 10s
          --health-timeout 5s --health-retries 5
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: '3.14'}
      - run: pip install -r requirements.txt -r requirements-test.txt
      - run: psql -h localhost -U ict_bot -d ict_bot -f db/init.sql
        env: {PGPASSWORD: ict_bot_dev}
      - run: for m in db/migrations/*.sql; do
          psql -h localhost -U ict_bot -d ict_bot -f "$m";
        done
        env: {PGPASSWORD: ict_bot_dev}
      - run: python -m pytest tests/unit/ -q --timeout 30
        env:
          DATABASE_URL: postgresql://ict_bot:ict_bot_dev@localhost:5432/ict_bot

  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: '3.14'}
      - run: pip install ruff
      - run: ruff check .

  docker-build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker compose build api frontend

  integration-nightly:
    if: github.event_name == 'schedule'
    runs-on: ubuntu-latest
    # ... (scheduled; includes tests/integration/)
```

Plus:
- `.github/workflows/integration-nightly.yml` cron `'0 6 * * *'` UTC
  that runs the full suite + tags a nightly snapshot
- `ruff.toml` — lint config (start permissive; no custom rules)

## 3. Branch protection

GitHub → Settings → Branches → `main`:
- Require status checks to pass: `unit-tests`, `lint`, `docker-build`
- Require up-to-date branch before merge
- Do NOT require code review yet (single contributor) — add when
  multi-tenant requires second pair of eyes.

## 4. Secrets

None needed for unit tests (no IB calls; all mocked).

Future (when integration tests run live):
- `IB_PAPER_USERNAME` / `IB_PAPER_PASSWORD` — stored as GH secrets
- `DATABASE_URL_STAGING` — points at managed staging db

## 5. Effort

| Piece | Effort |
|-------|--------|
| Write ci.yml + ruff.toml | 1 hr |
| First-run debug (migration ordering, timezone, etc.) | 2-3 hr |
| Branch protection config | 15 min |
| Fix any lint violations uncovered | 1-3 hr (depends on current state) |
| **Total** | **Half-day** |

## 6. Rollout

1. Merge ci.yml to `main` — first run will tell us which tests
   actually need a live DB vs pure mocks.
2. Fix any CI-only failures (usually timezone / path-style / env
   var differences).
3. Enable branch protection once 3 successful runs on `main`.
4. Add status badge to `README.md`.

## 7. Cost

GitHub Actions is free for public repos; ~2000 min/month for private.
Each unit run is ~1 min; 100 pushes/month = 100 min → free tier.
Docker build adds ~1 min. Total: comfortably free.

## 8. Open questions

1. Move `tests/integration/` to nightly or keep them per-PR with
   a `services: postgres` block? — Recommend nightly, because
   some require IB paper API access which can't run in CI.
2. Do we want auto-format on commit (pre-commit hooks)? — Optional,
   nice-to-have when multi-contributor.
3. Should the `main` → `v2026.04.23-stable` tag be validated by
   CI (e.g. "tags pass all tests + docker smoke")?

## 9. Dependencies

- None for v1 (GH Actions is free, all tooling already in repo)
- Future: needs `requirements-test.txt` to stay current; today it
  has pytest + pytest-cov + pytest-timeout, which should be enough.
