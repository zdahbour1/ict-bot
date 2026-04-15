# ICT Trading Bot — Backlog

## Last Updated: 2026-04-15

---

## CRITICAL — Must Fix

### BUG-033: Duplicate open trades in DB (GOOGL)
Two rows in trades table with status='open' for GOOGL. Reconciliation closed one
but the other remains open in DB despite being closed on IB.
- **Symptoms**: Dashboard shows stale open trade that doesn't exist on IB
- **Likely cause**: Reconciliation adopted the same IB position twice, or trade was
  entered via timeout recovery AND normal path simultaneously
- **Fix needed**: Reconciliation must check for existing DB records by conId before
  adopting. Dedup check: if a trade with same ib_con_id already exists in DB, skip.
- Status: **Not fixed**

### BUG-034: Negative position / double-close (regression of BUG-022)
Three trades in IB show negative contract count — same trade being closed twice.
One close from the bot's exit manager, one from the IB bracket order (TP or SL hit).
- **Symptoms**: -2 position on IB (should be 0 after close)
- **Root cause**: BUG-022 was fixed (cancel brackets → verify → sell) but the fix
  may not be working correctly with the new parallel connection pool architecture.
  Exit manager on Connection 0 cancels brackets, but the bracket orders on IB may
  have already filled on a different connection before the cancel arrives.
- **Race condition**: IB bracket fires TP/SL → exit manager also decides to exit →
  both close the same position → negative position
- **Fix needed**: Before sending any sell order, ALWAYS check current IB position
  quantity. If position is already 0, skip the sell. Add position lock per ticker.
- Status: **Not fixed — CRITICAL**

### BUG-035: Option rolling conflicts with bracket orders
Rolling logic (ENH-007) is currently enabled at 70% of PROFIT_TARGET but does NOT
cancel bracket orders before rolling. This creates a conflict:
- Bot decides to roll at 70% TP → closes current trade → opens new trade
- But IB bracket TP order is still active at 100% TP → IB may fill the TP
  on the OLD position that was already closed → negative position
- **Current config**: ROLL_ENABLED=True, ROLL_THRESHOLD=0.70 (70% of TP)
- **Required behavior**:
  1. Roll should trigger at (bracket_TP - 10%) to ensure bot rolls BEFORE IB bracket fires
  2. On roll decision: cancel ALL bracket orders first
  3. Close the current position via market order
  4. Open new trade at the next appropriate strike
  5. Place new bracket orders on the new trade
- **Additional concern**: `execute_roll()` in exit_executor.py currently calls
  `select_and_enter()` which places a NEW bracket order, but does NOT cancel
  the OLD bracket orders first. The old brackets reference the old contract.
- Status: **Not fixed — needs redesign**

---

## HIGH — Important for Reliable Operation

### ENH-001: IB Streaming Market Data
Replace snapshot polling with streaming subscriptions for sub-second price updates.
Spec: docs/production_improvements.md
Status: Not started

### ENH-007: Option Rolling Logic — REDESIGN NEEDED
Current implementation has conflicts with bracket orders (see BUG-035).
Needs full redesign:
- Roll trigger: at (bracket TP level - 10%) instead of fixed 70% threshold
- Sequence: cancel brackets → close position → open new position → new brackets
- Must be atomic: if any step fails, abort and leave position as-is
Status: Needs redesign per BUG-035

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

### Bug Fixes (BUG-001 through BUG-031)

| Bug | Description | Root Cause | Fix | Status |
|-----|-------------|------------|-----|--------|
| BUG-001–021 | Various early bugs | Multiple | Multiple | Fixed |
| BUG-022 | Double-sell (bracket + exit manager) | Exit manager and IB bracket both closing the same position | Exit flow: cancel brackets → verify position → sell | Fixed, verified live |
| BUG-027 | Reconciliation false closes | `get_ib_positions_raw` returned `[]` on timeout instead of raising | Raises on failure, safety check aborts on 0 positions with DB trades | Fixed, verified live |
| BUG-028 | Scanners auto-start on restart | `scans_active=true` left in DB from previous session | Bot resets `scans_active=false` on every startup | Fixed, verified live |
| BUG-029 | Phantom DB trades (Meta/Microsoft) | `option_selector.py` returned trade dict even when IB order status was Cancelled/Inactive — only logged a warning | Gate on order status: FAILED_STATUSES return `None`, only proceed for Filled/Submitted/PreSubmitted | Fixed |
| BUG-030 | Missing DB records (Google) | Trade filled on IB but `insert_trade()` failed silently during reconciliation adoption | Reconciliation verifies adopted orphans have `db_id`, retries `insert_trade()` if missing | Fixed |
| BUG-031 | IB error reasons silently lost | Zero `errorEvent` handlers registered — IB rejection codes (201, 202, 203) never captured | Registered `_on_ib_error` callback, stores per-order errors in `_last_errors`, attaches to order result dict | Fixed |
| **REG-001** | **IB event loop blocked by DB writes (REGRESSION from ENH-003)** | `_on_ib_error` callback called `handle_error()` which (after ENH-003) does 2 DB writes per call. Ran on IB main thread, blocking event loop. Cascading timeouts. | `_on_ib_error` now only logs to Python logger — zero DB calls on IB thread | Fixed |
| **REG-002** | **6x IB calls per contract lookup (REGRESSION from ENH-009)** | SPY fix added multi-exchange loop in `_occ_to_contract` — tries 6 exchanges for every contract lookup, even when SMART works fine | Try SMART first, only fall back to other exchanges if SMART fails (one at a time) | Fixed |
| **REG-003** | **Timeout recovery completely broken (REGRESSION from ENH-006)** | ENH-006 refactor moved timeout handling into `TradeEntryManager._handle_timeout()` but `ThreadPoolExecutor` was already closed. The 5-second recovery window that saves orphaned trades was replaced with `pass`. Trades filling on IB between 30-35s were never tracked. | Keep `ThreadPoolExecutor` alive during full 35s window. `finally` block shuts it down after recovery attempt | Fixed |
| **REG-004** | **Exit manager heartbeat too frequent (REGRESSION from ENH-002)** | `update_thread_status()` DB write every 5s inside exit manager monitor loop — unnecessary DB load | Heartbeat every 30s (6 cycles) instead of every 5s | Fixed |
| **REG-005** | **Stale bot state after crash** | Bot process crashed but DB still showed `status='running'`, `ib_connected=true`. Dashboard "Stop Bot" didn't work because sidecar was also dead | Manual DB cleanup; sidecar must be running for dashboard bot control to work | Fixed (manual cleanup) |

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
