# Backtest Analytics — Design & Implementation Record

**Branch:** `feature/profitability-research`
**Dates:** April 18–20, 2026
**Status:** Shipped (head commit `b34e448`)

## 1. Problem Statement

The Backtest tab had grown to 138 runs × 15,593 trades across 3 strategies
(ICT, ORB, VWAP) and 20+ tickers. Users had three unmet needs:

1. **No big-picture view.** The runs table showed one row per backtest
   but nothing aggregated across runs. To answer "which strategy works
   best on INTC?" a user had to mentally scan dozens of rows.
2. **Inconsistent drill-down.** Different clicks did different things
   on the same page — some opened modals, some scrolled, some nothing.
3. **Local-only column sort on paginated data.** Clicking a column
   header sorted only the visible page, which was misleading when the
   run they wanted sat on page 2.

This doc captures the design and implementation of the Backtest
Analytics feature that addresses all three.

## 2. Goals & Non-Goals

### Goals
- Cross-run slice-and-dice without leaving the Backtest tab.
- Charts (visual) + tables (precise) — user switches between them.
- One consistent drill-down UX. Every click leads predictably to:
  (a) a filter applied to the runs table below, or
  (b) a single unified trades modal for individual-run inspection.
- Server-side sort + filter so the entire dataset drives the UI,
  not just the rows currently rendered.
- Safe against SQL injection (whitelisted sort/filter columns).

### Non-Goals
- Cross-run feature-importance analysis (entry/exit indicators vs
  WIN/LOSS). The per-run endpoint `/backtests/{id}/feature_analysis`
  already exists; cross-run version is a follow-up.
- Sweep-launch UI form. Sweep runner + CLI already exist
  (`run_sweep.py`); UI wiring is pending.
- Agent-driven strategy optimization. Noted as user request for
  future work.

## 3. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                        BACKTEST TAB                              │
│                                                                  │
│  ┌─────────────────── AnalyticsPanel ──────────────────────┐   │
│  │                                                          │   │
│  │  [Charts] [Tables]  ← view switcher                      │   │
│  │                                                          │   │
│  │  Charts view (recharts BarCharts):                       │   │
│  │    • P&L by Strategy         ──┐                         │   │
│  │    • Top 20 Tickers          ──┤ click → filter runs    │   │
│  │    • Top 20 Ticker×Strategy  ──┘                         │   │
│  │    • Top 15 Runs             ─── click → open run modal  │   │
│  │                                                          │   │
│  │  Tables view (sortable/filterable ColDef tables):        │   │
│  │    • Ticker × Strategy  ──┐                              │   │
│  │    • By Strategy        ──┤ row click → filter runs      │   │
│  │    • By Ticker          ──┘                              │   │
│  │    • Top/Bottom 20 Runs                                  │   │
│  └──────────────────────────────────────────────────────────┘   │
│                              │                                   │
│                              ▼                                   │
│  ┌─────────────── Filter chip bar ──────────────────────────┐   │
│  │  [strategy=ict ×] [ticker=INTC ×]       Clear all         │   │
│  └──────────────────────────────────────────────────────────┘   │
│                              │                                   │
│                              ▼                                   │
│  ┌──────────────── RunsTable (paginated) ──────────────────┐    │
│  │  id · name · strategy · tickers · period · trades · PnL │    │
│  │  Header click → server-side sort across ENTIRE dataset  │    │
│  │  Row click ────────┐                                     │    │
│  └─────────────────── │ ────────────────────────────────────┘    │
│                      ▼                                           │
│  ┌──────────────── TradesModal (unified) ──────────────────┐    │
│  │  Same modal for every drill-down entry point:           │    │
│  │    • run row click  → /backtests/{id}/trades (paged)    │    │
│  │    • Top 15 Runs chart bar                              │    │
│  │  Server-side sort on header click                       │    │
│  │  WIN/LOSS filter toggle                                  │    │
│  │  ESC / backdrop / [×] to close                          │    │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

## 4. Backend API

### `GET /api/backtests/analytics/cross_run`

Aggregates completed runs into four rollups. Powers the Analytics panel.

**Query params**
| param        | type    | default    | notes                         |
|--------------|---------|------------|-------------------------------|
| `strategy_id`| int     | —          | Filter to one strategy        |
| `status`     | string  | `completed`| Run status filter             |
| `limit_runs` | int     | 500        | Hard cap on runs aggregated   |

