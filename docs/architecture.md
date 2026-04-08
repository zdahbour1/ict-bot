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
Signal Detection --> Order Execution --> Trade Monitoring --> Exit/CSV Logging
   (scanners)        (IB worker)        (exit manager)      (enriched data)
       |                  |                   |                    |
       v                  v                   v                    v
   PostgreSQL         PostgreSQL          PostgreSQL           PostgreSQL
   (threads)          (trades)            (trades)             (trades)
                                                               + CSV file
```

---

## Component Descriptions

### 1. React Frontend (Port 80 via nginx)

The dashboard is a React single-page application served by nginx. It provides four primary tabs for monitoring and controlling the trading system.

**Tabs:**

| Tab      | Purpose                                                        |
|----------|----------------------------------------------------------------|
| Trades   | P&L summary cards, sortable/filterable trade table, close actions |
| Threads  | Scanner thread status monitoring, error visibility             |
| Tickers  | CRUD operations for traded ticker symbols                      |
| Settings | System configuration CRUD, categorized settings                |

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
| `trades`         | Primary trade records. One row per trade. Tracks entry, exit, P&L, status, Greeks, indicators, and enriched metadata. |
| `trade_closes`   | Records of trade close events. Links back to trades. Captures close price, time, reason, and partial fill details. |
| `trade_commands` | Command queue for trade actions. The API writes commands (e.g., close trade) that the bot polls and executes. |
| `thread_status`  | Current state of each scanner thread. Updated by the bot, read by the dashboard Threads tab. |
| `bot_state`      | Singleton-style table tracking whether the bot is running, last heartbeat, start time, and configuration snapshot. |
| `errors`         | Error log table. Scanner threads and the exit manager write errors here for dashboard visibility. |
| `tickers`        | List of tradeable ticker symbols with enabled/disabled flag and configuration overrides. |
| `settings`       | Key-value configuration store with categories. Supports secrets masking. |

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

### Views and Triggers

- **Views** provide pre-aggregated data for the dashboard (e.g., P&L summaries, active trade counts)
- **Triggers** fire on trade status changes to propagate events and maintain data consistency

---

## Data Flow: Lifecycle of a Trade

### 1. Signal Detection (Scanner Thread)

Each of the 17 scanner threads monitors one ticker continuously:

1. Scanner fetches real-time price data from IB
2. Detects ICT patterns: liquidity raids, displacement moves, iFVG (inverse Fair Value Gap), and Order Block entries
3. Checks constraints: trade window, cooldown timer (15 min), one-trade-per-ticker limit
4. If a valid setup is found, queues an order request to the IB worker

### 2. Order Execution (IB Worker Queue)

1. The IB worker (main thread) dequeues the order request
2. Validates the option contract with IB (contract validation)
3. Places a bracket order: entry + take-profit + stop-loss (server-side on IB)
4. Receives fill confirmation from IB
5. Writes the new trade record to PostgreSQL `trades` table
6. Updates `thread_status` to reflect the active trade

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
Main Thread (IB Event Loop)
  |
  |-- Processes IB worker queue (all IB API calls)
  |-- Handles IB callbacks (fills, data, errors)
  |-- Runs the IB client event loop
  |
  +-- Scanner Threads (17 threads, one per ticker)
  |     |-- Each runs an independent scan loop
  |     |-- Enqueue IB requests to the worker queue
  |     |-- Write thread_status to PostgreSQL directly
  |     |-- Write errors to PostgreSQL directly
  |
  +-- Exit Manager Thread (1 thread)
  |     |-- Runs every 5 seconds
  |     |-- Enqueues IB data/order requests to worker queue
  |     |-- Updates trades in PostgreSQL
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
+-----------------------------------------------------+
|                                                     |
|  +-------------+  +-----------+  +---------------+  |
|  | PostgreSQL  |  | FastAPI   |  | React Frontend|  |
|  | :5432       |  | API :8000 |  | nginx :80     |  |
|  |             |  |           |  |               |  |
|  | Volume:     |  | Depends:  |  | Depends:      |  |
|  | pgdata      |  | postgres  |  | api           |  |
|  +-------------+  +-----------+  +---------------+  |
|                                                     |
+-----------------------------------------------------+

Local Machine (outside Docker)
+-----------------------------------------------------+
|                                                     |
|  +------------------+     +----------------------+  |
|  | Trading Bot      |     | IB TWS / Gateway     |  |
|  | Python process   |---->| :7497 (TWS)          |  |
|  |                  |     | :4001 (Gateway)       |  |
|  +------------------+     +----------------------+  |
|         |                                           |
|         | Connects to PostgreSQL :5432               |
|         | (exposed from Docker)                      |
+-----------------------------------------------------+
```

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
