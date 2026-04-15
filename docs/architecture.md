# ICT Trading Bot - Architecture

## System Overview

The ICT Trading Bot is a multi-component system that executes ICT (Inner Circle Trader) strategies on options via Interactive Brokers. It uses parallel scanner threads to detect setups across 17 tickers, executes bracket orders through IB TWS/Gateway, and provides a real-time web dashboard for monitoring and control.

```
                         +------------------+
                         |   React Frontend |
                         |   (nginx :80)    |
                         |                  |
                         | Trades | Threads |
                         | Tickers|Settings |
                         +--------+---------+
                              |   ^
                         REST | | Socket.IO
                        & WS  | | (real-time)
                              v   |
                         +--------+---------+
                         |   FastAPI API    |
                         |   (:8000)        |
                         |                  |
                         | 20 REST endpoints|
                         | Socket.IO server |
                         +--------+---------+
                              |   ^
                         SQL  | | Notify/
                              | | Triggers
                              v   |
                         +--------+---------+
                         |   PostgreSQL     |
                         |   (:5432)        |
                         |                  |
                         | 8 tables + views |
                         | triggers         |
                         +--------+---------+
                              ^   ^
                         SQL  |   | SQL
                              |   |
          +-------------------+   +-------------------+
          |                                           |
  +-------+--------+                         +--------+-------+
  |  Trading Bot   |                         |  Exit Manager  |
  |  (local)       |                         |  (thread)      |
  |                |                         |                |
  | 17 scanner     |                         | Monitor trades |
  | threads        |                         | every 5s       |
  +-------+--------+                         +--------+-------+
          |                                           |
          | IB API calls via worker queue             |
          +-------------------+------- --------------+
                              |
                              v
                    +---------+----------+
                    | IB TWS / Gateway   |
                    |                    |
                    | Market data        |
                    | Order execution    |
                    | Position mgmt      |
                    +--------------------+
```

### Data Flow Summary

```
Signal Detection --> Trade Entry Decision --> Order Execution --> Trade Monitoring --> Exit/CSV
 (SignalEngine)    (TradeEntryManager)        (IB worker)        (ExitManager)       Logging
      |                   |                       |                   |                 |
      v                   v                       v                   v                 v
  [pure logic]       PostgreSQL              PostgreSQL          PostgreSQL         PostgreSQL
  [no side fx]       (threads)               (trades)            (trades)           (trades)
                                                                                   + CSV file
```

### Signal Engine Architecture (ENH-006)

The scanner uses a clean three-layer separation:

```
Scanner (thin orchestrator)
  |
  +-- SignalEngine (strategy/signal_engine.py)
  |     Pure signal detection. No broker calls, no DB writes.
  |     - Runs ICT long + short strategies
  |     - Deduplicates signals by (type, entry_price)
  |     - Tracks seen setups to avoid re-signaling
  |     - Returns Signal dataclass objects
  |
  +-- TradeEntryManager (strategy/trade_entry_manager.py)
        Trade entry orchestration. Handles all side effects.
        - Entry gates: position conflicts, daily limits, cooldowns
        - Order placement via option_selector (30s timeout + recovery)
        - Trade enrichment (Greeks, VIX, indicators)
        - Registration with ExitManager
```

---

## Component Descriptions

### 1. React Frontend (Port 80 via nginx)

The dashboard is a React single-page application served by nginx. It provides four primary tabs for monitoring and controlling the trading system.

**Tabs:**

| Tab       | Purpose                                                        |
|-----------|----------------------------------------------------------------|
| Trades    | P&L summary cards, sortable/filterable trade table, close actions |
| Analytics | 12 interactive charts with drill-down, date range filtering, P&L by ticker/hour/day/signal |
| Threads   | Thread health with heartbeat monitoring, stale/dead detection, error popup, system log viewer |
| Tickers   | CRUD operations for traded ticker symbols                      |
| Settings  | System configuration CRUD, categorized settings                |

**Key behaviors:**
- Auto-refreshes data via Socket.IO WebSocket connection to the API
- Sends REST requests for mutations (close trade, update settings, CRUD operations)
- Displays real-time P&L calculations and trade status changes
- Provides sorting and filtering on trade table columns