**Response**
```json
{
  "run_count": 138,
  "trade_count": 15593,
  "by_strategy": [
    { "strategy": "ict", "trades": 5714, "pnl": 44994.96,
      "wins": 3198, "decided": 5714, "win_rate": 56.0, "runs": 60 }
  ],
  "by_ticker": [
    { "ticker": "QQQ", "trades": 2786, "pnl": 18755.18,
      "win_rate": 52.4, "strategies": ["ict","orb"] }
  ],
  "by_ticker_strategy": [
    { "ticker": "INTC", "strategy": "ict", "trades": 245,
      "pnl": 6013.30, "win_rate": 70.2, "runs": 8 }
  ],
  "top_runs":    [ ...top 20 sorted DESC by pnl... ],
  "bottom_runs": [ ...bottom 20 sorted ASC by pnl... ]
}
```

All rollups are computed in a single `GROUP BY` per cut using
`backtest_trades` joined to `backtest_runs` + `strategies`. Payload
is small — heaviest field is the `top_runs` list at ~20 rows.

### `GET /api/backtests/analytics/trades`

Cross-run trade drill-down with compound filters. Powers the
Top-15-Runs chart's individual-run modal and any future cross-run
trade view.

**Query params**
| param       | type    | default     | notes                                |
|-------------|---------|-------------|--------------------------------------|
| `strategy`  | string  | —           | Filter by strategy NAME              |
| `ticker`    | string  | —           | Exact ticker match on trade          |
| `run_id`    | int     | —           | Pin to a single run                  |
| `outcome`   | string  | —           | `WIN` / `LOSS` / `SCRATCH`           |
| `status`    | string  | `completed` | Parent run's status                  |
| `limit`     | int     | 500         | Max rows returned (1–5000)           |
| `offset`    | int     | 0           | Pagination offset                    |
| `sort`      | string  | —           | Column name from `_TRADES_SORT_COLS` |
| `direction` | string  | —           | `asc` / `desc` (regex-validated)     |

Filters AND-join. Unknown sort keys silently fall back to
`t.pnl_usd DESC` — whitelisted via `_TRADES_SORT_COLS`.

### `GET /api/backtests` — extended with filters & sort

New query params layered on the existing endpoint:

| param       | type    | notes                                               |
|-------------|---------|-----------------------------------------------------|
| `strategy`  | string  | Filter by strategy NAME (was only `strategy_id`)    |
| `ticker`    | string  | `= ANY(r.tickers)` membership on the text[] array  |
| `sort`      | string  | Column from `_RUNS_SORT_COLS`                       |
| `direction` | string  | `asc` / `desc`                                      |
| `offset`    | int     | Pagination offset (new)                             |
| `total`     | out     | Response now includes total count post-filter       |

### `GET /api/backtests/{run_id}/trades` — extended with sort

Added `sort` + `direction` params. Same whitelist approach as
`analytics/trades`. Default order unchanged (`entry_time ASC`).

### Sort-key whitelists

```python
# dashboard/routes/backtest.py
_RUNS_SORT_COLS = {
    "id": "r.id", "name": "r.name", "status": "r.status",
    "strategy": "s.name", "strategy_name": "s.name",
    "trades": "r.total_trades", "win_rate": "r.win_rate",
    "total_pnl": "r.total_pnl", "profit_factor": "r.profit_factor",
    "max_drawdown": "r.max_drawdown", "avg_hold_min": "r.avg_hold_min",
    "created_at": "r.created_at",
    "period": "r.start_date", "start_date": "r.start_date",
    "end_date": "r.end_date",
}

_TRADES_SORT_COLS = {
    "id", "ticker", "symbol", "direction", "entry_price",
    "exit_price", "pnl_usd", "pnl_pct", "hold_minutes",
    "entry_time", "exit_time", "signal_type", "exit_reason",
    "exit_result",
}
```

`_sort_clause()` returns a safe `ORDER BY` fragment; unknown keys
fall through to the caller-supplied default. Prevents SQL injection
and keeps query plans stable.

### Route-ordering gotcha

**Bug caught during implementation:** FastAPI matches routes by
registration order. `/backtests/analytics/trades` collided with
`/backtests/{run_id}/trades` because both are 3-segment paths ending
in literal `trades`. The int converter on `{run_id}` tried to parse
`analytics` and produced a 422.

