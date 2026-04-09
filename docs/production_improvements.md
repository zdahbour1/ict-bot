# Production Improvements — Reliability & Observability

## Date: 2026-04-09
## Priority: High — These are blockers for production trading

---

## 1. Real-Time Data Streaming (IB Market Data)

### Current Problem
The exit manager uses `reqMktData` + `ib.sleep(2)` per price request, then cancels.
This is a poll-based approach — subscribe, wait, read, unsubscribe. With 18 trades,
even batch mode takes 2-3 seconds per cycle and misses price movements between cycles.

### Root Cause
IB supports two modes:
- **Snapshot** (current): `reqMktData` → sleep → read → cancel. One-shot, stale by the time we read.
- **Streaming** (needed): `reqMktData` → keep subscription → IB pushes updates. Real-time, millisecond latency.

### Solution: IB Streaming Market Data
Switch from snapshot to streaming subscriptions:
1. On trade entry: subscribe to market data for that option contract (keep subscription alive)
2. IB pushes bid/ask/last updates in real-time to a callback
3. Exit manager reads from a price cache (dict updated by callbacks) — no IB queue needed
4. On trade exit: unsubscribe

Architecture:
```
IB Gateway → streaming callbacks → price_cache dict (thread-safe)
                                         ↑
Exit manager reads from cache (instant, no IB queue wait)
Scanner reads from cache for pricing
Dashboard reads from DB (updated by exit manager)
```

Benefits:
- Sub-second price updates instead of 2-3 second cycles
- No IB queue contention between exit manager and scanners
- Price cache is always fresh — exit conditions checked against live data
- Batch pricing eliminated — everything is streaming

IB API: `ib_async` supports streaming via `reqMktData` without snapshot flag.
The `Ticker` object auto-updates bid/ask/last as IB pushes data.

### Implementation
- New module: `broker/price_stream.py` — manages streaming subscriptions
- Price cache: `{occ_symbol: {bid, ask, mid, last, timestamp}}` with thread-safe access
- Exit manager reads from cache instead of making IB calls
- Subscribe on trade entry, unsubscribe on trade exit
- Heartbeat: if a subscription goes stale (>30s no update), re-subscribe

---

## 2. Error Reporting & Visibility

### Current Problems
- SPY traded all day with errors but no detailed error messages visible
- Error count shows in Threads tab but clicking shows empty popup
- Errors not consistently logged to DB `errors` table
- Scanner errors (trade_entry_failed, timeout) not always captured
- IB errors (contract not found, order rejected) not surfaced to dashboard

### Solution: Comprehensive Error Pipeline

Every error in the system should flow through:
```
Error occurs → log.error() + db.writer.log_error() → errors table → dashboard
```

#### Error Categories
| Category | Source | Example |
|----------|--------|---------|
| `ib_connection` | IB client | Connection lost, timeout, reconnect |
| `ib_order` | Order placement | Rejected, cancelled, no security definition |
| `ib_data` | Market data | No price, subscription failed |
| `trade_entry` | Scanner | Contract validation failed, timeout |
| `trade_exit` | Exit manager | Bracket cancel failed, sell failed |
| `trade_monitor` | Exit manager | Price fetch failed, DB write failed |
| `reconciliation` | Reconciliation | Phantom trade, orphan adoption failed |
| `db_write` | DB writer | Connection lost, serialization error |
| `system` | Bot lifecycle | Startup failed, shutdown error |

#### Sanity Checks (Pre-Flight Validation)
Before every IB call, validate the request:
- **Before order**: Contract qualified? Market open? Position limit ok?
- **Before price request**: Symbol valid? Not expired? Subscription active?
- **Before bracket update**: Order ID exists? Order still active on IB?

Each sanity check failure → logged to errors table with `error_type = 'sanity_check'`

