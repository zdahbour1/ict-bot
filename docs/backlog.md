# ICT Trading Bot — Backlog

## Last Updated: 2026-04-16

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

Status: **Implemented** — exit_manager reads from DB, no JSON, DB-first add_trade

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

Status: **Implemented** — close_trade uses SELECT FOR UPDATE NOWAIT, update_trade_price uses GREATEST

### ARCH-005: Single Close Authority + Graceful Already-Closed Handling
**Principle**: Only ONE code path can close trades — `_atomic_close()` in exit_manager. No other thread, module, or function sends sell orders. All close requests funnel through this single point.

**Verified sell order chain** (no other path exists):
```
_atomic_close() → execute_exit() → close_position_on_ib() → sell_call/sell_put
```

**Error handling for already-closed trades**:
When `execute_exit()` checks IB and finds position qty=0 (already closed by bracket order or another process), it:
1. Does NOT send a sell order (prevents naked shorts)
2. Returns to `_atomic_close()` which proceeds to `finalize_close()`
3. `finalize_close()` updates the DB to status='closed' with the current price
4. The DB is now in sync with IB reality

This means: regardless of WHO closed the trade on IB (bracket TP/SL, manual TWS close, another process), the DB always gets updated to reflect the true state.

**Future enhancement rule**: Any new feature that needs to close trades (new exit conditions, new UI actions, scheduled closes, API endpoints) MUST call `_atomic_close()` — never send sell orders directly.

Status: **Implemented and enforced**

### ARCH-003: Refactor into Clean, Small, Reusable Components
**Principle**: Each module has a single responsibility, is independently testable, and communicates only through the DB. No module should be >200 lines. No duplicated logic.

**Current code smells**:
1. `ib_client.py` (~750 lines) — mixes connection management, order placement, market data, position queries, contract validation, error handling. Should be split into focused modules.
2. `trade_entry_manager.py` (~275 lines) — has timeout recovery, orphan adoption, enrichment, thread status updates all in one class. Entry orchestration and IB recovery should be separate.
3. `scanner.py` (~280 lines) — cleaner after ENH-006 refactor, but still handles data fetching, window checks, news filtering, signal logging, and email sending.
4. `exit_manager.py` (~270 lines) — improved with ARCH-001 but still mixes monitoring loop, exit evaluation, price updates, UI commands, reconciliation scheduling, and heartbeat.
5. Multiple files handle OCC symbol parsing independently (exit_manager._is_expired, TradeTable.tsx regex, reconciliation symbol matching).
6. Config values scattered — some from config.py, some from DB settings, some hardcoded.

**Proposed module structure** (refactor plan):

```
broker/
  ib_pool.py          — Connection pool (exists, clean)
  ib_client.py        — Split into:
    ib_orders.py      — Order placement (bracket, simple, cancel)
    ib_market_data.py — Price fetching, Greeks, VIX, batch pricing
    ib_positions.py   — Position queries, fills checking
    ib_contracts.py   — Contract validation, ATM lookup, OCC parsing
    ib_client.py      — Thin facade combining above modules

strategy/
  signal_engine.py    — Signal detection (exists, clean)
  trade_entry.py      — Entry orchestration (rename from trade_entry_manager)
  exit_manager.py     — Monitoring loop + DB cache only
  exit_conditions.py  — Exit logic (exists, clean)
  exit_executor.py    — IB close operations (exists, improved)
  trade_logger.py     — CSV + DB logging (exists)
  reconciliation.py   — Two-pass sync (exists, improved)

utils/
  occ_parser.py       — Shared OCC symbol parsing/formatting
  time_windows.py     — Market hours, trade windows, news filters
  enrichment.py       — Trade enrichment (Greeks, indicators, VIX)
```

**Rules for refactoring**:
- Each module <200 lines
- No circular imports
- All DB access goes through db/writer.py (single point of truth)
- All IB access goes through ib_client.py facade
- Shared utilities in utils/ (OCC parsing, time windows)
- Each module independently testable

Status: **Planned — implement after ARCH-001/002 testing is stable**

### ARCH-004: Automated Regression Test Suite
**Principle**: Every bug fix and enhancement must have a corresponding test. No code change ships without passing the full regression suite. Tests run automatically before commits.

**Test categories**:

1. **Unit tests** (no IB, no DB — pure logic)
   - exit_conditions.py: evaluate_exit() with various P&L scenarios
   - signal_engine.py: detect() with mock bar data
   - OCC symbol parsing (once extracted to utils/)
   - Time window calculations

2. **DB integration tests** (real PostgreSQL, no IB)
   - insert_trade() → returns id
   - close_trade() with row-level locking: close same trade twice → second returns False
   - update_trade_price() with GREATEST: peak never downgrades
   - get_open_trades_from_db(): returns correct trades
   - Reconciliation pass 1: DB trade not on IB → closed
   - Reconciliation pass 2: IB position not in DB → adopted
   - Duplicate adoption prevention (BUG-033)

