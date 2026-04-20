# RESTART_PROMPT.md — Current Session Context

**Keep this file always-updated. Every commit refreshes it.**
Claude restarts by reading this file; the user's prompt is fixed and
points here.

---

## Last updated

**Apr 19 2026 — post-backtest-drilldown fix**
Latest commit on current branch: **`(pending this commit)`** on `feature/active-strategy-ui`
("Fix: Dockerfile.api missing backtest_engine module — /api/backtests/{id} was 500ing")

---

## All branches on origin (most recent first)

| Branch | Head | What it contains | Status |
|---|---|---|---|
| `feature/active-strategy-ui` | `dbc0eaa` | Strategies tab UI + auto-activate + bot-stuck auto-heal | **current working branch** |
| `feature/futures-options` | `9800d21` | FOP foundation (contract helpers, seeded tickers, research doc) | merge-ready |
| `feature/vwap-revert` | pushed | VWAP Mean Reversion end-to-end | merge-ready |
| `feature/orb-live` | pushed | ORB Breakout end-to-end | merge-ready |
| `feature/enh-024-strategy-plugins` | pushed | Plugin framework + rollout #1 + backtest framework + roadmap DDL | merge-ready |
| `feature/arch-003-ib-client-split` | pushed | ib_client mixin split | **awaits Monday live test** |
| `feature/dashboard` | pushed | Main target — tests + concurrency + Run Tests | merge-ready |
| `feature/enh-019-backtest` | pushed | Superseded by enh-024 — delete after merge |

## Running stack on user's machine

- Postgres container: all schemas applied (strategies, backtest_runs/trades,
  trades/tickers/settings with roadmap columns, test_runs). 4 strategies
  seeded (ict enabled+default, orb+vwap_revert enabled, delta_neutral
  disabled). ACTIVE_STRATEGY currently 'ict'.
- API container (ict-bot-api-1): built from `feature/active-strategy-ui`
  worktree, serves all /api/* routes including /api/strategies,
  /api/backtests, /api/test-runs.
- Frontend container: built from the same worktree, serves the dashboard
  at http://localhost with tabs: Trades / Analytics / Backtest /
  Strategies / Threads / Tickers / Tests / Settings.
- bot_manager sidecar: running on port 9000 with endpoints /start /stop
  /status /run-tests /run-backtest. Restarted after the bot-stuck fix
  so it has the _heal_db_on_exit hook.
- Bot process: NOT running. TWS needs to be open for it to start.

## Test suite

- **271 passed + 3 expected skips** as of latest commit
- Run: `DATABASE_URL="postgresql://ict_bot:ict_bot_dev@localhost:5432/ict_bot" python -m pytest tests/ -q`
- DB-persistent runs: `PYTEST_DB_REPORT=1 ...` then view at Tests tab

## What the user has approved me to do autonomously right now

Two parallel branches — work on whichever has momentum:

### Branch 1: `feature/profitability-research`
**Goal:** actually find a profitable strategy (user's core concern).

Milestones:
1. Replace the crude 5× leverage option-P&L proxy in
   `backtest_engine/engine.py` with Black-Scholes (or at minimum a
   delta-based proxy that decays with time). Current proxy almost
   certainly mis-models real option behavior on short holds.
2. Parameter grid-search framework: new `/api/backtests/sweep` endpoint
   + runner + UI. Runs ~20 variants across
   (profit_target × stop_loss × trade_window × ticker subset) and
   charts results.
3. Longer backtest windows (yfinance 1h goes back ~2 years) with
   per-ticker performance breakdowns so user sees "ORB wins on SPY,
   loses on AAPL" type patterns.
4. Report actual numbers after each commit. Don't oversell — if nothing
   profitable emerges, say so and propose what else to try.

### Branch 2: `feature/fop-data-provider`
**Goal:** make FOP backtests actually runnable.

Milestones:
1. New `backtest_engine/data_provider_ib.py` that calls IB
   `reqHistoricalData` for FOP contracts. Parquet caching like
   yfinance path.
2. Engine dispatches provider by `ticker.sec_type` (OPT → yfinance,
   FOP → IB).
3. Run first real backtest on MNQ or MES (micros — paper-trade safe).
4. Requires TWS open for end-to-end validation; build everything
   that works without TWS first and flag the gap clearly.

## Working principles (CLAUDE.md — must follow)

- ARCH-001..006 (DB source of truth, row-level locking, single close/open authority)
- **"Test every feature"** — every commit ships tests
- **"Run regression every unit of work"** — 251 tests must stay green
- **Design-first** for any live-code or schema change
- **After every commit, update this file (`RESTART_PROMPT.md`)** so the
  restart prompt stays accurate

## Queued (not started, not blocking)

- Rollout #4 (live scanner plugin wiring): switch live ICT→ORB→VWAP at
  bot-start. Requires scanner-level surgery, isolated in its own branch.
- Rollout #7 (bot-start dialog strategy picker): tiny commit once #4 is done.
- Delta-neutral + `trade_legs` table design doc + DDL + implementation.
- Cloud deployment doc → implementation.
- Authentication/2FA.
- iOS mobile app.

## Monday priority

**Validate `feature/arch-003-ib-client-split` live.** User opens TWS,
restarts bot on that branch, follows `MONDAY_CHECKLIST.md` on the
arch-003 worktree. If green → merge chain as proposed in
`SESSION_STATUS.md`.

---

## The fixed restart prompt (for the user to paste)

```
Load C:\src\trading\ict-bot-strategies\RESTART_PROMPT.md  in full,
then C:\src\trading\ict-bot\.claude\CLAUDE.md  for the arch principles.

Use TodoWrite to restore the queue from the "What the user has approved
me to do autonomously" section. Continue from the latest commit noted
at the top. Always update RESTART_PROMPT.md after every commit.
```

## Three-way sanity check (for Claude on restart)

Before touching anything, confirm:
1. `cd /c/src/trading/ict-bot-strategies` — am I on the expected branch?
2. `python -m pytest tests/ -q` with the DATABASE_URL env — does the
   expected test count still pass?
3. `docker ps --format '{{.Names}}'` — all containers up?
