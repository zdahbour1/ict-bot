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
12. [Bug Fixes & Changelog](#bug-fixes--changelog)
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

## Analytics Tab

The Analytics tab provides 12 interactive charts for strategy analysis and performance tuning. All timestamps are in Pacific Time.

### Date Range Controls

- **Date picker**: Select custom start/end dates
- **Quick buttons**: "Latest" (most recent day), "5 Days", "All" (entire history)
- Charts auto-refresh every 60 seconds

### Charts

| Chart | Description | Drill-Down |
|-------|-------------|------------|
| Cumulative P&L (Timeline) | Running P&L line chart by exit time | - |
| P&L by Ticker | Bar chart per ticker | Click bar to see trades |
| P&L by Exit Hour PT | When exits are most profitable | Click bar to see trades |
| P&L by Entry Hour PT | When entries are most profitable | Click bar to see trades |
| Risk Capital by Hour PT | Premium deployed per hour | - |
| P&L by Contract Type | Pie chart: Calls vs Puts | Click to see trades |
| Contracts by Hour PT | Volume of contracts per hour | - |
| Exit Reasons | Horizontal bars by exit reason | Click to see trades |
| P&L by Day of Week | Mon-Fri performance with win rate overlay | Click day to see trades |
| P&L by Signal Type | ICT signal performance (iFVG, OB, etc.) with win rate | Click signal to see trades |
| Hold Time Distribution | Histogram of trade duration in 5-min buckets | - |

### Top Stats Cards

Six KPI cards above the charts: Best Trade, Worst Trade, Win Streak, Loss Streak, Avg Hold Time, Total Risk Capital.

### Drill-Down

Click any chart bar/segment to open a popup showing the individual trades that make up that data point. The popup shows ticker, type, entry/exit prices, P&L, reason, and hold time.

---

## Threads Tab

The Threads tab shows the health of all system components: scanner threads, exit manager, and bot main loop. It includes heartbeat monitoring, stale/dead detection, error visibility, and a system log viewer.

### Thread Status Values

| Status       | Meaning                                                       |
|--------------|---------------------------------------------------------------|
| Running      | Thread is active and processing                               |
| Scanning     | Actively evaluating price data for trade setups               |
| Idle         | Thread is alive but between scan cycles                       |
| Error        | Thread encountered an error (click error count for details)   |
| Stopped      | Thread is not running (ticker disabled or bot stopped)        |
| **STALE**    | No heartbeat for >2 minutes (yellow badge) — may be hung     |
| **DEAD**     | No heartbeat for >5 minutes (red badge) — likely crashed     |

### Heartbeat Monitoring

Each component writes a heartbeat to the database at regular intervals:

| Component | Heartbeat Interval | What It Reports |
|-----------|-------------------|-----------------|
| Scanner threads | Every 60s (each scan cycle) | Scan count, trade count, error count |
| Exit Manager | Every 5s (each monitor cycle) | Number of open trades being monitored |
| Bot Main Loop | Every 30s | Active scanner count |

The "Last Heartbeat" column shows relative time (e.g., "15s ago", "2m 30s ago") with a color-coded health dot:
- Green: healthy (heartbeat within expected interval)
- Yellow: STALE (>2 minutes since last heartbeat)
- Red: DEAD (>5 minutes since last heartbeat)

An alert banner appears at the top if any thread is stale or dead.

### Error Popup

When the Errors column shows a non-zero count, click it to open a popup showing the recent errors for that thread/ticker. Each error shows:
- Error type and timestamp
- Error message
- Full Python traceback (expandable)

### System Log Viewer

Click the **System Log** button to toggle the system log panel below the thread table. Features:
- Filter by level: All, Errors, Warnings
- Auto-refreshes every 10 seconds
- Shows: timestamp, level badge (color-coded), component, message
- Scrollable with most recent entries at top

### Error Troubleshooting

Common errors visible in the error popup or system log:

- **"Order REJECTED"** -- IB rejected the order. Check IB error code in system log for reason (margin, invalid contract, etc.)
- **"'NoneType' object has no attribute 'secType'"** -- Option contract qualification failed. May indicate wrong exchange or expired option chain.
- **"Trade entry timed out"** -- Order placement took >30 seconds. Reconciliation will adopt any orphaned fills.
- **"No IB price data"** -- Market data subscription missing for this ticker.

### Thread Recovery

Threads automatically recover from transient errors. If a thread shows STALE or DEAD:

1. Check the System Log for recent errors from that component
2. Verify IB TWS/Gateway is connected and responsive
3. Try stopping and restarting scans via the dashboard
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

### Quick Start (Recommended)

Launch everything with one command:

```bash
python start_dashboard.py
```

This starts:
1. **Docker Compose** — PostgreSQL, API, Frontend, pgAdmin
2. **Bot Manager Sidecar** — HTTP server on port 9000 that manages the bot process

Once running, open **http://localhost** and click **"Start Bot"** in the dashboard.

### How Bot Start/Stop Works

The bot must run on your host machine (not in Docker) because it needs direct access to IB TWS/Gateway. A lightweight **Bot Manager Sidecar** (`bot_manager.py`) bridges the gap:

```
Dashboard "Start Bot" button
    → FastAPI API (Docker :8000)
    → Bot Manager Sidecar (host :9000)
    → spawns python main.py as a subprocess
    → Bot connects to IB TWS (host :7497)
```

| Action | What Happens |
|--------|-------------|
| **Start Bot** | Sidecar spawns `python main.py`, bot connects to IB + PostgreSQL |
| **Stop Bot** | Sidecar sends SIGTERM to bot process, waits 10s, then kills if needed |
| **Status** | Sidecar checks if bot PID is alive, dashboard shows green/red dot |

### Manual Start (Without Sidecar)

If you prefer to start the bot directly:

```bash
# Set DATABASE_URL so bot writes to Docker PostgreSQL
DATABASE_URL=postgresql://ict_bot:ict_bot_dev@localhost:5432/ict_bot python main.py
```

The bot will:
1. Connect to PostgreSQL (read tickers + settings from DB)
2. Connect to IB TWS/Gateway
3. Start scanner threads (one per active ticker)
4. Begin scanning and trading

### Stopping Everything

- **Ctrl+C in `start_dashboard.py`** — stops sidecar, bot (if running), and Docker services
- **"Stop Bot" in dashboard** — stops only the bot, dashboard stays up
- **`docker compose down`** — stops Docker services only

### Log Files

The bot writes to multiple log destinations:

| File | Content |
|------|---------|
| `bot.log` | Main bot log (scanner activity, trades, errors) |
| `bot_stdout.log` | Stdout/stderr when launched via sidecar |
| `logs/{account}_{timestamp}.csv` | Daily trade CSV with 50 columns |

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

## Bug Fixes & Changelog

### 2026-04-09 — Dashboard Bug Fix Session

#### BUG-001: Threads show "scanning" after bot stops
**Symptom:** After stopping the bot, the Threads tab continued showing all 19 threads with "scanning" status instead of "stopped".
**Root Cause:** Bot was killed via SIGTERM but never updated the `thread_status` table before dying. Daemon threads don't get cleanup hooks.
**Fix:** Added `_shutdown_cleanup()` in `main.py` that marks all threads as "stopped" in DB. Added SIGTERM signal handler so the sidecar's graceful stop triggers the cleanup.
**Commit:** `5aa988f`

#### BUG-002: Scans/Trades/Errors counters always zero
**Symptom:** The Scans, Trades, and Errors columns in the Threads tab always showed 0 even after the bot had been running and executing trades.
**Root Cause:** Two issues: (1) `scans_today` was mapped to `self._alerts_today` (wrong field — should be a dedicated scan counter), (2) No `_scans_today` or `_errors_today` counters existed in the Scanner class.
**Fix:** Added `_scans_today` and `_errors_today` counters to Scanner. Fixed `update_thread_status()` call to pass correct counter values. Added error counter increments on timeout and exceptions.
**Commit:** `5aa988f`

#### BUG-003: No refresh button on Threads tab
**Symptom:** Threads tab auto-refreshed every 10 seconds but user had no way to force an immediate refresh.
**Fix:** Added "Refresh Now" button and "Auto-refreshes 10s" indicator to the Threads tab controls bar.
**Commit:** `5aa988f`

#### BUG-004: No PID/Thread ID for OS-level debugging
**Symptom:** When threads showed unexpected status, there was no way to verify at the OS level whether the process/thread was actually running.
**Fix:** Added `pid` and `thread_id` columns to `thread_status` table. Writer auto-detects `os.getpid()` and `threading.get_ident()` on each update. Threads tab now shows PID and Thread ID columns in monospace font. User can run `tasklist /FI "PID eq <pid>"` to verify.
**Commit:** `ffc934b`

#### BUG-005: start_all doesn't check for already-running system
**Symptom:** Running `start_all.bat` while the system was already running could start duplicate sidecar/bot processes.
**Fix:** Added pre-flight check in `start_all.bat`, `start_all.sh`, and `start_dashboard.py` that queries the sidecar at port 9000. If running, shows current status and PID, tells user to run `stop_all` first.
**Commit:** `ffc934b`

#### BUG-006: Threads and Tickers tabs not sortable
**Symptom:** Column headers in Threads and Tickers tabs were static — no way to sort by scans, errors, ticker name, etc.
**Fix:** Replaced static HTML tables with TanStack Table (same as Trades tab). All columns are now clickable for ascending/descending sort with arrow indicators.
**Commit:** `6386fa8`

#### BUG-007: KPI cards too large, wasting screen space
**Symptom:** The P&L summary on the Trades tab used 6 large cards taking up ~200px of vertical space. Threads tab had 4 large cards. This pushed the actual data below the fold.
**Fix:** Replaced large card grids with compact single-line inline stats. Trades tab P&L is now one line. Threads tab KPIs are inline text with pipe separators. Saves ~150px of vertical space on each tab.
**Commit:** `6386fa8`

#### BUG-008: No way to view error details
**Symptom:** Threads tab showed error count but clicking it did nothing. No way to see what the actual errors were.
**Fix:** Added `GET /api/errors` endpoint (filterable by ticker/thread). Error count in Threads tab is now a clickable red link that opens a modal popup showing the most recent errors in descending order with error_type, message, traceback, and timestamp.
**Commit:** `6386fa8`

#### BUG-009: No next scan time shown
**Symptom:** After a scan completed, the Last Message column showed generic text with no indication of when the next scan would occur.
**Fix:** Post-scan message now includes: "Scan #N done at HH:MM PT | X signals | Next scan ~HH:MM PT". The next scan time is calculated as current time + 60 seconds (the scan interval).
**Commit:** `6386fa8`

#### BUG-010: Trades counter not updating in real-time
**Symptom:** After a trade was placed, the Trades column in Threads tab stayed at the old count until the next scan cycle (up to 60 seconds later).
**Root Cause:** The counter was incremented in Python but only written to DB at the start of the next scan via `update_thread_status()`.
**Fix:** Added an immediate `update_thread_status()` call right after `add_trade()` succeeds, so the DB reflects the new trade count within seconds.
**Commit:** `6386fa8`

### 2026-04-08 — Bot Start/Stop from Dashboard

#### BUG-011: "Start Bot" button does nothing
**Symptom:** Clicking "Start Bot" in the dashboard returned 200 OK but bot didn't actually start.
**Root Cause:** The FastAPI API in Docker tried to spawn `python main.py` as a subprocess inside the container, but the bot needs to run on the host machine for IB TWS connectivity.
**Fix:** Implemented Bot Manager Sidecar architecture: `bot_manager.py` runs on the host (port 9000) and manages the bot process. The API in Docker calls the sidecar via `host.docker.internal:9000`. Added `start_dashboard.py` as single-command launcher.
**Commit:** `b2745bb`

#### BUG-012: pgAdmin email validation error
**Symptom:** pgAdmin container crashed on startup with "admin@ict-bot.local does not appear to be a valid email address".
**Root Cause:** The `.local` TLD is a reserved/special-use domain rejected by pgAdmin's email validator.
**Fix:** Changed email to `admin@ictbot.com`. Added `pgadmin-servers.json` to pre-configure the database connection.
**Commit:** `07c4564`

#### BUG-013: Socket.IO API initialization error
**Symptom:** API container crashed with `TypeError: ASGIApp.__init__() got an unexpected keyword argument 'other_app'`.
**Root Cause:** The `python-socketio` library changed the `ASGIApp` constructor signature — `other_app` became a positional argument.
**Fix:** Changed `socketio.ASGIApp(sio, other_app=app)` to `socketio.ASGIApp(sio, app)`.
**Commit:** `a3a5830`

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