**Fix:** Moved both analytics routes (`/analytics/trades` and
`/analytics/cross_run`) above all `{run_id}`-prefixed routes. Added
a large comment in `backtest.py` so future edits don't reintroduce
the problem.

## 5. Frontend Design

### 5.1 Component tree

```
BacktestTab
├── Top bar (Refresh, + Run Backtest)
├── AnalyticsPanel
│   ├── StatBox × 5 (totals, best strategy, best ticker)
│   ├── Charts view   (recharts BarCharts × 4)
│   └── Tables view   (AnalyticsTable × 4)
├── Filter chip bar  (shows active strategy/ticker filters)
├── RunsTable        (paginated, server-side sorted)
├── TradesModal      (shared drill-down, run_id-mode)
└── LaunchDialog     (existing)
```

### 5.2 State lift — filter & sort

All filter/sort state that drives a server query lives at the
`BacktestTab` top level:

```tsx
const [runsSortKey,  setRunsSortKey]  = useState<string | null>(null);
const [runsSortDir,  setRunsSortDir]  = useState<SortDir>(null);
const [runsFilter,   setRunsFilter]   = useState<RunsFilter>({});
```

`fetchRuns()` builds the query string from these three pieces and
reloads on change (via `useEffect` deps). Child components receive
read-only values + callbacks; they never own the query truth.

### 5.3 Unified drill-down: `TradesModal`

One modal component serves all drill-down entry points:

```tsx
interface TradesModalProps {
  title: string;
  subtitle?: React.ReactNode;
  onClose: () => void;
  // Mode A: run-scoped, paginated via /backtests/{id}/trades
  runId?: number | null;
  // Mode B: cross-run filters via /backtests/analytics/trades
  analyticsFilters?: {
    strategy?: string; ticker?: string;
    run_id?: number; outcome?: string;
  };
}
```

Internally the modal:
- picks the right endpoint based on which prop is set,
- re-fetches on page/filter/sort change,
- renders a shared `TradesTable` with server-side sort,
- closes on ESC, backdrop click, or `[×]`.

This replaces three separate drill-down paths that existed in the
previous iteration:
1. inline modal in `BacktestTab` for runs,
2. row-click drill in `AnalyticsPanel` top_runs table,
3. chart-click drill on analytics bars (later removed, see 5.5).

### 5.4 Sortable/Filterable table primitive

```tsx
interface ColDef<T> {
  key: string;
  label: string;
  get: (row: T) => unknown;        // value for sort/filter
  render: (row: T) => ReactNode;   // cell UI
  filterable?: boolean;
  filterType?: 'text' | 'number';
  align?: 'left' | 'right';
}

function useSortableFilterable<T>(
  rows: T[], cols: ColDef<T>[], serverSort?: ServerSortCtrl,
)
```

- When `serverSort` is provided the hook bypasses local sorting and
  delegates to the caller (used by `RunsTable` + `TradesTable`).
- When `serverSort` is absent the hook sorts in memory — used by the
  Analytics rollup tables which already have all rows (aggregates
  are small).
- Filter behavior stays client-side everywhere. Numeric filters
  accept operators `>N`, `<N`, `>=N`, `<=N`, `=N`, plus substring
  fallback.

### 5.5 Click semantics — "filter the table below"

The **key UX change** in the last iteration: clicking a chart bar or
aggregate-table row now **filters the runs table below** rather than
opening a modal. This was explicit user feedback:

> "when I click or filter I would like it to filter the table below
>  so I can see the details"

Rationale: the Analytics panel gives the bird's-eye view; the runs
table shows which individual runs contributed; the modal drills into
trade details. Filter-then-drill is a natural progression; modal-
from-chart skips a step the user actually wants to see.

**Mapping of click → action:**

| Where              | What user clicks       | Action                           |
|--------------------|------------------------|----------------------------------|
| P&L by Strategy    | bar                    | set `strategy` filter            |
| Top Tickers        | bar                    | set `ticker` filter              |
| Ticker × Strategy  | bar                    | set both                         |
| Top 15 Runs        | bar                    | open that run's modal            |
| By Strategy table  | row                    | set `strategy` filter            |
| By Ticker table    | row                    | set `ticker` filter              |
| Ticker × Strategy  | row                    | set both                         |
| Top/Bottom 20 Runs | run ID link            | open that run's modal            |
| Runs table         | row                    | open that run's modal            |

Filters are additive (AND-join) — clicking a strategy after a ticker
is already applied keeps both.

