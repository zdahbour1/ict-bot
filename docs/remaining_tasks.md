# Remaining Tasks — feature/dashboard Branch

## Status: In Progress
Last updated: 2026-04-08

---

## COMPLETED (Steps 1-4)

### 1. Database Schema (db/init.sql)
- 8 tables: trades, trade_closes, trade_commands, thread_status, bot_state, errors, tickers, settings
- 12 indexes, 3 triggers, 3 views, seed data for 19 tickers + 43 settings
- ER diagram in docs/er_diagram.md

### 2. Bot DB Module (db/)
- connection.py, models.py (8 ORM models), writer.py, settings_loader.py
- Graceful degradation: bot works without DATABASE_URL

### 3. Bot DB Integration
- config.py reads from DB first, falls back to .env
- main.py writes bot_state on start/stop
- exit_manager.py: insert/update/close trades in DB, polls trade_commands
- scanner.py: updates thread_status in DB

### 4. FastAPI Backend (dashboard/)
- 20 API endpoints across 6 route modules
- Socket.IO real-time trade/thread updates
- Full CRUD for tickers and settings

---

## REMAINING — Dashboard Features (Steps 5-7)

### 5. React Frontend
- **Trades Tab**: P&L summary cards, sortable/filterable trade table, close trade modal, close all dialog
- **Threads Tab**: thread status table, log panel
- **Tickers Tab**: CRUD table with active toggle, add/edit/delete modals
- **Settings Tab**: grouped settings by category, inline editing, secrets masking, reload button
- Tech: React + Vite + TanStack Table + Tailwind CSS + Socket.IO client

### 6. Docker Compose
- 5 services: postgres, ibgateway, bot, api, frontend (nginx)
- Dockerfiles for bot, api, frontend
- docker-compose.yml with networking, volumes, env vars

### 7. Seed Scripts + E2E Testing
- Migrate tickers.txt → tickers table
- Migrate .env/config.py → settings table
- Full stack test with docker compose up

---

## REMAINING — Trading Engine Enhancements

### 8. TP → Trailing Stop at +100%
Instead of hard exit at take profit, move SL to the TP level and let the trade run.
If momentum continues, the trail protects profits. If it reverses, captures most of the move.
**Conflict with item 9**: Resolved via sequential approach — roll at 70%, trail rolled position at 100%.

### 9. Option Rolling at ~70% Profit
At ~70% of TP, close current position and open next strike to leverage momentum.
- Close current option at market
- Immediately open next OTM strike (same direction, same expiry if available)
- Calculate new TP/SL based on the rolled position's entry price
- Capture ~70% of the price difference as profit on the roll
- **Works with item 8**: Roll first, then trail the rolled position at its own 100% TP
- **Configurable**: ROLL_THRESHOLD (default 0.70), ROLL_ENABLED (default true)

### 10. IB Position Reconciliation (every 1-5 min)
- Compare open_trades.json with IB's actual positions (ib.positions())
- Detect: orphaned IB positions (bot doesn't know), phantom bot trades (not on IB)
- Orphaned positions: auto-adopt into bot tracker with calculated TP/SL
- Phantom trades: remove from bot tracker, log the discrepancy
- Surface discrepancies in the dashboard UI for user visibility
- Configurable interval: RECONCILIATION_INTERVAL_MIN (default 5)

### 11. IB Bracket Orders (OCO TP + SL)
- When placing a trade, submit as a bracket order: parent (market buy) + TP (limit sell) + SL (stop sell)
- IB enforces TP/SL server-side even if bot disconnects
- When trailing stop adjusts, update the bracket's SL leg via IB API
- On trade close by bot (time exit, EOD), cancel the remaining bracket legs
- Requires: ib_async bracket order support (parent + child orders)

### 12. Contract Validation Before Order
- Before placing an order, explicitly qualify the option contract on IB
- If qualifyContracts fails, log error and skip (don't place unvalidated order)
- Prevents "No security definition" errors that create phantom orders
- Add validation step in option_selector.py before buy_call/buy_put

### 13. Timeout Hardening
- Current: 30s timeout on trade entry, bot loses track if order fills after timeout
- Fix: On timeout, query IB for recent executions matching the symbol
- If filled: adopt the trade into open_trades with actual fill price
- If not filled: cancel the pending order on IB, clear pending flag
- Prevents the state desync between bot and IB

---

## PRIORITY ORDER (Suggested)

1. **Contract validation** (12) — quick, prevents most errors
2. **Timeout hardening** (13) — fixes root cause of state desync
3. **Bracket orders** (11) — safety net, IB enforces TP/SL independently
4. **TP → trailing stop** (8) — strategic improvement, quick to implement
5. **Option rolling** (9) — requires careful testing with real market data
6. **IB reconciliation** (10) — safety net, can run alongside other fixes
7. **React frontend** (5) — largest piece, independent of trading logic
8. **Docker Compose** (6) — depends on frontend being done
9. **Seed scripts + testing** (7) — final integration step

---

## BRANCH WORKFLOW

- `main` — stable bot for daily trading (DO NOT modify for new features)
- `feature/dashboard` — all new development
- When ready: merge feature/dashboard → main
- To trade: `git checkout main && python main.py`
- To develop: `git checkout feature/dashboard`
