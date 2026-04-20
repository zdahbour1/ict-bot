# RESTART_PROMPT.md — Current Session Context

**Keep this file always-updated. Every commit refreshes it.**
Claude restarts by reading this file; the user's prompt is fixed and
points here.

---

## Last updated

**Apr 20 2026 — Analytics click → filter runs table**
Latest commit: `dcd5ef7` on `feature/profitability-research`

User feedback: "when I click or filter I would like it to filter the
table below so I can see the details".

Changes:
  - Clicking any Analytics chart bar (strategy / ticker / ticker×strategy)
    now FILTERS THE RUNS TABLE below instead of popping a modal with
    raw trades. Modal drill-down still available for individual runs
    (click a run row or Top 15 Runs chart bar).
  - Filter chips shown above the runs table with [×] per chip +
    "Clear all". Page auto-scrolls to the runs table when a chart is
    clicked so the filtered result is visible immediately.
  - Filters persist across chart clicks (clicking a strategy keeps an
    existing ticker filter; they AND together).
  - Analytics-view tables (By Strategy / By Ticker / Ticker×Strategy)
    also filter on row click — hint text added.
  - Backend: `/api/backtests` accepts `?strategy=<name>&ticker=<sym>`.
    Ticker uses Postgres `:ticker = ANY(r.tickers)` for array
    membership. Both AND-join with existing filters.
  - Tests: +4 filter cases ⇒ **380 passed + 4 skips**.

Prior: `d7087b1` — Analytics charts + unified modal + server-side sort

This iteration:
  - Replaced Analytics tables with click-to-drill bar charts (recharts).
    Four charts: P&L by Strategy · Top 20 Tickers · Top 20 (Ticker×Strategy) ·
    Top 15 Runs. Plus a stat row (total P&L, trades, win%, best strategy/ticker).
    Click any bar → opens the unified drill-down modal.
  - Extracted `TradesModal` component — SAME modal for every drill-down
    entry point (run row click, analytics chart click, analytics table
    row click). Consistent header, filter bar, close behavior.
  - **Server-side sort** on all paginated endpoints (user request).
    Clicking a column header now re-queries the DB ordered by that column
    across the ENTIRE dataset, not just the loaded page.
    Whitelisted columns via `_RUNS_SORT_COLS` / `_TRADES_SORT_COLS`;
    unknown keys fall back to default (safe against SQL injection).
  - New endpoint `GET /api/backtests/analytics/trades` — cross-run trade
    drill-down with compound filters (strategy + ticker + run_id +
    outcome) + sort/pagination. Powers chart drill-downs.
  - Fixed route-ordering bug: `/backtests/analytics/trades` was being
    caught by `/backtests/{run_id}/trades` because FastAPI matches by
    registration order. Moved analytics routes above the `{run_id}` ones.
  - Tests: +12 (analytics trades + server-side sort). **376 passed + 4 skips.**

Prior: `7fbbd00` — Cross-run Analytics panel + API (tables only)

New this session:
  - `GET /api/backtests/analytics/cross_run` aggregates across all completed runs.
    Returns by_ticker_strategy, by_strategy, by_ticker, top/bottom_runs.
    Optional strategy_id + status filters; limit_runs default 500.
  - `AnalyticsPanel` component on Backtest page: collapsible section above
    runs table with 4 views (Ticker×Strategy, By Strategy, By Ticker,
    Top/Bottom Runs). Uses existing ColDef<T>+useSortableFilterable hook
    so every column sorts and filters. Click a run ID in Top/Bottom Runs
    to open its drill-down modal.
  - 7 new integration tests in test_backtest_api.py ⇒ **364 passed + 4 skips**.

Current DB state (proof endpoint works): 138 runs · 15,593 trades aggregated.
  By strategy: ICT +$44,995 (60 runs) · ORB +$31,732 (51 runs) · VWAP +$4,777 (27 runs)
  Top ticker/strategy combos:
    QQQ/orb +$18,755 · INTC/ict +$6,013 · SLV/ict +$5,775 · NVDA/orb +$4,753
    TSLA/ict +$4,746 · MU/ict +$4,631 · PLTR/ict +$4,425 · AMD/ict +$4,097