### 5.6 Filter chip bar

```tsx
<div id="backtest-runs-table">
  {(runsFilter.strategy || runsFilter.ticker) && (
    <div className="... chip bar ...">
      <span>Active filters:</span>
      {runsFilter.strategy && <Chip color="blue"
                                    label={`strategy = ${runsFilter.strategy}`}
                                    onClear={...} />}
      {runsFilter.ticker   && <Chip color="purple"
                                    label={`ticker ∈ tickers = ${runsFilter.ticker}`}
                                    onClear={...} />}
      <button onClick={clearAll}>Clear all</button>
    </div>
  )}
  <RunsTable ... />
</div>
```

- Blue chip = strategy filter, purple = ticker. Color-coded so the
  eye separates them instantly.
- `scrollIntoView` fires on `applyFilter` so the user sees the
  filtered table pop into view when they click a chart.
- Chips persist across chart clicks. "Clear all" resets both.

## 6. Data Model — no schema changes

This feature required **no** DDL. The existing schema already had
everything:

- `backtest_runs` — one row per run with strategy_id, tickers[],
  totals.
- `backtest_trades` — one row per trade with run_id, ticker,
  pnl_usd, exit_result, timestamps, and JSONB enrichment.
- `strategies` — lookup of strategy_id → name.

All rollups are on-the-fly SQL aggregations. At 15k trades they run
in <100ms on Postgres without any indexes beyond the primaries.
If trade count grows 100×, the natural next step is a
materialized view or `v_backtest_analytics` view.

## 7. Security

- **SQL injection:** all `sort` / `direction` inputs pass through
  whitelist lookups; unknown keys fall through to a hardcoded
  default. `direction` uses FastAPI's regex validator
  `pattern="^(asc|desc)$"`.
- **Filter params:** bound via SQLAlchemy named parameters (`:sid`,
  `:ticker`, etc.) — never string-interpolated.
- **Array membership:** `:ticker = ANY(r.tickers)` — Postgres
  parameterizes the scalar side safely.

## 8. Test Coverage

All routes have integration tests in
`tests/integration/test_backtest_api.py`. Full suite: 380 passed +
4 skipped as of head.

**New test classes added across the 4 commits:**

| Class                      | Cases | What it covers                            |
|----------------------------|-------|-------------------------------------------|
| `TestCrossRunAnalytics`    | 7     | rollup shape, ticker/strategy filters, bounds |
| `TestAnalyticsTrades`      | 6     | compound filters, sort, outcome, injection-safe |
| `TestRunsListSort`         | 8     | server sort asc/desc, total count, strategy/ticker filters, no-match empty set |
| `TestTradesListSort`       | 2     | sort by pnl/ticker, preserves default otherwise |

## 9. Implementation Timeline

| Commit   | Scope                                                       |
|----------|-------------------------------------------------------------|
| `7fbbd00`| Cross-run analytics endpoint + Analytics tables-only panel |
| `d7087b1`| Charts (recharts) + unified TradesModal + server-side sort |
| `dcd5ef7`| Chart/table click → filter runs table                      |
| `b34e448`| Chore: untrack `.bot_stop`, bump RESTART doc hash          |
| `01b69ba`| Filter re-slices ALL charts + KPIs (not just runs table)   |
| `3819f46`| #7 Server-side per-column filters on paginated tables      |
| `18020da`| 1m-resolution validation of top run per strategy           |
| `c20ce7b`| #1 Cross-run feature importance (quartile spread ranking)  |
| `d042044`| #2 Exit-indicator UX polish (baseline-relative tile colors)|
| `74bbfe4`| #3 Parameter Sweep launch UI form + sidecar wiring         |

## 9a. Follow-up Features Added (#7, #1, #2, #3)

### #7: Server-side per-column filters

Problem: column filter inputs operated only on the loaded page. Fixed
with a mirror of the sort-whitelist pattern:

```python
# dashboard/routes/backtest.py
_RUNS_FILTER_COLS = { "name": "r.name", "total_pnl": "r.total_pnl", ... }
_RUNS_NUMBER_COLS = { "trades", "win_rate", "total_pnl", ... }

def _build_column_filters(specs, column_map, number_cols, param_prefix):
    # Parse 'col:value' strings, build safe WHERE fragments.
    # Numeric accepts >N, <N, >=N, <=N, =N. Text uses ILIKE.
```