3. **IB integration tests** (mock IB or paper trading)
   - Order placement returns valid order_id/perm_id/con_id
   - Position check before sell: qty=0 → skip sell (BUG-039)
   - Position check before sell: negative qty → abort (BUG-039)
   - Bracket cancel before sell (BUG-022)
   - Multi-exchange contract qualification fallback (ENH-009, BUG-036)
   - Timeout recovery: orphaned fill adoption (BUG-032)

4. **End-to-end scenarios** (full system with paper IB)
   - Signal → order → fill → DB record → monitoring → exit → DB closed
   - Rolling: old trade closed → new trade opened → no double-close
   - Dashboard close: click Close → exit_manager sees it closed
   - Restart: trades survive in DB, resume monitoring
   - Reconciliation: stale DB trades cleaned, orphan IB positions adopted
   - 17 tickers simultaneous entry: all tracked in DB, no timeouts

5. **Race condition tests**
   - Two threads close same trade simultaneously → only one succeeds
   - Exit manager + dashboard close race → no negative position
   - Reconciliation + exit manager close race → no double-close
   - Scanner entry + exit manager close on same ticker → no conflict

**Tools to consider**:
- pytest for all test categories
- pytest-postgresql for DB integration tests with fresh schema per test
- unittest.mock for IB client mocking
- Docker test containers for isolated PostgreSQL
- Pre-commit hooks to run test suite before every commit
- CI/CD (GitHub Actions) for automated test runs on push

Status: **Planned — implement alongside ARCH-003 refactoring**

