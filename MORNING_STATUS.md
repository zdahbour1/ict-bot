# Morning Status — Backtest Framework Complete

**All work is committed, pushed, deployed, and end-to-end verified.**

## TL;DR — What to do first

1. Open **http://localhost** in your browser
2. Click the new **Backtest** tab in the nav bar (between Analytics and Threads)
3. You'll see **run #83** already there: 320 trades, 48% win rate, -$854 P&L,
   produced by the ICT strategy across QQQ + SPY over the last 60 days
4. Click the row to drill in — summary cards, cumulative P&L chart,
   P&L by ticker, exit reasons, day-of-week, and the **Feature Analysis**
   panel showing which entry indicators (RSI, VWAP distance, ATR, etc.)
   correlate with winning vs. losing trades
5. Click **+ Run Backtest** to launch a new one — pick strategy, tickers,
   date range, profit target / stop loss, interval (1m/5m/15m/1h)
6. Delete run #79 and #80 (the two empty smoke-test runs) whenever you like

## What's new since you went to sleep

### Branch: feature/enh-024-strategy-plugins

7 commits shipped, all pushed, all tested, all deployed. In order:

| # | Commit | What |
|---|---|---|
| 1 | `ad2be1d` | **Rollout #1** — `strategies` table + `strategy_id` FK on trades/tickers/settings + insert_trade auto-stamp + 24 new tests |
| 2 | `14dc807` | **Backtest A** — DDL (`backtest_runs` + `backtest_trades` with 4 JSONB enrichment cols) + ORM + metrics + fill_model + 18 unit + 9 integration tests |
| 3 | `547074c` | **Backtest B** — Engine (reuses live SignalEngine + evaluate_exit) + data provider (yfinance + parquet cache) + indicators + 10 tests |
| 4 | `83e9db7` | **Backtest C** — 5 API routes + sidecar `/run-backtest` + `run_backtest_engine.py` CLI + 17 integration tests |
| 5 | `50693d7` | **Backtest D** — BacktestTab UI (~500 LOC React) with LaunchDialog, run-list, drill-down, 4 charts, **Feature Analysis panel** |
| 6 | `06c6949` | Levels fix — engine now calls `get_all_levels()` every bar (live-code parity); produced the 320-trade run |

**164/164 tests passing** across unit + integration + concurrency.

### Branch: feature/arch-003-ib-client-split

1 commit cherry-picked: `9b6c826` (Rollout #1 on arch003 too). 103/103 tests
green on this branch. Ready for Monday live validation — you'll need to
reopen IB TWS/Gateway first (it was closed last night, that's the only
blocker).

## Running stack (all on your machine, all live)

- **Postgres** (container): has the new `strategies`, `backtest_runs`,
  `backtest_trades` tables + 254 historical trades backfilled to `strategy_id=1`
- **API** (container): rebuilt from the enh-024 worktree, mounts all the
  new `/api/backtests/*` routes
- **Frontend** (container): rebuilt, serves the new BacktestTab
- **bot_manager sidecar** (PID ~45344, port 9000): new `/run-backtest`
  endpoint wired up
- **Bot process**: NOT running (TWS was closed last night). Start TWS and
  accept the disclaimer before restarting the bot for Monday's live test.

## The Feature Analysis panel — this is the data science layer you asked for

For every numeric entry indicator (RSI, VWAP distance, ATR, volume ratio,
etc.), the panel shows:
- **WIN mean μ** vs **LOSS mean μ** — so "RSI averaged 38 on winning
  trades vs. 55 on losing ones" is a one-glance insight
- **Edge** — the signed difference, sorted descending by absolute value
  so the most informative features surface at the top
- **Quartile-bucketed win rate** — four mini bars showing "trades with RSI
  in Q1 win 67%, Q2 win 52%, Q3 win 46%, Q4 win 38%" — actionable for
  strategy tuning

The raw indicator data is stored in `backtest_trades.entry_indicators`
(JSONB, GIN-indexed) so future analytics queries like "all trades where
RSI < 30 AND VIX > 20" run fast.

## Restart prompt (in case of machine crash)

```
Continuing ICT bot review queue — backtest framework was completed
overnight on Apr 19 2026. Read:
- C:\src\trading\ict-bot\.claude\CLAUDE.md
- C:\src\trading\ict-bot-strategies\MORNING_STATUS.md  (this file)
- docs/active_strategy_design.md on feature/enh-024-strategy-plugins
- docs/backtest_framework.md

Latest commits on feature/enh-024-strategy-plugins:
  06c6949 levels fix
  50693d7 Backtest D (UI)
  83e9db7 Backtest C (API)
  547074c Backtest B (engine)
  14dc807 Backtest A (DDL)
  ad2be1d rollout #1

arch003 sync: 9b6c826 cherry-picked.

Status: Everything green. 164/164 tests passing. Dashboard deployed.
Use TodoWrite to restore the queue, then wait for user direction.
```

## What's still open (not urgent)

1. **Monday live validation of arch003** (ib_client split) — needs TWS + market hours
2. **Rollouts #2-#8** on active_strategy_design.md — settings loader, tickers
   loader, `main.py` wiring, Dashboard Strategies tab, Bot-start strategy
   picker. Only rollout #1 + #5 (pulled forward) are in. Further rollouts
   are needed before you can actually switch strategies at bot-start time.
3. **ORB backtest smoke test** — the plugin path in the engine accepts any
   `BaseStrategy` but we haven't run ORB through it end-to-end
4. **ARCH-004 Step 3** — deeper DB integration tests (reconciliation race
   scenarios)

None of these block reviewing the backtest work. Whenever you want.

## Suggested review order

1. **Just look around** — open http://localhost, click **Backtest**, click row #83, see everything working
2. **Launch a new backtest** of your own from the UI to confirm the flow
3. **Browse the code** on the feature/enh-024-strategy-plugins branch on GitHub — the design doc (`docs/active_strategy_design.md`) is the best entry point
4. When you're ready, **merge feature/enh-024-strategy-plugins into feature/dashboard** — that brings rollout #1 + the backtest framework to the main branch
5. Decide when you want rollouts #2-#8 — could be a whole session or spread over several
