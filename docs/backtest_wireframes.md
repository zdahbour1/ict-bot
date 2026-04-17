# Backtest Framework — Wireframes & Flow Diagrams

## 1. Backtest Engine Flow

```
┌──────────────────────────────────────────────────────────────────┐
│                    BACKTEST EXECUTION FLOW                        │
│                                                                   │
│  USER                        SYSTEM                              │
│  ─────                       ──────                              │
│                                                                   │
│  1. Configure Run            ┌─────────────────────┐             │
│     ─ Tickers               │ Dashboard UI         │             │
│     ─ Date Range            │ BacktestTab          │             │
│     ─ Strategy Config       └──────────┬──────────┘             │
│     ─ Click [Run]                      │                         │
│                                         ▼                         │
│                              ┌─────────────────────┐             │
│                              │ POST /api/backtests  │             │
│                              │ ─ Validate params    │             │
│                              │ ─ Create DB run row  │             │
│                              │   (status=pending)   │             │
│                              └──────────┬──────────┘             │
│                                         │                         │
│                                         ▼                         │
│                              ┌─────────────────────┐             │
│                              │ Background Thread    │             │
│  2. Monitor Progress         │ ─ status → running   │             │
│     ─ See status badge      │ ─ Update thread_status│             │
│     ─ See trade count       └──────────┬──────────┘             │
│                                         │                         │
│                                         ▼                         │
│                              ┌─────────────────────┐             │
│                              │ FOR EACH DAY:        │             │
│                              │                      │             │
│                              │ a. Fetch 1m bars     │             │
│                              │    (yfinance)        │             │
│                              │                      │             │
│                              │ b. Aggregate to      │             │
│                              │    1h, 4h timeframes  │             │
│                              │                      │             │
│                              │ c. Compute levels    │             │
│                              │    (levels.py)       │             │
│                              │                      │             │
│                              │ d. SignalEngine       │             │
│                              │    .detect()         │             │
│                              │    (REUSED from live)│             │
│                              │                      │             │
│                              │ e. FOR EACH SIGNAL:  │             │
│                              │    ─ Check entry gates│            │
│                              │    ─ Simulate fill   │             │
│                              │    ─ Walk bars forward│            │
│                              │    ─ evaluate_exit() │             │
│                              │      (REUSED)        │             │
│                              │    ─ Record trade    │             │
│                              │      to DB           │             │
│                              └──────────┬──────────┘             │
│                                         │                         │
│                                         ▼                         │
│                              ┌─────────────────────┐             │
│                              │ Calculate Metrics    │             │
│                              │ ─ Win rate           │             │
│                              │ ─ Profit factor      │             │
│                              │ ─ Sharpe ratio       │             │
│                              │ ─ Max drawdown       │             │
│                              │ ─ Streak analysis    │             │
│                              │ ─ Update run row     │             │
│  3. View Results             │   (status=completed) │             │
│     ─ Summary cards         └──────────┬──────────┘             │
│     ─ Charts                           │                         │
│     ─ Trade list                       ▼                         │
│     ─ Export Excel           ┌─────────────────────┐             │
│     ─ Compare runs           │ Results Available    │             │
│                              │ in Dashboard         │             │
│                              └─────────────────────┘             │
└──────────────────────────────────────────────────────────────────┘
```

## 2. Backtest Tab — Main Screen Wireframe