### BUG-042: Option rolling leaves old trade open in DB (AMD 265→270)
**Observed**: AMD 265 was rolled to AMD 270. IB closed 265 and opened 270 correctly.
But DB record for 265 was never marked closed (still status='open'). Exit manager
sees 265 as open, tries to close it every 5s, finds -2 qty (naked short from the
roll's sell) → direction mismatch error repeating every 5 seconds.

**Root cause analysis needed**: The `_atomic_close` with `should_roll=True` calls
`execute_roll()` which internally calls `execute_exit()` to close on IB. But there
are two separate issues:
1. The `execute_roll()` closes the IB position but may the `finalize_close()` not be
   completing properly — possibly the `execute_roll` throws or `safe_call` swallows
   the error, and `finalize_close` is called with a session that's in a bad state
2. The exit_manager may be processing the SAME AMD trade on the NEXT cycle (5s later)
   before the first close completes — the DB cache hasn't refreshed yet, so the old
   trade is still visible

**Additional factor**: The `get_ib_position_qty()` in `execute_exit()` uses the
265 conId to check IB, but after rolling, the 265 position may show as -2 (sold
during the roll) while the 270 position is +2 (new position). The safety guard
sees -2 for LONG → direction mismatch → refuses to sell → trade stays open in DB.

**Fix needed**:
- Rolling must be truly atomic: lock DB → close IB → mark DB closed → open new → all in one flow
- The 265 DB record MUST be marked closed regardless of whether the 270 open succeeds
- Need to verify why finalize_close() didn't update the DB for trade 103

Status: **Fixed** — root cause was SQL syntax error in finalize_close (`:ee::jsonb` → `CAST(:ee AS jsonb)`). Plus reconciliation no longer adopts negative positions.

### BUG-043: ENH-007/BUG-038 overlap — rolling config should be in settings table
ROLL_ENABLED and ROLL_THRESHOLD are hardcoded in config.py. They need to be in the
settings table so users can adjust from the dashboard. Also, BUG-038 noted that the
roll trigger should be at (bracket_TP - 10%) instead of fixed 70% of TP, to ensure
the bot rolls BEFORE the IB bracket order fires.

**Current config (hardcoded)**:
- ROLL_ENABLED = True (config.py line 169)
- ROLL_THRESHOLD = 0.70 (config.py line 170)

**Required**:
- Add to settings table: ROLL_ENABLED, ROLL_THRESHOLD
- Settings tab should show these under "exit_rules" category
- Roll trigger should be: (bracket_TP_price - 10%) converted to P&L percentage
  instead of fixed 70% of PROFIT_TARGET

Status: **Fixed** — ROLL_ENABLED, ROLL_THRESHOLD, TP_TO_TRAIL, STOP_LOSS, PROFIT_TARGET, USE_BRACKET_ORDERS, RECONCILIATION_INTERVAL_MIN all in settings table. Dashboard Settings tab shows them under "exit_rules" category.

### ARCH-006: Single Open Authority — one function opens all trades
**Principle**: Same as ARCH-005 (single close), but for opening trades.
Only ONE function creates trade records in the DB. All paths use it.

**Current open paths** (3 — should be 1):
1. `trade_entry_manager.enter()` → `exit_manager.add_trade()` — scanner signal
2. `execute_roll()` → `exit_manager.add_trade()` — rolling
3. `reconciliation` → `exit_manager.add_trade()` — orphan adoption

**Required**: `exit_manager.add_trade()` already serves as the single point,
but it needs a DB-level guard:
- Before INSERT, check no other open trade exists for same ticker + conId
- Use DB unique constraint or SELECT before INSERT to prevent duplicates
- Return db_id or None (already does this)

Status: **Implemented** — add_trade() checks DB for existing open trade on same ticker before INSERT

### BUG-044: Exit reason values inconsistent — not analyzable
**Current exit_reason values** (34 distinct strings, many with embedded P&L):
- `TIME EXIT (90min)`, `STOP LOSS`, `TRAIL STOP (SL=+0%)`, `TRAIL STOP (SL=-20%)`
- `ROLL (P&L=+71%)`, `ROLL (P&L=+137%)` — different string per trade
- `CLOSED ON IB (RECONCILE)`, `BRACKET/CLOSED (RECONCILE)`, `BRACKET/CLOSED (BOT OFFLINE)`
- `CLOSED (UI CLOSE ALL)`, `CLOSED (UI)`, `EXPIRED CONTRACT`

**Problem**: You can't GROUP BY exit_reason for analytics because each ROLL has
a different P&L percentage embedded in the string. You can't filter on "all
reconciliation closes" because there are 3 different strings for it.

**Fix**: Standardize exit_reason to a fixed set of categories. Move variable
data (P&L%, SL level) to exit_enrichment JSONB.

**Proposed standard values**:

| exit_reason | Meaning | exit_result |
|-------------|---------|-------------|
| `TP` | Take profit hit | WIN |
| `SL` | Stop loss hit | LOSS |
| `TRAIL_STOP` | Trailing stop triggered | WIN or LOSS |
| `ROLL` | Position rolled to next strike | WIN |
| `TIME_EXIT` | Held >90 minutes | WIN/LOSS/SCRATCH |
| `EOD_EXIT` | End of day forced close | WIN/LOSS/SCRATCH |
| `EXPIRED` | Contract expired | LOSS |
| `UI_CLOSE` | User clicked close in dashboard | WIN/LOSS/SCRATCH |
| `RECONCILE` | Closed by reconciliation (was closed on IB) | WIN/LOSS/SCRATCH |
| `BRACKET_IB` | IB bracket order fired (TP or SL) | WIN or LOSS |

**"CLOSED ON IB (RECONCILE)" meaning**: This means the reconciliation process
found a trade that was open in the DB but did not exist on IB anymore. This
happens when the IB bracket order (TP or SL) fires while the bot was down or
between reconciliation cycles. Reconciliation detected the mismatch and updated
the DB to match IB reality.

**Resolution**: Use structured `exit_reason` with separate `exit_detail` for analytics:

**exit_reason** (fixed category for GROUP BY):
`TP`, `SL`, `TRAIL_STOP`, `ROLL`, `TIME_EXIT`, `EOD_EXIT`, `EXPIRED`, `UI_CLOSE`, `RECONCILE`, `BRACKET_IB`

**exit_enrichment JSONB** (variable detail for drill-down):
```json
{
  "roll_pnl_pct": 71.0,
  "roll_from_symbol": "AMD260417C00265000",
  "roll_to_symbol": "AMD260417C00270000",
  "trail_sl_level": -0.20,
  "reconcile_source": "periodic"
}
```

**Analytics use cases enabled**:
1. Compare ROLL vs TP vs TRAIL_STOP performance over time
2. Filter by roll percentage ranges (>50%, >100%, >150%) to find optimal roll threshold
3. A/B analysis: trades that rolled vs trades that hit bracket TP — which had better total return?
4. Day-of-week × exit_reason cross-analysis
5. Signal_type × exit_reason — which signals are best for rolling vs quick TP?

Status: **Implemented** — standardized to TP, SL, TRAIL_STOP, ROLL, TIME_EXIT, EOD_EXIT, EXPIRED, UI_CLOSE, RECONCILE. Detail in exit_enrichment JSONB.

### BUG-045: Orphaned "transmit" state orders stuck in IB (MSFT, META)
Orders were submitted to IB but never completed — stuck in "transmit" state.
Likely from earlier timeout issues where the order was sent to IB but the
bot timed out waiting for the fill. These orders are not in the DB.

**Required**: Add a startup cleanup function that:
1. On bot start, query IB for all open/pending orders
2. For each order not matched to a DB trade, cancel it
3. Log the cancelled orders to system_log

Status: **Implemented** — cleanup_orphaned_orders() runs on startup, cancels IB orders not matched to DB trades

### BUG-047: Double-close from bracket order + exit_manager racing
IB bracket orders (TP/SL) fire independently on IB servers. When a trade
hits SL, the IB bracket sells the position. But the exit_manager ALSO
detects SL and sends its own sell via execute_exit(). Both happen within
seconds → double-sell → negative position.

**Root cause**: Exit manager and IB bracket orders are BOTH trying to close
the same trade. The position qty check in execute_exit helps but can't
prevent a race where both check qty=2 at the same moment.

**The reconciliation then adopts the -2 position as a new open trade**
(BUG-047b: fixed by filtering negative positions).

**Required architectural fix**: When bracket orders are active, the exit
manager should NOT send its own SL/TP sell orders. Instead:
1. Exit manager monitors P&L and DETECTS that bracket should fire
2. If bracket didn't fire yet → update the bracket SL level on IB (trailing)
3. If bracket already fired → just update DB to reflect the close
4. Only send manual sell for: TIME_EXIT, EOD_EXIT, ROLL, UI_CLOSE
   (exit types that IB brackets don't handle)

This means: for SL and TP exits, the exit_manager is a MONITOR, not an executor.
The IB bracket is the executor. Exit_manager only updates DB after the fact.

Status: **Fixed** — cancel_bracket_orders() now verifies cancellation (polls up to 3s). If not confirmed, ABORTS the close entirely. execute_exit() checks return value and skips sell if brackets still active.

### BUG-046: Race condition — scanner and exit manager can both open for same ticker
Trade entry does NOT use a DB queue. Scanner directly places IB orders.
If exit manager is rolling ticker X (closing old + opening new) at the
same time scanner detects a signal for ticker X, both could open a position.

**Current guard**: `can_enter()` checks `exit_manager.open_trades` for ticker.
But during a roll, the old trade may be locked/closing while the scanner
doesn't see the new rolled trade yet (not in DB until roll completes).

**Fix needed**: Before INSERT in `add_trade()`, do:
```sql
SELECT id FROM trades WHERE ticker = :ticker AND status = 'open' FOR UPDATE NOWAIT
```
If a row exists → skip the insert (another process already has an open trade).
This is the DB-level duplicate guard from ARCH-006.

Status: **Implemented** — add_trade() checks DB for existing open trade on same ticker

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

Status: **Fixed** — ARCH-001 (DB source of truth) + ARCH-005 (single close authority) + atomic close with DB lock

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

Status: **Fixed** — get_ib_position_qty() checks before every sell. close_position_on_ib() takes max_qty param. Direction mismatch aborts.

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

### ENH-018: Authentication — Login Screen + 2FA
Secure dashboard with username/password login and TOTP two-factor authentication.
Role-based access: admin, trader, viewer. JWT tokens with httpOnly cookies.
Spec: docs/authentication.md
Status: Design documented, not started

### ENH-019: Backtest Framework
Run ICT strategy against historical data, store results in DB, visualize in dashboard.
Compare configurations side-by-side. Reuse live trading signal + exit code.
Spec: docs/backtest_framework.md, docs/backtest_wireframes.md
Status: Design documented, not started

### ENH-020: Cloud Deployment — Installable Image
Docker-based deployment to AWS/GCP/Azure. GitHub Actions CI/CD pipeline.
Install script, IB Gateway in Docker, SSL, environment configuration.
Spec: docs/cloud_deployment.md
Status: Design documented, not started

### ENH-021: Automated Testing Framework
pytest-based test suite with CI/CD integration. Unit, integration, E2E tests.
Test results stored in DB, viewable in dashboard. Pre-commit hooks.
Spec: docs/testing_framework.md
Status: Design documented, not started

### ENH-024: Strategy Plugin Framework — Multi-Scanner Architecture
Pluggable strategy system where each strategy implements BaseStrategy interface.
Strategies produce Signal objects, trade engine executes them. Fully decoupled.
Includes two new recommended strategies: ORB (Opening Range Breakout) and
VWAP Mean Reversion. All strategies configurable from dashboard Settings tab.
Spec: docs/strategy_plugin_framework.md
Status: Design documented, not started

### ENH-023: Futures Options Support (MNQ, NQ, ES, MES, GC, CL)
Trade options on futures contracts. Requires: FOP contract creation, different exchanges
(GLOBEX, NYMEX, COMEX), multiplier-aware P&L, extended trading hours, futures data feed.
Most strategy code is instrument-agnostic (signals, exit conditions) — changes concentrated
in contract creation, P&L calculation, and trading hours.
Spec: docs/futures_options_support.md
Status: Design documented, not started

### ENH-022: Code Profiling & Performance Monitoring
cProfile, line_profiler, memory_profiler integration. Performance monitoring
endpoint. Optimization areas identified: DB cache, IB streaming, exit evaluation.
Spec: docs/testing_framework.md (profiling section)
Status: Design documented, not started

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
