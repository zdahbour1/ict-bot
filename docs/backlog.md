# ICT Trading Bot — Backlog

## Last Updated: 2026-04-14

---

## HIGH — Important for Reliable Operation

### ENH-001: IB Streaming Market Data
Replace snapshot polling with streaming subscriptions for sub-second price updates.
Spec: docs/production_improvements.md
Status: Not started

### ENH-007: Option Rolling Logic
At ~70% profit, close and roll to next strike.
Status: Implemented but untested with live market

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

### Bug Fixes
- **BUG-022**: Double-sell (bracket + exit manager) — exit flow cancels brackets first, verifies position
- **BUG-027**: Reconciliation false closes — safety check, conId matching, raises on timeout
- **BUG-028**: Scanners auto-start on restart — bot resets scans_active=false on startup
- **Phantom trades** (Meta/Microsoft): option_selector now blocks trade return on failed IB status (Cancelled/Inactive)
- **Missing DB records** (Google): reconciliation verifies adopted orphans get db_id, retries if missing
- **IB error capture**: registered errorEvent handler, captures rejection reasons per order
- 28+ additional bug fixes (BUG-001 through BUG-028)

### Enhancements
- **ENH-002**: Heartbeat monitoring — exit manager (5s), bot main (30s), scanner (60s) heartbeats to thread_status
- **ENH-003**: Error pipeline — connected log_error() to populate errors table for dashboard popup
- **ENH-004**: System status — stale/dead detection in ThreadsTab, system log viewer panel with level filtering
- **ENH-005**: Analytics v2 — 3 new charts (P&L by day of week, P&L by signal type, hold time distribution), 2 new SQL views, drilldown support
- **ENH-006**: Separate signal engine from trade management — new signal_engine.py (pure detection) + trade_entry_manager.py (orchestration), scanner reduced from 472 to 265 lines
- **ENH-009**: SPY option chain fix — prefer 0DTE chain, try multiple exchanges (SMART/AMEX/CBOE/PSE/BATS/ISE), stock qualification guard

### Infrastructure
- Database schema (8 tables + 11 analytics views + system_log)
- Bot DB integration (dual-write)
- FastAPI backend (20+ endpoints)
- React frontend (6 tabs: Trades, Analytics, Threads, Tickers, Settings)
- Docker Compose deployment (PostgreSQL, API, Frontend, pgAdmin)
- Bot manager sidecar
- IB trade ID integration (permId, conId)
- DB-based state management (replaced file-based)
- Batch IB pricing
- Centralized error handler (handle_error + safe_call)