```
┌─────────────────────────────────────────────────────────────────────┐
│  ICT Trading Bot          [Trades] [Analytics] [Threads] [Tickers] │
│                           [Settings] [Backtest]                     │
│  ● Trading  DU1566080  19 tickers    15s▾  [Stop Scans] [Stop Bot] │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌─── New Backtest ──────────────────────────────────────────────┐  │
│  │                                                                │  │
│  │  Name: [ICT Strategy - April 2026          ]                  │  │
│  │                                                                │  │
│  │  Tickers: [QQQ ✕] [SPY ✕] [AAPL ✕] [AMD ✕] [+ Add]         │  │
│  │                                                                │  │
│  │  Period:  [2026-03-01] ──to── [2026-04-17]                    │  │
│  │           [Last 30 Days] [Last 60 Days] [Last 90 Days] [YTD]  │  │
│  │                                                                │  │
│  │  Strategy Configuration:                                       │  │
│  │  ┌──────────────┬──────────────┬──────────────┐               │  │
│  │  │ TP:   [100%] │ SL:   [60%] │ Contracts:[2]│               │  │
│  │  ├──────────────┼──────────────┼──────────────┤               │  │
│  │  │ Roll: [✓] at │ [70%] of TP │ Trail: [✓]   │               │  │
│  │  ├──────────────┼──────────────┼──────────────┤               │  │
│  │  │ Window: [7AM]│ to [9AM] PT │ Cooldown:[15m]│              │  │
│  │  ├──────────────┼──────────────┼──────────────┤               │  │
│  │  │ Max/Day: [8] │ News: [15m] │              │                │  │
│  │  └──────────────┴──────────────┴──────────────┘               │  │
│  │                                                                │  │
│  │  [▶ Run Backtest]                                             │  │
│  │                                                                │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌─── Previous Runs ─────────────────────────────────────────────┐  │
│  │                                                                │  │
│  │  Status │ Name                 │ Tickers│Trades│Win% │ P&L    │  │
│  │  ───────┼──────────────────────┼────────┼──────┼─────┼────────│  │
│  │  ✅ Done│ ICT Default April    │ 4      │ 156  │ 54% │+$2,340 │  │
│  │         │ [View] [Compare] [Export] [Delete]                   │  │
│  │  ───────┼──────────────────────┼────────┼──────┼─────┼────────│  │
│  │  ✅ Done│ No Rolling Test      │ 4      │ 142  │ 51% │+$1,890 │  │
│  │         │ [View] [Compare] [Export] [Delete]                   │  │
│  │  ───────┼──────────────────────┼────────┼──────┼─────┼────────│  │
│  │  ✅ Done│ Tight SL 40%         │ 4      │ 178  │ 48% │ +$980  │  │
│  │         │ [View] [Compare] [Export] [Delete]                   │  │
│  │  ───────┼──────────────────────┼────────┼──────┼─────┼────────│  │
│  │  🔄 Run │ Wide Window Test     │ 17     │ 34.. │ --  │ --     │  │
│  │         │ [Running... 45% complete]                            │  │
│  │                                                                │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

## 3. Backtest Results — Detail View Wireframe

```
┌─────────────────────────────────────────────────────────────────────┐
│  ← Back to Runs    ICT Default April    ✅ Completed in 45s        │
│                    Mar 1 – Apr 17, 2026  │ 4 tickers │ 156 trades  │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌─── Summary Cards ─────────────────────────────────────────────┐  │
│  │                                                                │  │
│  │  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐     │  │
│  │  │+$2,340 │ │  54.2% │ │  1.82  │ │ -$890  │ │  42min │     │  │
│  │  │Total   │ │Win Rate│ │Profit  │ │Max     │ │Avg     │     │  │
│  │  │P&L     │ │84W/72L │ │Factor  │ │Drawdown│ │Hold    │     │  │
│  │  └────────┘ └────────┘ └────────┘ └────────┘ └────────┘     │  │
│  │                                                                │  │
│  │  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐     │  │
│  │  │ +$67   │ │ -$41   │ │ 7 wins │ │ 4 loss │ │ 22     │     │  │
│  │  │Avg Win │ │Avg Loss│ │Best    │ │Worst   │ │Rolls   │     │  │
│  │  │        │ │        │ │Streak  │ │Streak  │ │        │     │  │
│  │  └────────┘ └────────┘ └────────┘ └────────┘ └────────┘     │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌─── Charts (2x3 grid) ─────────────────────────────────────────┐  │
│  │                                                                │  │
│  │  ┌─ Cumulative P&L ──────┐  ┌─ P&L by Ticker ───────┐       │  │
│  │  │    ╱──╲    ╱──────    │  │  ██  QQQ  +$890        │       │  │
│  │  │   ╱    ╲──╱           │  │  ██  AAPL +$620        │       │  │
│  │  │  ╱                    │  │  ██  AMD  +$510        │       │  │
│  │  │ ╱                     │  │  ██  SPY  +$320        │       │  │
│  │  └───────────────────────┘  └────────────────────────┘       │  │
│  │                                                                │  │
│  │  ┌─ Exit Reasons ────────┐  ┌─ P&L by Signal Type ──┐       │  │
│  │  │  TP        ████ 45    │  │  ██  LONG_iFVG +$1,200│       │  │
│  │  │  SL        ████ 38    │  │  ██  SHORT_OB  +$680  │       │  │
│  │  │  TRAIL     ███  22    │  │  ██  LONG_OB   +$340  │       │  │
│  │  │  ROLL      ██   18    │  │  ██  SHORT_iFVG+$120  │       │  │
│  │  │  TIME      ██   15    │  │                        │       │  │
│  │  │  EOD       █     8    │  │                        │       │  │
│  │  └───────────────────────┘  └────────────────────────┘       │  │
│  │                                                                │  │
│  │  ┌─ Day of Week ─────────┐  ┌─ Hold Time Dist ──────┐       │  │
│  │  │ Mon ████  +$420       │  │  ███ 0-15m   28 trades │       │  │
│  │  │ Tue ██    +$180       │  │  ████ 15-30m 42 trades │       │  │
│  │  │ Wed █████ +$680       │  │  █████ 30-60m 52 trades│       │  │
│  │  │ Thu ███   +$340       │  │  ███ 60-90m  34 trades │       │  │
│  │  │ Fri ████  +$720       │  │                        │       │  │
│  │  └───────────────────────┘  └────────────────────────┘       │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌─── Trade List ─────────────────────────────────────────────────┐  │
│  │  Filter: [All ▾] [All Tickers ▾] [All Signals ▾]  [Export]   │  │
│  │                                                                │  │
│  │  # │Date │Ticker│Dir │Entry│Exit │P&L $ │P&L%│Reason│Rolled?│  │
│  │  ──┼─────┼──────┼────┼─────┼─────┼──────┼────┼──────┼───────│  │
│  │  1 │3/1  │QQQ   │LONG│$2.40│$4.80│+$480 │100%│TP    │ No    │  │
│  │  2 │3/1  │AMD   │LONG│$1.10│$0.44│-$132 │-60%│SL    │ No    │  │
│  │  3 │3/1  │AAPL  │SHRT│$0.85│$1.45│+$120 │ 71%│ROLL  │ Yes   │  │
│  │  4 │3/1  │AAPL  │SHRT│$0.90│$1.62│+$144 │ 80%│TRAIL │ Roll→ │  │
│  │  5 │3/2  │SPY   │LONG│$3.20│$1.28│-$384 │-60%│SL    │ No    │  │
│  │  ──┼─────┼──────┼────┼─────┼─────┼──────┼────┼──────┼───────│  │
│  │  Showing 1-50 of 156          [< Prev] [1] [2] [3] [4] [Next >]│
│  └────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