Prior: `8e256cc` — All-ticker sweeps + sortable/filterable UI + modal drill-down

All-ticker per-strategy sweep results (5m bars, 60 days, BS pricer, SL=0.8 for ORB):
  ICT  top 5: INTC +$6,013 · SLV +$5,775 · MU +$4,631 · PLTR +$4,425 · AMD +$4,097
  ORB  top 5: INTC +$3,776 · AMD +$3,661 · NFLX +$3,616 · NVDA +$1,870 · MU +$994
  VWAP top 5: NFLX +$2,846 · TSLA +$689 · SLV +$575 · INTC +$527 · META +$462
              (VWAP fires rarely — ~10 trades/ticker vs ICT ~200)

Common winners across all 3 strategies: INTC, AMD, NFLX, SLV.
IWM is a loser across ICT + ORB. SPX/DJI/NDX/RUT produce 0 trades
(yfinance doesn't resolve those symbols to option chains).

Fixes this session:
  - Drill-down root cause: runs table uncapped → trades panel rendered
    below the fold. Converted to centered modal with sortable+filterable columns.
  - Sweep bug: was passing strategy_id but not a strategy instance,
    so non-ICT sweeps silently ran ICT. Caught when VWAP numbers
    matched ICT exactly. Fixed in backtest_engine/sweep.py.
  - Exit enrichment CONFIRMED captured (15,591/15,591 trades have
    non-empty exit_indicators — UI just wasn't surfacing it).

Still to do (user's open asks):
  - Feature-importance: correlate entry/exit indicators vs WIN/LOSS
    (per-run endpoint exists; need cross-run version + UI)
  - Sweep launch UI form
  - FOP backtest with user-supplied contract (data provider works)
  - Agents for optimization (user suggestion)

Prior: `c14dd1c` BS pricer made ICT + ORB profitable on 3-ticker sets.
ORB parameter sweep found SL=0.8 any PT → +$1,493 (+15% over default).

Backtest page rewritten simple per user request: refresh button + runs
table + trades on row-click. No polling, no auto-select, no charts.

Use `python run_sweep.py <json>` to run more sweeps. Example payload in
run_sweep.py docstring.

Still to ship on this branch:
  - Parameter sweep framework (find optimal PT/SL combos, especially
    for VWAP which still loses)
  - Per-ticker breakdown
  - Longer backtests (yfinance 1h goes back 2 years)

Previous work landed on parallel branches:
  - feature/fop-data-provider (80f3b51)  IB historical data + UI drill-down defensive rewrite
  - feature/active-strategy-ui          Strategies tab, bot-stuck heal, drill-down fixes
  - feature/futures-options             FOP contract foundation
  - feature/vwap-revert, feature/orb-live — strategy plugins

Drill-down UI still broken per user (confirmed in incognito + Edge).
Defensive rewrite with console diagnostics on feature/fop-data-provider
(commit 80f3b51) — awaits user browser evidence.

## ⚠️ KNOWN ISSUE: backtest drill-down UI

Trades can be queried directly from Postgres:
  `SELECT * FROM backtest_trades WHERE run_id = <id> ORDER BY entry_time;`
  `SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT 20;`

Full JSONB enrichment:
  `SELECT entry_indicators, signal_details FROM backtest_trades WHERE id = <n>;`

API endpoints are working (verified via curl — the UI pagination code
is correct). Most likely remaining cause is some browser-specific cache
state that's not clearing. Re-visit after profitability work.

Chain of fixes on the backtest drill-down bug (all three were needed):
  1. `0d69e86` — Dockerfile.api missing backtest_engine module (caused 500)
  2. `e980b2c` — 663KB payload + 10s polling (added pagination + /trades endpoint)
  3. `d7087b1` — nginx cached index.html indefinitely (browsers loaded OLD bundle
                 even after rebuilds; "still not working" for the user)

Lesson for future: for SPA deployments, index.html MUST have
Cache-Control: no-cache headers. The hashed /assets/*.js bundles CAN
cache forever (content → hash), but the entry HTML must always be
revalidated or users pin to stale code.

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

- **380 passed + 4 expected skips** as of latest commit
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
