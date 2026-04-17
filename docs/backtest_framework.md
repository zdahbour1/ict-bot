# Backtest Framework — Architecture & Design

## Purpose

Enable strategy analysis by running the ICT trading strategy against historical data,
storing results in PostgreSQL, and visualizing them in the dashboard. Compare different
configurations (roll %, TP/SL levels, trailing logic) to optimize the live strategy.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────���───────────┐
│                    BACKTEST FRAMEWORK                            │
│                                                                  │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐    │
│  │  Dashboard UI │────▶│  FastAPI API  │────▶│  PostgreSQL  │    │
│  │  BacktestTab  │◀────│  /backtests   │◀────│  2 new tables│    │
│  └──────────────┘     └──────┬───────┘     └──────────────┘    │
│                               │                                  │
│                        ┌──────▼─────��─┐                         │
│                        │ Backtest      │                         │
│                        │ Engine        │                         │
│                        │ (background)  │                         │
│                        └──────┬───────┘                         │
│                               │                                  │
│                    ┌──────────┼──────────┐                      │
│                    │          │          │                       │
│               ┌────▼───┐ ┌───▼────┐ ┌───▼────┐                │
│               │ Signal │ │ Exit   │ │ Data   │                 │
│               │ Engine │ │ Conds  │ │Provider│                  │
│               │(reuse) │ │(reuse) │ │(yfinance)│               │
│               └────────┘ └────────┘ └────────┘                 │
└─────────────────────────────────────────────────────────────────┘
```

## Key Principle: Reuse Live Trading Code

The backtest engine MUST use the same signal detection and exit condition code
as live trading. No separate backtest logic that can diverge.

**Reused modules:**
- `strategy/signal_engine.py` — SignalEngine.detect() for signal generation
- `strategy/exit_conditions.py` — evaluate_exit() for TP/SL/trail/roll decisions
- `strategy/ict_long.py` + `strategy/ict_short.py` — ICT strategy logic
- `strategy/levels.py` — Level computation
- `utils/occ_parser.py` — Symbol parsing

**Backtest-specific:**
- `backtest/engine.py` — New: simulation loop, fills, P&L calculation
- `backtest/data_provider.py` — New: historical data fetching (yfinance)
- No IB connection needed — simulated fills at bar prices

---

## Database Design

### Table: `backtest_runs`

Each row = one backtest execution.

```sql
CREATE TABLE backtest_runs (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100),                -- user-friendly name
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',
                    -- pending, running, completed, failed
    -- Configuration snapshot (frozen at run time)
    tickers         TEXT[] NOT NULL,              -- array of tickers tested
    start_date      DATE NOT NULL,               -- backtest period start
    end_date        DATE NOT NULL,               -- backtest period end
    config          JSONB NOT NULL DEFAULT '{}',  -- full config snapshot:
                    -- profit_target, stop_loss, roll_enabled, roll_threshold,
                    -- tp_to_trail, trade_window, cooldown, max_trades, etc.
    
    -- Results (populated on completion)
    total_trades    INT DEFAULT 0,
    wins            INT DEFAULT 0,
    losses          INT DEFAULT 0,
    scratches       INT DEFAULT 0,
    total_pnl       NUMERIC(12,2) DEFAULT 0,
    win_rate        NUMERIC(5,2) DEFAULT 0,
    avg_win         NUMERIC(12,2) DEFAULT 0,
    avg_loss        NUMERIC(12,2) DEFAULT 0,
    max_drawdown    NUMERIC(12,2) DEFAULT 0,
    sharpe_ratio    NUMERIC(8,4),
    profit_factor   NUMERIC(8,4),                -- gross profit / gross loss
    avg_hold_min    NUMERIC(8,1),
    max_win_streak  INT DEFAULT 0,
    max_loss_streak INT DEFAULT 0,
    
    -- Metadata
    error_message   TEXT,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    duration_sec    NUMERIC(10,2),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Comparison
    notes           TEXT                          -- user notes about this run
);

