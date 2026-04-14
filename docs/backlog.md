# ICT Trading Bot — Backlog

## Last Updated: 2026-04-14

---

## CRITICAL — Must Fix Before Production Trading

### AUDIT-001: Comprehensive Error Handling Audit
Every try/except in the codebase needs review. No bare `except: pass`.
Every failed call must: log the error, save context to DB, report to dashboard.
Files to audit:
- strategy/exit_manager.py
- strategy/exit_executor.py
- strategy/exit_conditions.py
- strategy/reconciliation.py
- strategy/trade_logger.py
- strategy/scanner.py
- strategy/option_selector.py
- broker/ib_client.py
- db/writer.py
- main.py

### AUDIT-002: Trade Lifecycle Integrity
Trace every path from signal → order → fill → DB insert → monitoring → exit → DB close.
Ensure no trade can exist on IB without a DB record. Specific issues:
- QQQ trade on IB but not in DB (order filled after timeout, add_trade never called)
- Timeout on trade entry should check IB for actual fill and adopt if filled
- Every IB order must result in either a DB record or an explicit error log

### AUDIT-003: Reconciliation Reliability
- BUG-027: False closes on IB timeout (fixed but needs verification)
- conId matching implemented but untested with live market
- Reconciliation must NEVER close trades based on incomplete data
- Need to verify the safety check works (0 IB positions + DB trades = abort)

### AUDIT-004: Syntax and Import Errors
Review all files for syntax errors that may have been introduced during rapid editing.
Run full import test on every module.

---

## HIGH — Important for Reliable Operation

### ENH-001: IB Streaming Market Data
Replace snapshot polling with streaming subscriptions for sub-second price updates.
Spec: docs/production_improvements.md
Status: Not started

### ENH-002: Processes Tab + Heartbeat Monitoring
Centralized health view for all processes/threads with 60s heartbeat.
Spec: docs/production_improvements.md
Status: Not started

### ENH-003: Error Pipeline + Sanity Checks
Pre-flight validation before every IB call. All errors flow to system_log.
Spec: docs/production_improvements.md
Status: Partially implemented (system_log table exists, not fully used)

### ENH-004: System Status Tab
Dashboard tab showing all component health, system_log viewer, process status.
Status: Not started

### BUG-028: Scanners Auto-Start on Restart
Fixed: bot now resets scans_active=false on startup.
Status: Fix committed, needs verification

### BUG-027: Reconciliation False Closes
Fixed: safety check, conId matching, raises on timeout.
Status: Fix committed, needs verification with live market

### BUG-022: Double-Sell (Bracket + Exit Manager)
Fixed: exit flow cancels brackets first, verifies position.
Status: Fix committed, needs verification with live market

---

## MEDIUM — Enhancements

### ENH-005: Analytics v2 Improvements
- PT timezone consistency in all charts (partially done)
- Drill-down click → popup (done)
- Date range filter (done)
- More analytics: win/loss by day of week, by time of day patterns
Spec: docs/analytics_v2.md

### ENH-006: Separate Signal Engine from Trade Management Engine
Architectural refactor to cleanly separate scanning from trade management.
Status: Conceptual

### ENH-007: Option Rolling Logic
At ~70% profit, close and roll to next strike.
Status: Implemented but untested with live market

### ENH-008: TP → Trailing Stop
At 100% TP, move SL to TP level instead of hard exit.
Status: Implemented but untested with live market

### ENH-009: SPY Option Chain Issue
SPY picks wrong expiry (June 2025 from SMART chain).
Need to check multiple exchanges or filter expirations.
Status: Known issue, not fixed

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

## COMPLETED (for reference)

- Database schema (8 tables + analytics views)
- Bot DB integration (dual-write)
- FastAPI backend (20+ endpoints)
- React frontend (6 tabs: Trades, Analytics, Threads, Tickers, Settings)
- Docker Compose deployment
- Bot manager sidecar
- IB trade ID integration (permId, conId)
- DB-based state management (replaced file-based)
- Batch IB pricing
- 28+ bug fixes (BUG-001 through BUG-028)