### 2. FastAPI API (Port 8000)

The API layer serves as the bridge between the frontend dashboard and the PostgreSQL database. It exposes 20 REST endpoints plus a Socket.IO server for real-time push updates.

**Responsibilities:**
- Serve trade, thread, ticker, and settings data to the frontend
- Accept trade commands (close individual trade, close all)
- Manage bot lifecycle (start/stop)
- Perform ticker and settings CRUD operations
- Push real-time updates to connected WebSocket clients

**Technology:**
- FastAPI for REST endpoints
- Socket.IO for WebSocket-based real-time communication
- SQLAlchemy or asyncpg for PostgreSQL access

### 3. PostgreSQL Database (Port 5432)

The central data store for all system state. Contains 8 tables, views for aggregated data, and triggers for event propagation.

See the Database Schema section below for full details.

### 4. Trading Bot (Runs Locally)

The core trading engine runs as a local Python process (not in Docker) to maintain a direct connection to IB TWS/Gateway. It manages 17+ parallel scanner threads, an exit manager, and coordinates all IB API interactions through a centralized worker queue.

**Why local (not Docker):**
- IB TWS/Gateway requires a local network connection
- The IB API client needs low-latency access to the TWS socket
- IB credential management and trust is tied to the local machine

**Key subsystems:**
- Scanner threads (one per ticker)
- Exit manager thread
- IB worker queue (main thread)
- Flask webhook thread (for API callbacks)
- Position reconciliation (every 5 minutes)

### 5. IB TWS/Gateway

Interactive Brokers' Trader Workstation or IB Gateway provides the connection to live markets. The bot connects via the IB API (port 7497 for TWS, 4001 for Gateway).

**Provides:**
- Real-time market data (price bars, quotes)
- Order placement and management
- Position and account information
- Contract validation and lookups

---

## Database Schema

### Tables

| Table            | Purpose                                                             |
|------------------|---------------------------------------------------------------------|
| `trades`         | Primary trade records. One row per trade. Tracks entry, exit, P&L, status, IB order IDs (permId, conId), Greeks, indicators, and enriched metadata (JSONB). |
| `trade_closes`   | Records of trade close events. Links back to trades. Captures close price, time, reason, and partial fill details. |
| `trade_commands` | Command queue for trade actions. The API writes commands (e.g., close trade) that the bot polls and executes. |
| `thread_status`  | Heartbeat monitoring for all threads. Updated every 5-60s by scanners, exit manager, and bot main loop. Dashboard uses `updated_at` for stale/dead detection. |
| `bot_state`      | Singleton table (id=1) tracking bot status, IB connection, scans_active flag, stop_requested flag, and last error. |
| `errors`         | Error log table. Populated by centralized `handle_error()` via `log_error()`. Feeds the dashboard error popup per thread/ticker. |
| `system_log`     | General system log. All errors, warnings, and info events with JSONB details. Feeds the System Log viewer panel. |
| `tickers`        | List of tradeable ticker symbols with enabled/disabled flag and per-ticker contract count. |
| `settings`       | Key-value configuration store with categories (broker, strategy, exit_rules, trade_window, email, webhook, general). Supports secrets masking. |

### Relationships

```
trades
  |-- trade_closes (one-to-many: a trade can have multiple close events)
  |-- trade_commands (one-to-many: commands targeting a specific trade)

tickers
  |-- trades (one-to-many: a ticker has many trades over time)
  |-- thread_status (one-to-one: each ticker has one scanner thread)

settings
  (standalone key-value store, no foreign keys)

bot_state
  (standalone singleton table)

errors
  (standalone log table, may reference trade_id or thread)
```

### Analytics Views (11 PostgreSQL views, all Pacific Time)