CREATE INDEX idx_backtest_runs_status ON backtest_runs(status);
CREATE INDEX idx_backtest_runs_created ON backtest_runs(created_at DESC);
```

### Table: `backtest_trades`

Each row = one simulated trade within a backtest run.

```sql
CREATE TABLE backtest_trades (
    id              SERIAL PRIMARY KEY,
    run_id          INT NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    
    -- Trade details (mirrors live trades table)
    ticker          VARCHAR(10) NOT NULL,
    symbol          VARCHAR(40),                 -- OCC option symbol
    direction       VARCHAR(5) NOT NULL,          -- LONG or SHORT
    contracts       INT NOT NULL DEFAULT 2,
    
    -- Pricing
    entry_price     NUMERIC(10,4) NOT NULL,
    exit_price      NUMERIC(10,4),
    pnl_pct         NUMERIC(8,4) DEFAULT 0,
    pnl_usd         NUMERIC(12,4) DEFAULT 0,
    peak_pnl_pct    NUMERIC(8,4) DEFAULT 0,
    
    -- Timing
    entry_time      TIMESTAMPTZ NOT NULL,
    exit_time       TIMESTAMPTZ,
    hold_minutes    NUMERIC(8,1),
    
    -- Signal info
    signal_type     VARCHAR(40),                 -- LONG_iFVG, SHORT_OB, etc.
    entry_bar_idx   INT,                         -- bar index at entry
    
    -- Exit info
    exit_reason     VARCHAR(20),                 -- TP, SL, TRAIL_STOP, ROLL, TIME_EXIT, EOD_EXIT
    exit_result     VARCHAR(10),                 -- WIN, LOSS, SCRATCH
    
    -- Strategy details
    tp_level        NUMERIC(10,4),               -- take profit price
    sl_level        NUMERIC(10,4),               -- stop loss price
    dynamic_sl_pct  NUMERIC(8,4),                -- trailing SL at exit
    tp_trailed      BOOLEAN DEFAULT FALSE,       -- did TP-to-trail activate?
    rolled          BOOLEAN DEFAULT FALSE,        -- was this trade rolled?
    
    -- Enrichment (same as live)
    entry_indicators JSONB DEFAULT '{}',          -- RSI, VWAP, SMAs at entry
    
    -- Metadata
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_backtest_trades_run ON backtest_trades(run_id);
CREATE INDEX idx_backtest_trades_ticker ON backtest_trades(ticker);
CREATE INDEX idx_backtest_trades_result ON backtest_trades(exit_result);
```

---

## API Endpoints

### Backtest Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/backtests` | Create and start a new backtest run |
| `GET` | `/api/backtests` | List all backtest runs with summary |
| `GET` | `/api/backtests/{id}` | Get run details + trades |
| `GET` | `/api/backtests/{id}/trades` | Get trades with filtering |
| `GET` | `/api/backtests/{id}/export` | Download Excel report |
| `DELETE` | `/api/backtests/{id}` | Delete run and its trades |
| `POST` | `/api/backtests/{id}/compare` | Compare two runs side-by-side |

### Create Backtest Request

```json
POST /api/backtests
{
    "name": "ICT Default 60-day",
    "tickers": ["QQQ", "SPY", "AAPL", "AMD"],
    "start_date": "2026-03-01",
    "end_date": "2026-04-17",
    "config": {
        "profit_target": 1.00,
        "stop_loss": 0.60,
        "roll_enabled": true,
        "roll_threshold": 0.70,
        "tp_to_trail": true,
        "max_trades_per_day": 8,
        "trade_window_start": 7,
        "trade_window_end": 9,
        "cooldown_minutes": 15
    }
}
```

---

## UI Design — BacktestTab

### Tab Layout

```
┌─────────────────────────────────────────────────────────────┐
│ [Trades] [Analytics] [Threads] [Tickers] [Settings]         │
│ [Backtest]  ← NEW TAB                                       │
├─────────────────────────────────────────────────────────────┤
��                                                              │
│  ┌─ New Backtest ─────────────────────────────────────────┐ │
│  │ Name: [ICT Default 60d    ]                             │ │
│  │ Tickers: [QQQ] [SPY] [AAPL] [AMD] [+]                 │ │
│  │ Period: [2026-03-01] to [2026-04-17]                    │ │
│  │ Config: TP [100%] SL [60%] Roll [✓ 70%] Trail [✓]     │ │
│  │ [Run Backtest]                                          │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌─ Backtest Runs ─────────────────────────────────────────┐│
│  │ Name          │ Tickers │ Trades │ Win% │ P&L    │ Status│
│  │ ICT Default   │ 4       │ 156    �� 54%  │ +$2,340│ Done  │
│  │ No Rolling    │ 4       │ 142    │ 51%  │ +$1,890│ Done  │
│  │ Tight SL 40%  │ 4       │ 178    │ 48%  │ +$980  │ Done  │
│  │ [View] [Compare] [Export] [Delete]                      ���
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌─ Selected Run: ICT Default 60d ────────────────────────┐│
│  │ ┌─ Summary Cards ──────────────────────────────────┐    ││
│  │ │ 156 trades │ 54% win │ +$2,340 │ PF: 1.8 │ MDD -$890│
│  │ └──────────────────────────────────────────────────┘    ││
│  │                                                          ││
│  │ ┌─ Charts (reuse AnalyticsTab patterns) ───────────┐    ││
│  │ │ Cumulative P&L │ P&L by Ticker │ Exit Reasons     │   ││
│  │ │ P&L by Signal  │ Hold Time Dist│ Day of Week      │   ││
│  │ └──────────────────────────────────────────────────┘    ││
│  │                                                          ││
│  │ ┌─ Trade List (sortable, filterable) ──────────────┐    ││
│  │ │ # │ Ticker │ Entry │ Exit │ P&L │ Reason │ Notes  │   ││
│  │ │ 1 │ QQQ    │ $2.40 │ $4.80│ +$480│ TP    │ Clean  │  ││
│  │ │ 2 │ AMD    │ $1.10 │ $0.44│ -$132│ SL    │        │  ││
│  │ └──────────────────────────────────────────────────┘    ││
│  └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
```

### Comparison View

Select two runs → side-by-side metrics:

```
┌─ Compare: "ICT Default" vs "No Rolling" ───────────────────┐
│ Metric          │ Default  │ No Rolling │ Diff              │
│ Total Trades    │ 156      │ 142        │ +14               │
│ Win Rate        │ 54%      │ 51%        │ +3%               │
│ Total P&L       │ +$2,340  │ +$1,890    │ +$450             │
│ Profit Factor   │ 1.82     │ 1.54       │ +0.28             │
│ Max Drawdown    │ -$890    │ -$1,120    │ +$230 (better)    │
│ Avg Hold (min)  │ 42       │ 38         │ +4                │
│ Rolls           │ 22       │ 0          │ +22               │
�� TP-to-Trail     │ 18       │ 18         │ 0                 │
└────────────────────────────────────────────────────────────┘
```

---

## Backtest Engine Flow

```
1. User configures run in UI → POST /api/backtests
2. API creates backtest_runs row (status='pending')
3. API spawns background thread/process for the backtest
4. Engine loads historical data (yfinance, 1m bars)
5. For each day in the period:
   a. Aggregate to 1h, 4h timeframes
   b. Compute levels
   c. Run SignalEngine.detect() → signals
   d. For each signal:
      - Check entry gates (limits, cooldown, one-per-ticker)
      - Simulate fill at next bar's open
      - Walk forward bar-by-bar:
        * Update P&L
        * Call evaluate_exit() — same code as live trading
        * If exit triggered → record trade, apply exit price
        * If roll triggered → close + open new simulated trade
   e. Record all trades to backtest_trades table
6. Calculate summary metrics → update backtest_runs
7. Status → 'completed'
```

### What Gets Simulated vs Reused

| Component | Simulated | Reused from Live |
|-----------|-----------|------------------|
| Market data | yfinance 1m bars | - |
| Signal detection | - | signal_engine.py |
| Exit conditions | - | exit_conditions.py |
| Order execution | Simulated fill at bar price | - |
| Bracket orders | Simulated TP/SL checks | - |
| P&L calculation | Simulated | - |
| Trade enrichment | RSI, VWAP, SMAs from bars | indicators.py |
| DB persistence | backtest_trades table | - |

---

## Benefits

1. **Strategy Optimization**: Test different TP/SL levels, roll thresholds, trailing configurations against historical data before risking real capital.

2. **A/B Comparison**: Run the same period with different configs side-by-side. Quantify the impact of rolling vs not rolling, TP-to-trail vs hard exit.

3. **Code Confidence**: Since backtest uses the same signal_engine.py and exit_conditions.py as live trading, improvements to the strategy code automatically flow into backtests.

4. **Historical Analysis**: Replay past market conditions to understand why certain trades worked or failed. Identify patterns by ticker, time of day, signal type.

5. **Configuration Tuning**: Find optimal settings for ROLL_THRESHOLD, STOP_LOSS, PROFIT_TARGET, COOLDOWN_MINUTES by running parameter sweeps.

6. **Regression Testing**: After code changes, run backtests to verify the strategy still performs as expected. Detect if a "fix" degraded performance.

---

## Implementation Order

1. **Database tables** — Create backtest_runs and backtest_trades tables
2. **Backtest engine** — New backtest/engine.py with simulation loop
3. **API routes** — /api/backtests CRUD + run
4. **UI tab** — BacktestTab with run config, results list, trade viewer
5. **Charts** — Reuse AnalyticsTab chart components for backtest results
6. **Comparison** — Side-by-side run comparison view
7. **Excel export** — Reuse trades export pattern for backtest trades

---

## Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `db/models.py` | Modify | Add BacktestRun and BacktestTrade ORM models |
| `db/backtest_schema.sql` | New | SQL DDL for new tables |
| `backtest/engine.py` | New | Simulation engine (reuses signal + exit code) |
| `backtest/data_provider.py` | New | Historical data fetching |
| `dashboard/routes/backtest.py` | New | API endpoints |
| `dashboard/app.py` | Modify | Include backtest router |
| `dashboard/frontend/src/components/BacktestTab.tsx` | New | UI tab |
| `dashboard/frontend/src/App.tsx` | Modify | Add backtest tab |

---

## Configuration Parameters (stored per run in JSONB)

| Parameter | Default | Description |
|-----------|---------|-------------|
| profit_target | 1.00 | TP as fraction of entry (100%) |
| stop_loss | 0.60 | SL as fraction of entry (60%) |
| roll_enabled | true | Enable rolling at threshold |
| roll_threshold | 0.70 | Roll at 70% of TP |
| tp_to_trail | true | Convert TP to trailing stop |
| max_trades_per_day | 8 | Daily trade limit per ticker |
| trade_window_start | 7 | Start hour PT |
| trade_window_end | 9 | End hour PT |
| cooldown_minutes | 15 | Post-exit cooldown |
| contracts | 2 | Contracts per trade |
| news_buffer_min | 15 | Skip scans near news events |

Each backtest run freezes these values at run time so results are reproducible.