#### Error Detail in DB
Current `errors` table schema is good but needs richer usage:
```sql
errors (
    id, thread_name, ticker, trade_id,
    error_type,   -- category from table above
    message,      -- human-readable description
    traceback,    -- full Python traceback
    context,      -- JSONB with request details (symbol, params, etc.)
    created_at
)
```
Add `context JSONB` column for structured error data.

---

## 3. Process Monitoring & Heartbeat

### Current Problem
Multiple processes/threads running with no centralized health view:
- Bot manager sidecar (port 9000)
- Bot process (main.py)
- 19 scanner threads
- 1 exit manager thread
- IB connection
- Docker: PostgreSQL, API, Frontend, pgAdmin

If any of these dies silently, there's no way to know until something fails.

### Solution: Processes Tab + Heartbeat

#### New `processes` table
```sql
CREATE TABLE processes (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) UNIQUE NOT NULL,      -- 'bot', 'sidecar', 'exit_manager', 'scanner-QQQ'
    type VARCHAR(20) NOT NULL,             -- 'process', 'thread', 'service'
    pid INT,
    status VARCHAR(20) DEFAULT 'unknown',  -- 'running', 'stopped', 'error', 'stale'
    last_heartbeat TIMESTAMPTZ,
    heartbeat_interval_s INT DEFAULT 60,   -- expected heartbeat frequency
    metadata JSONB DEFAULT '{}',           -- extra info (port, host, version)
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

#### Heartbeat Mechanism
Every process/thread writes a heartbeat every 60 seconds:
```python
# In every thread's main loop:
update_heartbeat('scanner-QQQ', pid=os.getpid(), 
                 metadata={'scans': 42, 'last_signal': '10:15 PT'})
```

The dashboard checks:
- `last_heartbeat` > 2 × `heartbeat_interval_s` → status = **STALE** (yellow)
- `last_heartbeat` > 5 × `heartbeat_interval_s` → status = **DEAD** (red)

#### Processes Tab in Dashboard
Shows all processes with:
- Name, Type, PID, Status (color-coded badge)
- Last Heartbeat (relative time: "15s ago", "2m ago")
- Heartbeat graph (last 10 minutes, green dots = heartbeat received)
- Error message (if any)
- Action buttons: restart process, view logs

Processes to monitor:
| Process | Type | Heartbeat Source |
|---------|------|-----------------|
| Bot Sidecar | process | HTTP health check |
| Bot Main | process | main loop iteration |
| IB Connection | service | IB event loop ping |
| Exit Manager | thread | _check_exits() iteration |
| Scanner-QQQ | thread | _scan() iteration |
| Scanner-SPY | thread | _scan() iteration |
| ... (all 19) | thread | _scan() iteration |
| PostgreSQL | service | Docker health check |
| FastAPI API | service | /api/health response |
| Frontend | service | HTTP 200 check |
| pgAdmin | service | HTTP 200 check |

#### Alert on Process Death
If a critical process goes stale:
- Dashboard: red banner at top "⚠ Exit Manager heartbeat missed — last seen 3m ago"
- Email alert (if configured)
- Bot auto-restart attempt (if sidecar detects bot process died)

---

## 4. Implementation Priority

1. **Error pipeline** (immediate) — make every error visible in dashboard
2. **Sanity checks** (immediate) — prevent bad IB calls before they happen
3. **Streaming market data** (high) — eliminates price staleness root cause
4. **Heartbeat + Processes tab** (high) — production observability
5. **Auto-recovery** (medium) — auto-restart on process death

---

## 5. Relationship to Trade Management Engine

These improvements directly support the Trade Management Engine becoming more sophisticated:
- **Streaming data**: Sub-second exit decisions, critical for trailing stops
- **Error visibility**: Know immediately when a trade isn't being managed
- **Heartbeat**: Confidence that the exit manager is alive and working
- **Sanity checks**: Prevent bad orders that create negative positions
- **Reconciliation**: Safety net when things go wrong

The goal: a system where you can walk away from the screen and trust that
trades are being managed properly, with clear alerts when something needs attention.