| View | Purpose |
|------|---------|
| `v_trades_analytics` | Base: all trades with PT timestamps, computed entry_hour/exit_hour, contract_type, risk_capital, hold_minutes |
| `v_pnl_by_ticker` | P&L aggregated per ticker per date with win/loss/scratch counts |
| `v_pnl_by_exit_hour` | P&L aggregated by exit hour (PT) |
| `v_pnl_by_entry_hour` | P&L aggregated by entry hour (PT) |
| `v_risk_by_hour` | Risk capital deployed per entry hour |
| `v_contracts_by_hour` | Contract count per entry hour |
| `v_pnl_by_contract_type` | P&L split by Call vs Put |
| `v_pnl_by_exit_reason` | P&L by exit reason (TP, SL, trailing, manual, etc.) |
| `v_daily_summary` | Daily account-level rollup: trades, win rate, P&L, risk capital |
| `v_pnl_by_day_of_week` | Win/loss patterns by day of week (Mon-Fri) with win rate |
| `v_pnl_by_signal_type` | Performance breakdown by ICT signal type (LONG_iFVG, SHORT_OB, etc.) |

### Triggers

| Trigger | Table | Purpose |
|---------|-------|---------|
| `trg_trades_updated_at` | trades | Auto-set `updated_at` on UPDATE |
| `trg_thread_status_updated_at` | thread_status | Auto-set `updated_at` on UPDATE |
| `trg_bot_state_updated_at` | bot_state | Auto-set `updated_at` on UPDATE |

---

## Data Flow: Lifecycle of a Trade

### 1. Signal Detection (SignalEngine)

Each of the 17 scanner threads delegates to a `SignalEngine` instance:

1. Scanner fetches real-time price data from IB (1m, 1h, 4h bars)
2. `SignalEngine.detect()` runs ICT long + short strategies (pure, no side effects)
3. Detects ICT patterns: liquidity raids, displacement moves, iFVG (inverse Fair Value Gap), and Order Block entries
4. Deduplicates signals by (signal_type, entry_price) and filters already-seen setups
5. Returns a list of `Signal` dataclass objects

### 2. Trade Entry Decision (TradeEntryManager)

The scanner passes each signal to `TradeEntryManager.enter()`:

1. Checks entry gates: one-trade-per-ticker, daily limit (8), post-exit cooldown
2. If allowed, delegates order placement to `option_selector` via thread pool (30s timeout)
3. Validates the returned trade has IB order IDs (blocks phantom trades)
4. Enriches trade with Greeks, VIX, technical indicators
5. Registers trade with ExitManager (writes to DB + in-memory tracking)

### 3. Order Execution (IB Worker Queue)

1. The IB worker (main thread) dequeues the order request from `option_selector`
2. Validates the option contract on IB (tries SMART, AMEX, CBOE, PSE, BATS, ISE)
3. Places a bracket order: entry + take-profit + stop-loss (server-side on IB)
4. Captures any IB error events (errorEvent handler logs rejection reasons)
5. Returns fill confirmation with permId, conId, and any ib_error details
6. `option_selector` gates on order status: Cancelled/Inactive returns `None` (no phantom trade)

### 3. Trade Monitoring (Exit Manager)

The exit manager thread runs on a 5-second loop:

1. Queries all open trades from PostgreSQL
2. For each open trade, fetches current market data via IB worker queue
3. Evaluates exit conditions:
   - **TP hit**: Converts remaining position to trailing stop (let winners run)
   - **Option rolling**: At 70% profit, rolls the option to capture more upside
   - **SL hit**: Trade closes via IB bracket order (server-side, no bot dependency)
4. Updates trade records in PostgreSQL with current P&L, Greeks, and status

### 4. Position Reconciliation

Every 5 minutes, the bot:

1. Queries all positions from IB
2. Compares with open trades in PostgreSQL
3. Reconciles discrepancies (fills that arrived while disconnected, partial fills)
4. Updates trade records accordingly

### 5. Trade Exit and Logging

When a trade closes (via TP, SL, trailing stop, manual close, or rolling):

1. Final trade record is updated in PostgreSQL with exit price, time, P&L, and reason
2. A `trade_closes` record is created
3. Trade data is enriched with 50 columns: Greeks, technical indicators, VIX, and metadata
4. Enriched record is appended to the daily CSV file
5. Socket.IO event is emitted so the dashboard updates in real time

---

## Threading Model

