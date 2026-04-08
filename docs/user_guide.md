# ICT Trading Bot - User Guide

## Table of Contents

1. [Getting Started](#getting-started)
2. [Dashboard Overview](#dashboard-overview)
3. [Trades Tab](#trades-tab)
4. [Threads Tab](#threads-tab)
5. [Tickers Tab](#tickers-tab)
6. [Settings Tab](#settings-tab)
7. [Bot Management](#bot-management)
8. [Database Access](#database-access)
9. [Trading Behavior](#trading-behavior)
10. [Configuration Reference](#configuration-reference)
11. [Troubleshooting](#troubleshooting)
11. [API Reference](#api-reference)

---

## Getting Started

### Prerequisites

- **Docker** and **Docker Compose** (for PostgreSQL, API, and frontend)
- **Python 3.10+** (for the trading bot)
- **Interactive Brokers TWS or IB Gateway** installed and running
- IB account with market data subscriptions for traded tickers
- IB API connections enabled in TWS/Gateway settings (Configure > API > Settings)

### Installation

1. **Clone the repository** and switch to the feature branch:

   ```bash
   git clone <repository-url>
   cd ict-bot
   git checkout feature/dashboard
   ```

2. **Configure environment variables.** Copy the example and edit:

   ```bash
   cp .env.example .env
   ```

   Required variables:
   ```
   POSTGRES_USER=ict_bot
   POSTGRES_PASSWORD=<your-password>
   POSTGRES_DB=ict_trading
   POSTGRES_HOST=localhost
   POSTGRES_PORT=5432
   IB_HOST=127.0.0.1
   IB_PORT=7497
   IB_CLIENT_ID=1
   ```

3. **Start Docker services** (PostgreSQL, API, Frontend):

   ```bash
   docker compose up -d
   ```

   This starts:
   - PostgreSQL on port 5432
   - FastAPI API on port 8000
   - React frontend (nginx) on port 80

4. **Install bot dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

5. **Start IB TWS or Gateway** and log in. Ensure API connections are enabled.

6. **Start the trading bot:**

   ```bash
   python main.py
   ```

7. **Open the dashboard** at `http://localhost` in your browser.

### Verifying the Setup

- Dashboard loads with four tabs
- Threads tab shows scanner threads initializing
- Bot state shows "Running" after startup completes
- No errors in the Threads tab error column

---

## Dashboard Overview

The dashboard is a single-page React application with four tabs, accessible at `http://localhost`. It auto-refreshes via WebSocket (Socket.IO), so data updates appear in real time without manual page reloads.

### Tab Layout

| Tab      | Purpose                                                     |
|----------|-------------------------------------------------------------|
| Trades   | View all trades, P&L summaries, close trades                |
| Threads  | Monitor scanner thread health and status                    |
| Tickers  | Manage which tickers the bot trades                         |
| Settings | View and edit bot configuration                             |

### Real-Time Updates

The dashboard maintains a persistent WebSocket connection to the API. When the bot writes new data to PostgreSQL, the API pushes updates to all connected dashboard clients. You do not need to refresh the page to see new trades, thread status changes, or settings updates.

---

## Trades Tab

The Trades tab is the primary monitoring view. It shows P&L summary cards at the top and a detailed trade table below.

### P&L Summary Cards

At the top of the Trades tab, summary cards display aggregated performance metrics:

- **Total P&L** -- Net profit/loss across all trades
- **Today's P&L** -- Profit/loss for the current trading day
- **Open Trades** -- Count of currently active positions
- **Win Rate** -- Percentage of closed trades that were profitable

Cards update in real time as trades open, close, or change value.

### Trade Table

The main trade table shows one row per trade with detailed columns:

**Key columns include:**
- Ticker symbol
- Direction (long/short)
- Entry price and time
- Current price (live for open trades)
- Take-profit and stop-loss levels
- Unrealized / realized P&L
- Status (open, closed, rolling)
- Greeks (delta, gamma, theta, vega)
- Exit reason (TP, SL, trailing, manual, rolled)

### Sorting and Filtering

- **Click any column header** to sort ascending. Click again for descending.
- **Filter controls** at the top of the table allow filtering by:
  - Ticker symbol
  - Trade status (open, closed, all)
  - Date range
  - Direction (long, short, all)

### Close Trade

To manually close an individual trade:

1. Locate the trade in the table
2. Click the **Close** button on that row
3. Confirm the close action in the dialog
4. The bot receives the command and submits a market close order to IB
5. The trade status updates to "closed" once the fill is confirmed

### Close All

To close all open trades at once:

1. Click the **Close All** button above the trade table
2. Confirm in the dialog
3. All open positions are closed via market orders on IB
4. Trades update as fills are confirmed

**Warning:** Close All sends market orders for every open position. In fast-moving markets, fills may differ from displayed prices.

---

## Threads Tab

The Threads tab shows the status of each scanner thread (one per ticker) and the exit manager thread.

### Thread Status Values

| Status       | Meaning                                                       |
|--------------|---------------------------------------------------------------|
| Running      | Thread is active and scanning normally                        |
| Scanning     | Actively evaluating price data for trade setups               |
| In Trade     | A trade is open for this ticker; scanner pauses new entries   |
| Cooldown     | A trade recently closed; 15-minute cooldown before next entry |
| Error        | Thread encountered an error (see error column)                |
| Stopped      | Thread is not running (ticker disabled or bot stopped)        |
| Initializing | Thread is starting up, loading historical data                |

### Error Troubleshooting

When a thread shows **Error** status:

1. Check the **Error** column for the specific error message
2. Common errors:
   - **"No market data"** -- IB market data subscription missing for this ticker. Check TWS subscriptions.
   - **"Contract not found"** -- Invalid ticker symbol or expired option contract. Check the Tickers tab.
   - **"Connection lost"** -- IB connection dropped. Check that TWS/Gateway is running.
   - **"Rate limited"** -- Too many IB API requests. The bot will auto-recover.
3. Most errors are transient and the thread will auto-recover on its next scan cycle
4. Persistent errors may require checking IB TWS/Gateway or the bot logs in the `logs/` directory

### Thread Recovery

Threads automatically attempt to recover from errors. If a thread remains in Error status for an extended period:

1. Check the bot console output or daily log file in `logs/`
2. Verify IB TWS/Gateway is connected and responsive
3. Try disabling and re-enabling the ticker in the Tickers tab
4. As a last resort, restart the bot

---

## Tickers Tab

The Tickers tab manages which ticker symbols the bot trades. Each ticker gets its own scanner thread.

### Adding a Ticker

1. Click **Add Ticker**
2. Enter the ticker symbol (e.g., `AAPL`, `SPY`)
3. The ticker is added in an **enabled** state
4. A new scanner thread starts on the next bot polling cycle

### Removing a Ticker

1. Locate the ticker in the list
2. Click **Delete**
3. Confirm the deletion
4. The scanner thread for that ticker stops

**Note:** Removing a ticker does not close any open trades for that symbol. Open trades continue to be monitored by the exit manager.

### Enabling / Disabling Tickers

- Toggle the **Enabled** switch to disable a ticker without deleting it
- Disabled tickers retain their configuration but their scanner thread is stopped
- Re-enabling a ticker restarts its scanner thread

### When Changes Take Effect

- **New tickers**: The bot picks up new tickers on its next polling cycle (typically within seconds)
- **Disabled tickers**: The scanner thread stops at the end of its current scan cycle
- **Re-enabled tickers**: A new scanner thread starts on the next polling cycle
- **Deleted tickers**: Thread stops immediately; ticker is removed from the database

---

## Settings Tab

The Settings tab provides a categorized view of all bot configuration values.

### Categories

Settings are organized into logical categories:

| Category    | Description                                                  |
|-------------|--------------------------------------------------------------|
| Trading     | Trade parameters: position size, TP/SL ratios, cooldown     |
| Strategy    | ICT strategy settings: raid thresholds, displacement params  |
| Risk        | Risk management: max positions, daily loss limit             |
| IB          | Interactive Brokers connection and API settings              |
| Options     | Option-specific: rolling threshold, Greeks filters           |
| System      | System behavior: scan interval, reconciliation frequency     |
| Logging     | Log verbosity, CSV output configuration                      |

### Editing Values

1. Locate the setting you want to change
2. Click the **Edit** icon or the value field
3. Enter the new value
4. Click **Save**
5. The setting is written to the database immediately
6. The bot picks up the new value on its next configuration check

### Secrets

Some settings are marked as secrets (e.g., API keys, passwords):

- Secret values appear as `********` in the dashboard
- You can edit a secret by entering a new value (the current value is not displayed)
- Secrets are never exposed in API responses or logs

### Reload

After changing settings, most values take effect automatically on the bot's next configuration poll. For settings that require explicit reload:

1. Click the **Reload** button
2. The bot re-reads all settings from the database
3. A confirmation appears when the reload completes

---

## Bot Management

### Starting and Stopping from the Dashboard

The dashboard provides bot start/stop controls:

- **Start**: Sends a start command via the API. The bot process must already be running locally (the dashboard cannot launch the Python process). This resumes scanning and trading if the bot was in a stopped state.
- **Stop**: Sends a stop command. Scanner threads halt, but open trades remain protected by their IB bracket orders (server-side TP/SL).

### Running Locally

The bot must run on the local machine (not in Docker) because it needs direct access to IB TWS/Gateway:

```bash
# Start the bot
python main.py

# The bot will:
# 1. Connect to PostgreSQL (reads connection from .env)
# 2. Connect to IB TWS/Gateway
# 3. Load tickers and settings from the database
# 4. Start scanner threads and exit manager
# 5. Begin trading
```

### Log Files

The bot writes daily log files to the `logs/` directory:

- Filename format: `bot_YYYY-MM-DD.log`
- Logs are account-based (one file per day per account)
- Contains trade entries, exits, errors, and system events
- Credentials and secrets are stripped from log output

---

## Database Access

The system uses PostgreSQL for all trade data, settings, and ticker configuration. You can connect directly to run custom queries, inspect data, or export reports.

### Option 1: pgAdmin (Web GUI)

pgAdmin runs as part of the Docker stack on **http://localhost:5050**.

| Field | Value |
|-------|-------|
| **URL** | http://localhost:5050 |
| **Login Email** | `admin@ictbot.com` |
| **Login Password** | `admin` |

The "ICT Bot Database" server is pre-configured. Click it and enter password `ict_bot_dev` when prompted.

Features: visual schema browser, SQL query editor, data export (CSV/JSON), table viewer, ER diagrams.

### Option 2: psql via Docker (Command Line)

No installation needed -- runs inside the PostgreSQL container:

```bash
docker exec -it ict-bot-postgres-1 psql -U ict_bot -d ict_bot
```

Useful commands:
```sql
\dt                              -- list all tables
\d trades                        -- describe trades table schema
\dv                              -- list all views
SELECT * FROM tickers;           -- view all tickers
SELECT * FROM settings WHERE category = 'broker';  -- broker settings
SELECT * FROM thread_status;     -- scanner thread status
SELECT * FROM v_daily_summary;   -- daily P&L summary view
SELECT * FROM v_ticker_performance;  -- per-ticker performance view
```

### Option 3: External SQL Client

PostgreSQL port 5432 is exposed to your host machine. Connect with any SQL tool (DBeaver, DataGrip, Azure Data Studio, TablePlus, etc.):

| Field | Value |
|-------|-------|
| **Host** | `localhost` |
| **Port** | `5432` |
| **Database** | `ict_bot` |
| **Username** | `ict_bot` |
| **Password** | `ict_bot_dev` |

### Key Tables

| Table | Purpose |
|-------|---------|
| `trades` | All trades (open, closed, errored) with live P&L, enrichment JSONB |
| `trade_closes` | Partial close audit trail |
| `trade_commands` | UI-to-bot command queue (close requests) |
| `thread_status` | Scanner thread health (updated every scan) |
| `bot_state` | Bot running/stopped status (singleton) |
| `errors` | Structured error log |
| `tickers` | Tradeable instruments with active/inactive flag |
| `settings` | All bot configuration (key-value, typed, categorized) |

### Useful Views

| View | Purpose |
|------|---------|
| `v_daily_summary` | Daily P&L, win rate, trade counts per account |
| `v_ticker_performance` | All-time P&L and win rate per ticker |
| `v_pending_commands` | Active close commands waiting for bot execution |

### Example Queries

```sql
-- Today's trades with P&L
SELECT ticker, direction, entry_price, exit_price, pnl_usd, exit_reason
FROM trades WHERE entry_time::date = CURRENT_DATE ORDER BY entry_time;

-- Best performing tickers (all time)
SELECT * FROM v_ticker_performance;

-- Open trades right now
SELECT ticker, symbol, pnl_pct, peak_pnl_pct, dynamic_sl_pct
FROM trades WHERE status = 'open';

-- Trades where Greeks were captured
SELECT ticker, entry_enrichment->'entry_greeks'->>'delta' as entry_delta,
       exit_enrichment->'exit_greeks'->>'delta' as exit_delta
FROM trades WHERE status = 'closed' LIMIT 10;
```

---

## Trading Behavior

### Trade Window

The bot only enters new trades during configured market hours. Outside the trade window, scanner threads continue running but do not generate entry signals. The trade window is configurable via the Settings tab.

### Cooldown Period

After a trade closes for a given ticker, a **15-minute cooldown** period begins. During cooldown:

- The scanner thread for that ticker continues monitoring but does not enter new trades
- This prevents overtrading after a loss (revenge trading) or immediately re-entering after a win
- The cooldown duration is configurable in Settings

### One Trade Per Ticker

Each ticker is limited to **one open trade at a time**. While a trade is open:

- The scanner thread status shows "In Trade"
- No new entries are generated for that ticker
- The exit manager monitors the existing trade
- Once the trade closes and cooldown expires, the scanner resumes normal operation

### Take-Profit and Stop-Loss

Every trade is placed as an IB bracket order with:

- **Take-Profit (TP)**: Limit order above entry (calls) or below entry (puts)
- **Stop-Loss (SL)**: Stop order to limit downside

Both TP and SL are **server-side orders on IB's infrastructure**. They execute even if the bot is disconnected.

### Trailing Stop (Let Winners Run)

When a trade's take-profit is partially or fully hit:

1. The TP order fills
2. The bot converts the remaining position (if any) to a **trailing stop**
3. The trailing stop follows the price, locking in profits while allowing further upside
4. If the price reverses by the trail amount, the position closes

### Option Rolling

At **70% profit**, the bot evaluates rolling the option:

1. Closes the current option position
2. Opens a new position in a further-dated or different-strike option
3. This captures additional upside while banking partial profits
4. The roll is recorded as a linked trade in the database

### IB Bracket Order Safety

Because all trades use IB server-side bracket orders:

- If the bot crashes, TP and SL remain active on IB servers
- If the machine loses power, positions are still protected
- If the network drops, IB continues to manage the orders
- This is the primary safety mechanism for the system

---

## Configuration Reference

### Trading Settings

| Setting                | Default  | Description                                    |
|------------------------|----------|------------------------------------------------|
| `position_size`        | varies   | Dollar amount per trade                        |
| `tp_ratio`             | 2.0      | Take-profit as multiple of risk                |
| `sl_ratio`             | 1.0      | Stop-loss distance from entry                  |
| `cooldown_minutes`     | 15       | Minutes between trades on the same ticker      |
| `max_open_trades`      | 17       | Maximum simultaneous open positions            |
| `trade_window_start`   | 09:30    | Earliest time to enter trades (ET)             |
| `trade_window_end`     | 15:45    | Latest time to enter trades (ET)               |

### Strategy Settings

| Setting                | Default  | Description                                    |
|------------------------|----------|------------------------------------------------|
| `raid_threshold`       | varies   | Minimum price move to qualify as a raid         |
| `displacement_min`     | varies   | Minimum displacement candle body size           |
| `ifvg_lookback`        | varies   | Bars to look back for inverse Fair Value Gaps   |
| `ob_lookback`          | varies   | Bars to look back for Order Blocks              |

### Risk Settings

| Setting                | Default  | Description                                    |
|------------------------|----------|------------------------------------------------|
| `daily_loss_limit`     | varies   | Maximum daily loss before stopping trading      |
| `max_positions`        | 17       | Maximum number of concurrent positions          |

### Options Settings

| Setting                | Default  | Description                                    |
|------------------------|----------|------------------------------------------------|
| `roll_threshold`       | 0.70     | Profit percentage to trigger option rolling     |
| `trailing_stop_pct`    | varies   | Trail amount for converted trailing stops       |

### System Settings

| Setting                | Default  | Description                                    |
|------------------------|----------|------------------------------------------------|
| `scan_interval`        | varies   | Seconds between scanner cycles                  |
| `reconcile_interval`   | 300      | Seconds between IB position reconciliation (5m) |
| `exit_check_interval`  | 5        | Seconds between exit manager cycles              |

---

## Troubleshooting

### Common Issues

#### Bot won't connect to IB

**Symptoms:** Bot logs show connection errors, no threads start.

**Solutions:**
1. Verify IB TWS or Gateway is running and logged in
2. Check API settings in TWS: Configure > API > Settings
   - "Enable ActiveX and Socket Clients" must be checked
   - Socket port must match `IB_PORT` in `.env` (default: 7497 for TWS, 4001 for Gateway)
   - "Allow connections from localhost only" should be checked
3. Verify `IB_HOST` and `IB_PORT` in `.env`
4. Check that no other application is using the same IB client ID

#### Dashboard shows no data

**Symptoms:** Dashboard loads but all tabs are empty.

**Solutions:**
1. Check that Docker containers are running: `docker compose ps`
2. Verify the API is responding: `curl http://localhost:8000/health`
3. Check API logs: `docker compose logs api`
4. Verify PostgreSQL is accessible: `docker compose logs postgres`

#### Thread stuck in Error state

**Symptoms:** A scanner thread shows Error and does not recover.

**Solutions:**
1. Read the error message in the Threads tab
2. Check the daily log file in `logs/` for details
3. Verify IB market data subscription for that ticker
4. Try disabling and re-enabling the ticker
5. Restart the bot if the issue persists

#### Trades not appearing in the dashboard

**Symptoms:** Bot logs show trades executing but dashboard Trades tab is empty.

**Solutions:**
1. Check the WebSocket connection (browser console for Socket.IO errors)
2. Verify API is connecting to the correct PostgreSQL instance
3. Check that the database has data: query the `trades` table directly
4. Clear browser cache and reload

#### "No market data" errors

**Symptoms:** Threads show "No market data" error for specific tickers.

**Solutions:**
1. Verify IB market data subscriptions in Account Management
2. Check that the ticker symbol is valid and the contract exists
3. Ensure market hours apply (some data is only available during RTH)
4. IB may rate-limit data requests; wait and retry

#### Position mismatch between bot and IB

**Symptoms:** Dashboard shows different positions than TWS.

**Solutions:**
1. Wait for the next reconciliation cycle (runs every 5 minutes)
2. Check bot logs for reconciliation results
3. Verify the bot is connected to the correct IB account
4. Manually check positions in TWS and compare with the `trades` table

#### Docker containers won't start

**Symptoms:** `docker compose up` fails or containers exit immediately.

**Solutions:**
1. Check `.env` file exists and has valid values
2. Verify port availability (80, 5432, 8000 not in use)
3. Check Docker logs: `docker compose logs`
4. Ensure Docker has sufficient resources allocated
5. Try `docker compose down` then `docker compose up -d`

---

## API Reference

The FastAPI API exposes 20 REST endpoints on port 8000, plus a Socket.IO WebSocket server.

### Health and Status

| Method | Endpoint         | Description                           |
|--------|------------------|---------------------------------------|
| GET    | `/health`        | Health check, returns API status      |
| GET    | `/bot/state`     | Current bot state (running/stopped)   |

### Trades

| Method | Endpoint                | Description                              |
|--------|-------------------------|------------------------------------------|
| GET    | `/trades`               | List all trades (supports query filters) |
| GET    | `/trades/{id}`          | Get a specific trade by ID               |
| GET    | `/trades/summary`       | Aggregated P&L summary for dashboard     |
| POST   | `/trades/{id}/close`    | Send close command for a specific trade  |
| POST   | `/trades/close-all`     | Send close command for all open trades   |

### Threads

| Method | Endpoint         | Description                           |
|--------|------------------|---------------------------------------|
| GET    | `/threads`       | List all scanner thread statuses      |
| GET    | `/threads/{id}`  | Get a specific thread's status        |

### Tickers

| Method | Endpoint           | Description                          |
|--------|--------------------|--------------------------------------|
| GET    | `/tickers`         | List all tickers                     |
| POST   | `/tickers`         | Add a new ticker                     |
| PUT    | `/tickers/{id}`    | Update a ticker (enable/disable)     |
| DELETE | `/tickers/{id}`    | Remove a ticker                      |

### Settings

| Method | Endpoint            | Description                          |
|--------|---------------------|--------------------------------------|
| GET    | `/settings`         | List all settings (secrets masked)   |
| GET    | `/settings/{key}`   | Get a specific setting               |
| PUT    | `/settings/{key}`   | Update a setting value               |

### Bot Control

| Method | Endpoint         | Description                           |
|--------|------------------|---------------------------------------|
| POST   | `/bot/start`     | Send start command to the bot         |
| POST   | `/bot/stop`      | Send stop command to the bot          |

### Errors

| Method | Endpoint         | Description                           |
|--------|------------------|---------------------------------------|
| GET    | `/errors`        | List recent errors                    |

### WebSocket (Socket.IO)

Connect to `ws://localhost:8000` with a Socket.IO client. The server emits the following events:

| Event              | Payload               | Description                        |
|--------------------|-----------------------|------------------------------------|
| `trade_update`     | Trade object          | A trade was created or updated     |
| `thread_update`    | Thread status object  | A thread status changed            |
| `bot_state`        | Bot state object      | Bot started or stopped             |
| `error`            | Error object          | A new error was logged             |
| `settings_update`  | Settings object       | A setting was changed              |

### Authentication

The API currently does not require authentication. It is intended to run on a private network or localhost only. Do not expose port 8000 to the public internet without adding an authentication layer.