Frontend: `useSortableFilterable(rows, cols, serverSort?, serverFilter?)`
now accepts both controllers. When `serverFilter` is present, local
filtering is skipped and filter-state updates delegate to the caller.
The caller (`TradesModal`, `BacktestTab`) debounces 300ms via
`useEffect + setTimeout` before refetching.

### #1: Cross-run feature importance

`GET /api/backtests/analytics/feature_importance?source=entry|exit`
scans the requested JSONB column across every trade in every
run matching the active strategy/ticker filter. Per feature:

- WIN mean vs LOSS mean (raw edge metric)
- Quartile win rates (4-bucket breakdown of feature value vs outcome)
- **Quartile spread** = `max(win%) - min(win%)` across quartiles —
  the primary ranking signal. Unlike raw |edge|, quartile spread is
  normalized against feature magnitude, so RSI (0-100) can be
  compared against volume (millions) on equal footing.

Refactored the per-run endpoint math into `_compute_feature_analysis`
to share the implementation.

UI: new "Feature Importance" view in the Analytics panel. Each
feature rendered as a card with a 4-tile quartile strip, colored
**relative to the per-feature baseline** (not absolute) so a 55% tile
is green when baseline is 45% and red when baseline is 60%.

### #2: Exit-indicator correlation

Same endpoint with `source=exit`. Yellow caveat on the UI explains
that exit indicators are mostly tautological (RSI at exit high ↔
trade closed up ↔ WIN). The value is in studying exit-timing rules
rather than finding predictive setups.

### #3: Parameter Sweep launch UI

`POST /api/backtests/sweep/launch` proxies to
`POST http://localhost:9000/run-sweep` on the bot_manager sidecar.
Sidecar spawns `run_sweep.py` as a subprocess with `PYTHONIOENCODING=
utf-8` to avoid Windows codec issues on log output.

UI: a `SweepDialog` modal with preset grid fields for the four most-
swept params (profit_target, stop_loss, base_interval, option_dte_
days). Each is a comma-separated list; blank means "don't sweep
that param, use base_config default." Live cell-count readout plus
a `per_ticker` checkbox. Warning banner over 30 total runs.

### Route-ordering re-gotcha

Had to add the new `/backtests/analytics/feature_importance` route
above `/backtests/{run_id}/feature_analysis` in the file, same
registration-order trap documented in §4.

## 10. Known Limitations & Follow-ups

- **Client-side filters on paginated tables** still operate only on
  the current page. The header SORT is server-side; the per-column
  FILTER inputs are local. Users wanting to filter across pages
  should use the chart/aggregate-table row clicks instead.
- **AnalyticsPanel data** caps at 500 runs
  (`limit_runs` default). Sufficient today (138 runs) but will need
  pagination or time-windowing at high scale.
- **No charts for hold-time, exit-reason, etc. across runs** — only
  the per-run `/analytics` endpoint has those. A cross-run version
  would be valuable for exit-reason drift analysis.
- **Feature-importance cross-run** is the natural next step. The
  per-run `/feature_analysis` endpoint exists; a cross-run variant
  that joins by (ticker, strategy) and correlates entry/exit
  indicators against outcomes would close the "what's actually
  profitable?" loop.

## 11. Files Touched

**Backend**
- `dashboard/routes/backtest.py` — two new endpoints, extended three
  existing ones, added sort whitelists + `_sort_clause` helper.

**Frontend**
- `dashboard/frontend/src/components/BacktestTab.tsx` — rewrote the
  whole tab around `AnalyticsPanel`, `TradesModal`, chip bar,
  server-side sort controller.

**Tests**
- `tests/integration/test_backtest_api.py` — +23 cases across four
  new test classes.

**Docs**
- `RESTART_PROMPT.md` — commit hash + milestone summary updated.
- `docs/backtest_analytics_design.md` — this file.

## 12. How to Verify Live

```bash
# Start the stack
COMPOSE_PROJECT_NAME=ict-bot docker compose up -d

# Smoke-test endpoints
curl 'http://localhost/api/backtests/analytics/cross_run' | jq .run_count
curl 'http://localhost/api/backtests?strategy=ict&ticker=INTC' | jq .total
curl 'http://localhost/api/backtests?sort=total_pnl&direction=desc&limit=5' | jq

# Open the dashboard
# → http://localhost → Backtest tab
# → click any bar in P&L by Strategy → confirm runs table filters
# → column header click → confirm DB re-query (watch Network tab)
```