The bot uses a multi-threaded architecture with careful coordination to avoid IB API thread-safety issues.

```
Main Thread (IB Event Loop + Heartbeat)
  |
  |-- Processes IB worker queue (all IB API calls)
  |-- Handles IB callbacks (fills, data, errors via errorEvent)
  |-- Runs the IB client event loop
  |-- Heartbeat: updates thread_status every 30s ("bot-main")
  |
  +-- Scanner Threads (17 threads, one per ticker)
  |     |-- Each contains: SignalEngine + TradeEntryManager
  |     |-- SignalEngine: pure signal detection (no IB calls)
  |     |-- TradeEntryManager: entry gates + order placement
  |     |-- Enqueue IB requests to the worker queue
  |     |-- Heartbeat: updates thread_status every 60s
  |     |-- Errors flow to both errors + system_log tables
  |
  +-- Exit Manager Thread (1 thread)
  |     |-- Runs every 5 seconds
  |     |-- Enqueues IB data/order requests to worker queue
  |     |-- Updates trades in PostgreSQL
  |     |-- Heartbeat: updates thread_status every 5s
  |
  +-- Flask Webhook Thread (1 thread)
        |-- Listens for API callbacks
        |-- Receives trade commands from the dashboard
        |-- Enqueues commands to worker queue
```

### Thread Summary

| Thread           | Count | Purpose                                    |
|------------------|-------|--------------------------------------------|
| Main (IB loop)   | 1     | IB event loop, processes worker queue      |
| Scanner          | 17    | One per ticker, detects trade setups       |
| Exit Manager     | 1     | Monitors open trades, manages exits        |
| Flask Webhook    | 1     | Receives commands from API/dashboard       |

---

## Error Handling Architecture

All errors flow through a centralized pipeline:

```
Exception caught → handle_error() → Python logger (file + console)
                                   → system_log table (JSONB details, traceback)
                                   → errors table (per-thread/ticker, for dashboard popup)
```

**IB Error Events:**
- `ib_client.py` registers an `errorEvent` handler on IB connect
- Captures IB error codes (201=rejected, 202=cancelled, 203=unavailable, etc.)
- Stores per-order errors in `_last_errors` dict
- Attaches `ib_error` details to order result dicts
- `option_selector.py` gates on order status — failed orders return `None`

**Dashboard Error Visibility:**
- ThreadsTab error popup fetches from `errors` table per ticker/thread
- System Log panel fetches from `system_log` table with level filtering
- Stale/dead thread detection via `updated_at` heartbeat age

---

## IB Worker Queue Pattern

### The Problem

The IB API (ibapi) is **not thread-safe**. Calling IB API methods from multiple threads simultaneously causes race conditions, corrupted state, and dropped messages.

### The Solution

All IB API interactions are funneled through a single worker queue processed on the main thread:

```
Scanner Thread 1 --|
Scanner Thread 2 --|
  ...              |----> [ IB Worker Queue ] ----> Main Thread ----> IB API
Scanner Thread 17--|                                    |
Exit Manager ------|                                    |
Flask Webhook -----|                                    v
                                                   IB TWS/Gateway
```

**How it works:**

1. Any thread needing IB data or order execution creates a work item (request + callback)
2. The work item is placed on a thread-safe queue
3. The main thread processes the queue in order, making IB API calls sequentially
4. Results are delivered back to the requesting thread via callbacks or response objects

**Benefits:**
- Guarantees thread safety for all IB interactions
- Serializes requests to avoid IB rate limits
- Centralizes error handling for IB connectivity issues
- Allows the main thread to interleave IB event processing with queue processing

---

## Docker Deployment Topology