## 4. Comparison View Wireframe

```
┌─────────────────────────────────────────────────────────────────────┐
│  ← Back    Compare: "ICT Default" vs "No Rolling"                   │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌─── Side-by-Side Metrics ──────────────────────────────────────┐  │
│  │                                                                │  │
│  │  Metric           │ ICT Default │ No Rolling  │ Difference    │  │
│  │  ─────────────────┼─────────────┼─────────────┼───────────────│  │
│  │  Total Trades      │ 156         │ 142         │ +14           │  │
│  │  Win Rate          │ 54.2%       │ 51.4%       │ +2.8% ✅     │  │
│  │  Total P&L         │ +$2,340     │ +$1,890     │ +$450 ✅     │  │
│  │  Profit Factor     │ 1.82        │ 1.54        │ +0.28 ✅     │  │
│  │  Max Drawdown      │ -$890       │ -$1,120     │ +$230 ✅     │  │
│  │  Avg Win           │ +$67        │ +$58        │ +$9 ✅       │  │
│  │  Avg Loss          │ -$41        │ -$39        │ -$2 ⚠️      │  │
│  │  Avg Hold (min)    │ 42          │ 38          │ +4            │  │
│  │  Sharpe Ratio      │ 1.34        │ 1.12        │ +0.22 ✅     │  │
│  │  Rolls Executed    │ 22          │ 0           │ +22           │  │
│  │  TP-to-Trail Used  │ 18          │ 18          │ 0             │  │
│  │                                                                │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌─── Overlaid Cumulative P&L ───────────────────────────────────┐  │
│  │                                                                │  │
│  │  $2,500 ┤                                  ╱── ICT Default    │  │
│  │         │                              ╱──╱                    │  │
│  │  $2,000 ┤                          ╱──╱                        │  │
│  │         │                    ╱────╱╱ ── No Rolling             │  │
│  │  $1,500 ┤               ╱──╱──╱╱                               │  │
│  │         │           ╱──╱╱──╱                                    │  │
│  │  $1,000 ┤       ╱──╱╱╱                                         │  │
│  │         │   ╱──╱╱                                               │  │
│  │    $500 ┤╱─╱╱                                                   │  │
│  │         │╱                                                      │  │
│  │      $0 ┼───────────────────────────────────────────────────── │  │
│  │         Mar 1        Mar 15       Apr 1        Apr 15          │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌─── Per-Ticker Comparison ─────────────────────────────────────┐  │
│  │                                                                │  │
│  │  Ticker │ Default P&L │ No Roll P&L │ Diff   │ Better With    │  │
│  │  ───────┼─────────────┼─────────────┼────────┼────────────────│  │
│  │  QQQ    │ +$890       │ +$720       │ +$170  │ Rolling ✅     │  │
│  │  AAPL   │ +$620       │ +$580       │ +$40   │ Rolling ✅     │  │
│  │  AMD    │ +$510       │ +$340       │ +$170  │ Rolling ✅     │  │
│  │  SPY    │ +$320       │ +$250       │ +$70   │ Rolling ✅     │  │
│  │                                                                │  │
│  └────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

## 5. Data Flow Diagram

```
┌─────────────┐     ┌──────────────┐     ┌──────────────────────────┐
│  yfinance    │────▶│ Data Provider │────▶│ 1m bars (60 days)        │
│  (free API)  │     │ (cached)      │     │ Aggregated: 1h, 4h       │
└─────────────┘     └──────────────┘     └───────────┬──────────────┘
                                                      │
                                                      ▼
                                          ┌──────────────────────────┐
                                          │ FOR EACH DAY:            │
                                          │                          │
                                          │ levels.py                │
                                          │ └─▶ Support/Resistance   │
                                          │                          │
                                          │ signal_engine.py         │
                                          │ └─▶ ICT signals          │
                                          │     (REUSED from live)   │
                                          │                          │
                                          │ FOR EACH SIGNAL:         │
                                          │ ┌────────────────┐       │
                                          │ │ Simulate Entry  │       │
                                          │ │ ─ Fill at bar   │       │
                                          │ │   open price    │       │
                                          │ │ ─ Set TP/SL     │       │
                                          │ └───────┬────────┘       │
                                          │         │                │
                                          │         ▼                │
                                          │ ┌────────────────┐       │
                                          │ │ Walk Bars       │       │
                                          │ │ Forward         │       │
                                          │ │                 │       │
                                          │ │ Each bar:       │       │
                                          │ │ ─ Update P&L    │       │
                                          │ │ ─ evaluate_exit()│      │
                                          │ │   (REUSED)      │       │
                                          │ │ ─ Check:        │       │
                                          │ │   TP? SL? Roll? │       │
                                          │ │   Trail? Time?  │       │
                                          │ │   EOD?          │       │
                                          │ └───────┬────────┘       │
                                          │         │                │
                                          │         ▼                │
                                          │ ┌────────────────┐       │
                                          │ │ Record Trade    │       │
                                          │ │ ─ INSERT into   │       │
                                          │ │   backtest_trades│      │
                                          │ │ ─ Include:      │       │
                                          │ │   entry/exit    │       │
                                          │ │   P&L, reason   │       │
                                          │ │   indicators    │       │
                                          │ │   tp_trailed?   │       │
                                          │ │   rolled?       │       │
                                          │ └────────────────┘       │
                                          └───────────┬──────────────┘
                                                      │
                                                      ▼
                                          ┌──────────────────────────┐
                                          │ Calculate Run Metrics     │
                                          │ ─ Win rate, P&L           │
                                          │ ─ Profit factor           │
                                          │ ─ Sharpe, Sortino         │
                                          │ ─ Max drawdown            │
                                          │ ─ Streaks                 │
                                          │ ─ UPDATE backtest_runs    │
                                          └──────────────────────────┘
```
