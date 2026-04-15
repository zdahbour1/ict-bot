# ICT Trading Bot — Backlog

## Last Updated: 2026-04-15

---

## CRITICAL — Architecture

### ARCH-001: Database is the Single Source of Truth
**Principle**: The PostgreSQL database is the ONLY source of truth for all system state. No process should rely on its in-memory cache for state decisions. Every component reads from the DB. Accuracy is more important than speed — PostgreSQL has its own caching layer.

**Current violations**:
1. `exit_manager.open_trades` (in-memory list) acts as parallel source of truth — trades can exist in memory but not DB, or vice versa
2. `open_trades.json` file is a stale backup of the in-memory list
3. Reconciliation has to sync two sources (IB + DB) but also a third (in-memory list)
4. When bot restarts, it loads from `open_trades.json`, not from DB
5. `add_trade()` writes to in-memory list first, then DB — if DB write fails, state diverges

**Required changes**:
- Exit manager reads open trades from DB every cycle (5s) — no in-memory list as source of truth
- `add_trade()` writes to DB FIRST. If DB write fails, trade is NOT tracked (fail-safe).
- Remove `open_trades.json` entirely — DB is the persistence layer
- On startup, rebuild state from DB, not from JSON file
- All trade state decisions (is this trade open? what's the qty?) query the DB
- Dashboard already reads from DB (correct pattern)

Status: **Tracked — needs incremental implementation**

### ARCH-002: Row-Level Locking for Trade State Transitions
**Principle**: When changing the state of any trade (open→closed, updating price, rolling), the process MUST use `SELECT ... FOR UPDATE` (row-level locking) to prevent race conditions between parallel components.

**Why this matters**: Multiple components run in parallel:
- Exit manager (every 5s, checks all open trades)
- Scanner threads (17+, can place orders concurrently)
- Reconciliation (every 2min, can close/adopt trades)
- Dashboard API (user clicks Close, Reconcile Now)
- IB bracket orders (fire independently on IB servers)

Without row-level locking, two processes can both read a trade as "open", both decide to close it, and both send sell orders → negative position.

**Required implementation**:
- `close_trade()` in db/writer.py: `SELECT ... FOR UPDATE WHERE id=X AND status='open'` — if status is already 'closed', skip (another process got there first)
- `insert_trade()`: use DB-generated ID, return it immediately
- `update_trade_price()`: `SELECT ... FOR UPDATE` to prevent stale overwrites
- Choose blocking vs non-blocking:
  - Exit manager: blocking wait (critical path, must complete)
  - Reconciliation: `FOR UPDATE NOWAIT` or `SKIP LOCKED` (best-effort, retries next cycle)
  - Dashboard close: blocking wait (user action, must complete)
- All state transitions go through DB — no in-memory-only state changes

**Example close_trade with locking**:
```sql
BEGIN;
SELECT id FROM trades WHERE id = :trade_id AND status = 'open' FOR UPDATE;
-- If no row returned → trade already closed by another process → ROLLBACK
UPDATE trades SET status = 'closed', exit_price = :price, ... WHERE id = :trade_id;
COMMIT;
```

Status: **Tracked — implement alongside ARCH-001**

### BUG-038: QQQ 634 Call closed 3 times — negative position (-6 contracts)
The QQQ 634 call was rolled (ENH-007 rolling logic), but after rolling, the old
position kept being "closed" repeatedly. Each close sold 2 more contracts, resulting
in -6 naked short calls on IB.

**Root cause (multiple failures)**:
1. Rolling closes the position but the old trade stays in exit_manager memory as "open"
2. Exit manager evaluates exit conditions on the stale in-memory trade
3. Each cycle, exit manager sees the old trade, decides to close it, sends another sell
4. IB executes each sell — creating naked short positions

**This is a direct consequence of ARCH-001 violation**: exit_manager trusts its
in-memory list instead of checking the DB/IB for actual position state.

**Required fix**: Before ANY sell order, verify:
- The position actually exists on IB with positive quantity
- Use "reduce only" or check position direction to prevent naked shorts
- After closing a trade, REMOVE it from exit_manager memory AND mark closed in DB atomically

Status: **Not fixed — CRITICAL (can create unlimited naked short exposure)**

### BUG-039: No "sell-to-close" / position existence check before selling
The bot sends raw SELL orders without verifying a position exists. IB executes
the sell regardless, creating naked short options positions.

**Required validation before every sell order**:
1. Query IB for current position quantity for the specific contract (by conId)
2. If quantity is 0 → skip the sell (position already closed)
3. If quantity is negative → ABORT and log critical error (already naked)
4. Only sell up to the actual position quantity (never more)

**IB API options**:
- Use `reduceOnly=True` on orders (IB rejects if no position to reduce)
- Or manually check `ib.positions()` before every sell

**This is a safety guard** — even if all other logic is perfect, this prevents
the worst-case scenario of accidentally selling naked options.

Status: **Not fixed — CRITICAL safety requirement**

---

## HIGH — Important for Reliable Operation

### ENH-001: IB Streaming Market Data
Replace snapshot polling with streaming subscriptions for sub-second price updates.
Spec: docs/production_improvements.md
Status: Not started

### ENH-007: Option Rolling Logic — PARTIALLY REDESIGNED
Rolling now uses execute_exit() for the close step (BUG-035 fix).
Remaining work:
- Roll trigger should be at (bracket TP level - 10%) instead of fixed 70% threshold
- Needs live market testing to verify the full sequence works end-to-end
- Config: ROLL_ENABLED=True, ROLL_THRESHOLD=0.70 (70% of TP)
Status: Close step fixed, trigger threshold not yet adjusted, needs live testing

### ENH-008: TP to Trailing Stop
At 100% TP, move SL to TP level instead of hard exit.
Status: Implemented but untested with live market

---

## LOW — Nice to Have

### ENH-010: Compact Trade Table
Additional UI polish for the trades tab.

### ENH-011: Trade Notes
Allow user to add notes to individual trades.

### ENH-012: Export to Excel
Export trade data and analytics to Excel/CSV from dashboard.

### ENH-013: Mobile Responsive Design
Dashboard usable on phone/tablet.

---

## COMPLETED

### Critical Audits (all verified with live market)
- **AUDIT-001**: Comprehensive error handling audit — 51 bare except/pass reduced to 1 intentional
- **AUDIT-002**: Trade lifecycle integrity — timeout recovery, orphan detection, IB fill verification
- **AUDIT-003**: Reconciliation reliability — conId matching, safety checks, direct IB calls on startup
- **AUDIT-004**: Syntax and import verification — all 72 Python files compile, 30 modules import

### Bug Fixes (BUG-001 through BUG-035)

| Bug | Description | Root Cause | Fix | Status |
|-----|-------------|------------|-----|--------|
| BUG-001–021 | Various early bugs | Multiple | Multiple | Fixed |
| BUG-022 | Double-sell (bracket + exit manager) | Exit manager and IB bracket both closing the same position | Exit flow: cancel brackets → verify position → sell | Fixed, verified live |
| BUG-027 | Reconciliation false closes | `get_ib_positions_raw` returned `[]` on timeout instead of raising | Raises on failure, safety check aborts on 0 positions with DB trades | Fixed, verified live |
| BUG-028 | Scanners auto-start on restart | `scans_active=true` left in DB from previous session | Bot resets `scans_active=false` on every startup | Fixed, verified live |
| BUG-029 | Phantom DB trades (Meta/Microsoft) | `option_selector.py` returned trade dict even when IB order status was Cancelled/Inactive — only logged a warning | Gate on order status: FAILED_STATUSES return `None`, only proceed for Filled/Submitted/PreSubmitted | Fixed |
| BUG-030 | Missing DB records (Google) | Trade filled on IB but `insert_trade()` failed silently during reconciliation adoption | Reconciliation verifies adopted orphans have `db_id`, retries `insert_trade()` if missing | Fixed |
| BUG-031 | IB error reasons silently lost | Zero `errorEvent` handlers registered — IB rejection codes (201, 202, 203) never captured | Registered `_on_ib_error` callback, stores per-order errors in `_last_errors`, attaches to order result dict | Fixed |
| BUG-032 | Orphaned IB fills not adopted into DB | With 17 tickers placing orders simultaneously, IB queue backed up >60s. Orders filled on IB but scanner timed out. Orphaned fills were detected but only logged, not adopted into DB. | `_check_orphaned_fills()` now builds trade dict from fill data and calls `add_trade()` immediately. Timeout increased from 30s→60s, recovery window from 5s→10s. | Fixed |
| BUG-033 | Duplicate open trades in DB (GOOGL) | Reconciliation adopted same IB position twice — no dedup check against existing DB records | Reconciliation now queries DB for open trades by `ib_con_id` before adopting. Skips if already exists in DB. | Fixed |
| BUG-034 | Negative positions / double-close (regression of BUG-022) | `execute_roll()` bypassed `execute_exit()` — opened new position without properly closing old one via cancel-brackets→verify→sell flow | Root cause was BUG-035. Fixed by rewriting `execute_roll()` to use `execute_exit()`. | Fixed |
| BUG-035 | Option rolling conflicts with bracket orders | `execute_roll()` had its own close logic that didn't cancel bracket orders first. IB bracket could fire on already-closed position → negative qty. | Rewrote `execute_roll()` to call `execute_exit()` (the ONE close function) first, then verify position closed, then open new trade. No duplicate close logic. | Fixed |
| **REG-001** | **IB event loop blocked by DB writes (REGRESSION from ENH-003)** | `_on_ib_error` callback called `handle_error()` which (after ENH-003) does 2 DB writes per call. Ran on IB main thread, blocking event loop. Cascading timeouts. | `_on_ib_error` now only logs to Python logger — zero DB calls on IB thread | Fixed |
| **REG-002** | **6x IB calls per contract lookup (REGRESSION from ENH-009)** | SPY fix added multi-exchange loop in `_occ_to_contract` — tries 6 exchanges for every contract lookup, even when SMART works fine | Try SMART first, only fall back to other exchanges if SMART fails (one at a time) | Fixed |
| **REG-003** | **Timeout recovery completely broken (REGRESSION from ENH-006)** | ENH-006 refactor moved timeout handling into `TradeEntryManager._handle_timeout()` but `ThreadPoolExecutor` was already closed. The 5-second recovery window that saves orphaned trades was replaced with `pass`. Trades filling on IB between 30-35s were never tracked. | Keep `ThreadPoolExecutor` alive during full 35s window. `finally` block shuts it down after recovery attempt | Fixed |
| **REG-004** | **Exit manager heartbeat too frequent (REGRESSION from ENH-002)** | `update_thread_status()` DB write every 5s inside exit manager monitor loop — unnecessary DB load | Heartbeat every 30s (6 cycles) instead of every 5s | Fixed |
| **REG-005** | **Stale bot state after crash** | Bot process crashed but DB still showed `status='running'`, `ib_connected=true`. Dashboard "Stop Bot" didn't work because sidecar was also dead | Manual DB cleanup; sidecar must be running for dashboard bot control to work | Fixed (manual cleanup) |
| **REG-006** | **IB pool connections all timing out (REGRESSION from Steps 3-4)** | `ib_async` ties its asyncio event loop to the thread that calls `ib.connect()`. Pool called `connect_all()` on main thread but ran event loops on dedicated threads. Event loop threads couldn't pump IB events → all calls timed out. | `IBConnection.start()` now does both `connect()` and event loop on the same dedicated thread. `_ready_event` blocks caller until connected. | Fixed |
| BUG-036 | MSFT option contract qualification fails (code 200: No security definition) | Chain returns strikes like $412.50 that exist for weekly/monthly but not for 0DTE. The single ATM strike fails qualification on all exchanges. | Try ATM + 6 nearest candidate strikes. If ATM fails, fall back to closest qualifying strike. | Fixed |
| BUG-037 | Reconciliation not syncing DB with IB (SPY in DB not IB, SLV/QQQ in IB not DB) | Pass 1 only checked exit_manager memory, not DB. `periodic_reconciliation` only removed phantoms, didn't adopt orphans. Symbol matching fragile (spaces). | Complete rewrite: two clean passes. Pass 1: DB→IB (close stale DB trades). Pass 2: IB→DB (adopt orphans). Both use conId matching and query DB directly. Periodic reconciliation now does full two-pass, not just phantom removal. | Fixed |
| BUG-038 | QQQ 634 Call closed 3 times after rolling → -6 naked short contracts | Rolling called `execute_exit()` on old trade, THEN `execute_roll()` called it AGAIN (double-close). Old trade stayed in memory, exit manager kept exiting it. | Split exit_manager logic: if rolling → only call `execute_roll()` (handles close internally). If normal exit → call `execute_exit()`. No double-call. | Fixed |
| BUG-039 | No position check before sell orders — can create naked shorts | `close_position_on_ib()` sent raw SELL without checking if position exists. If position already closed (bracket fired), sell creates naked short. | New `get_ib_position_qty(conId)` checks IB before every sell. `close_position_on_ib()` takes `max_qty` param — never sells more than IB shows we hold. Returns False if qty=0. Direction mismatch check added. | Fixed |
| BUG-040 | Option symbol shows raw OCC format (with spaces) instead of human-friendly | IB returns symbols with spaces like `"GOOGL 260415P00332500"`. Frontend regex `/^[A-Z]+(\d{6})[CP](\d{8})$/` failed to match. | Strip all spaces before regex matching. Also added Call/Put label to display. | Fixed |

### Lessons Learned from Regressions

> **REG-001 through REG-005 were all introduced in the same session (2026-04-14/15).** Root causes:
>
> 1. **Calling blocking code on the IB event loop thread** — The IB error callback MUST be non-blocking. Any DB write, network call, or lock acquisition on the IB thread can cascade into system-wide timeouts.
>
> 2. **Refactoring scope too large without incremental testing** — ENH-006 moved timeout recovery logic between methods without verifying that the `future` variable was still in scope. A smaller refactor with per-step testing would have caught this.
>
> 3. **Loop amplification** — Adding a 6-exchange loop to a function called in a hot path (every price lookup, every order) multiplied IB calls by 6x. Performance impact was not considered.
>
> 4. **No integration test after multi-file changes** — 7 files changed in one commit. Each change was individually sound but the interaction between them (error handler writing to DB + error callback on IB thread) created the blocking cascade.

### Enhancements
- **ENH-014**: Button loading states — Start/Stop Bot and Scans show "Starting..."/"Stopping..." with blue pulse
- **ENH-015**: Trade count summary badges — total/open/closed/errored counts with colored badges in Trades tab PnlSummary — Start/Stop Bot and Start/Stop Scans buttons show "Starting...", "Stopping..." with blue pulsing color while action is in progress. Buttons disabled during operation to prevent double-clicks.
- **ENH-002**: Heartbeat monitoring — exit manager (30s), bot main (30s), scanner (60s) heartbeats to thread_status
- **ENH-003**: Error pipeline — connected `log_error()` to populate errors table for dashboard popup (was empty because only `system_log` was written)
- **ENH-004**: System status — stale/dead detection in ThreadsTab (>2m=STALE, >5m=DEAD), system log viewer panel with level filtering, health dot indicators
- **ENH-005**: Analytics v2 — 3 new charts (P&L by day of week, P&L by signal type, hold time distribution), 2 new SQL views (`v_pnl_by_day_of_week`, `v_pnl_by_signal_type`), drilldown support for day_of_week and signal_type
- **ENH-006**: Separate signal engine from trade management — `signal_engine.py` (pure detection, Signal dataclass) + `trade_entry_manager.py` (entry gates, order placement, enrichment, timeout recovery)
- **ENH-009**: SPY option chain fix — prefer chain with 0DTE expiry, try multiple exchanges for option qualification, stock qualification guard

### Infrastructure
- Database schema (8 tables + 11 analytics views + system_log)
- Bot DB integration (dual-write)
- FastAPI backend (20+ endpoints)
- React frontend (6 tabs: Trades, Analytics, Threads, Tickers, Settings)
- Docker Compose deployment (PostgreSQL, API, Frontend, pgAdmin)
- Bot manager sidecar (port 9000)
- IB trade ID integration (permId, conId)
- DB-based state management (replaced file-based)
- Batch IB pricing
- Centralized error handler (handle_error + safe_call)
- IB errorEvent handler (non-blocking, log-only)