```
Docker Compose
+----------------------------------------------------------+
|                                                          |
|  +-------------+  +-----------+  +--------+  +--------+ |
|  | PostgreSQL  |  | FastAPI   |  | React  |  | pgAdmin| |
|  | :5432       |  | API :8000 |  | :80    |  | :5050  | |
|  +-------------+  +-----------+  +--------+  +--------+ |
|                         |                                |
+-------------------------|--------------------------------+
                          | HTTP (host.docker.internal:9000)
                          v
Local Machine (Host)
+----------------------------------------------------------+
|                                                          |
|  +---------------------+                                 |
|  | Bot Manager Sidecar |  ← manages bot lifecycle        |
|  | :9000               |                                 |
|  +--------|------------+                                 |
|           | spawns / stops                                |
|           v                                              |
|  +------------------+     +----------------------+       |
|  | Trading Bot      |     | IB TWS / Gateway     |      |
|  | Python process   |---->| :7497 (TWS)          |      |
|  |                  |     | :4001 (Gateway)       |      |
|  +------------------+     +----------------------+       |
|         |                                                |
|         | Connects to PostgreSQL :5432                    |
|         | (exposed from Docker)                          |
+----------------------------------------------------------+
```

### Bot Manager Sidecar

The bot must run on the host machine (not in Docker) because it needs direct access to IB TWS/Gateway. The Bot Manager Sidecar bridges this gap:

- **`bot_manager.py`** — a lightweight HTTP server running on the host (port 9000)
- Provides `POST /start`, `POST /stop`, `GET /status` endpoints
- The FastAPI API in Docker calls the sidecar via `host.docker.internal:9000`
- When you click "Start Bot" in the dashboard, the request flows: **Browser → nginx → FastAPI → sidecar → spawns python main.py**
- The sidecar also auto-restarts if it crashes (via `start_dashboard.py`)

### Single-Command Launch

```bash
python start_dashboard.py
```

This script:
1. Starts Docker Compose (PostgreSQL, API, Frontend, pgAdmin)
2. Starts the Bot Manager sidecar (port 9000)
3. Prints all URLs
4. Monitors sidecar health, restarts if it dies
5. On Ctrl+C: stops sidecar, runs `docker compose down`

**Container configuration:**
- **PostgreSQL**: Persistent volume for data. Port 5432 exposed to host for bot access.
- **FastAPI API**: Depends on PostgreSQL. Connects via Docker internal network. Port 8000 exposed.
- **React Frontend**: nginx serves static build. Proxies API requests to the API container. Port 80 exposed.
- **Trading Bot**: Runs on the host. Connects to PostgreSQL on localhost:5432 and IB on localhost:7497/4001.

---

## Security

### Secrets Masking

- The `settings` table supports a `is_secret` flag
- Secret values are masked in API responses (displayed as `********` in the dashboard)
- Full values are only accessible to the bot process reading directly from the database
- The Settings tab UI shows masked values but allows editing with new values

### Environment Variables

- Sensitive configuration (database credentials, IB connection details) stored in `.env`
- `.env` file is excluded from version control via `.gitignore`
- Docker Compose reads `.env` for container configuration

### Bracket Orders for Disconnect Safety

- Every trade is placed as an IB bracket order (entry + TP + SL)
- TP and SL orders are **server-side** on IB's infrastructure
- If the bot crashes, loses connectivity, or the machine goes down, TP and SL orders remain active on IB's servers
- This ensures no trade is ever left unprotected

### Additional Measures

- Daily log files contain no credentials (secrets are stripped)
- Database connection strings use environment variables, never hardcoded
- API does not expose raw database credentials or IB connection details

---

## Configuration Hierarchy

Settings are resolved in the following priority order (highest to lowest):

```
1. Database settings table    (highest priority - runtime overrides)
        |
        v
2. Environment variables (.env)  (deployment configuration)
        |
        v
3. Hardcoded defaults            (lowest priority - fallback values)
```

**How it works:**

1. **Database settings** (highest priority): Values in the `settings` table override everything. These can be changed at runtime via the dashboard Settings tab. Changes take effect on the next bot polling cycle or after a reload.

2. **Environment variables**: Values from the `.env` file. Used primarily for infrastructure config (database URL, IB host/port) and as defaults for settings not yet in the database.

3. **Hardcoded defaults**: Built into the bot source code. Used when neither database nor environment provides a value. These represent safe, conservative defaults.

This hierarchy allows operators to:
- Change trading parameters at runtime without restarting the bot (via dashboard)
- Override infrastructure settings per deployment (via `.env`)
- Fall back to safe defaults if configuration is missing
