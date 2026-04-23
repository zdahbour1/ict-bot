# Design — DN Phase A alpha filters (ENH-052 + ENH-055 + ENH-056)

**Status:** Proposed
**Owner:** TBD
**Prereq:** ENH-035 (production ATM IV) shipped ✅
**Supersedes / complements:** `docs/enh_026_dn_research_and_design.md`

## Purpose

Phase A ships the three highest-expected-value edge filters the
research doc identified. Each is a ~1-day task. Together they move
the bot from *"mechanically fires DN entries whenever IV proxy is
up"* to *"fires only when every published edge factor is present."*

## 1. ENH-052 — IV-rank gate

### Problem
Today's entry gate is a flat `DELTA_NEUTRAL_IV_THRESHOLD`
comparison against either a rolling-std IV proxy or (post ENH-035)
a back-solved BS ATM IV. **A fixed cutoff doesn't adapt to the
ticker's own vol history.** IV=0.35 is high for JNJ but low for
TSLA. Literature (tastytrade, projectfinance) consistently shows
**IV rank** — today's IV vs the 52-week high/low envelope — is
the single most predictive entry filter.

### Proposed design

**New module**: `strategy/iv_rank.py`

```python
def iv_rank(ticker: str, current_iv: float, lookback_days: int = 252) -> float:
    """Return 0..100 percentile-of-range for current_iv vs the
    trailing-252d daily ATM IV time series.
    Formula (classic tastytrade): 100 * (iv - iv_min) / (iv_max - iv_min)."""
```

**Storage**: new table `iv_daily` (`ticker`, `date`, `atm_iv`) — one
row per ticker per session close. Populated by:
1. Backfill job pulling historical option chain IV from IB
   (`reqHistoricalData` on SPY options, sample one IV per day).
2. Daily append in `main.py` post-close: snapshot each active
   ticker's ATM IV, insert row. Migration 014.

**Gate wiring** — in `delta_neutral_strategy.py::detect`:

```python
iv_now = self._compute_atm_iv(bars_1m, ticker)     # already shipped
rank = iv_rank(ticker, iv_now)                      # new
if rank < settings.DN_IVR_MIN:                      # default 30
    return []                                       # skip entry
```

### Code impact
- `db/migrations/014_iv_daily.sql` — new table + index
- `strategy/iv_rank.py` — ~80 LOC
- `scripts/backfill_iv_history.py` — one-shot ~200 LOC (walks IB
  option chain for past 252 trading days, extracts ATM IV)
- `strategy/delta_neutral_strategy.py` — 10 LOC to gate
- `main.py` — daily close hook (~20 LOC)
- `tests/unit/test_iv_rank.py` — 6-8 tests (formula, edge cases,
  lookback boundary)
- Setting: `DN_IVR_MIN` default 30, tunable

### Effort
1.5 days (0.5 day implementation, 1 day backfill + reconcile)

### Risk / open questions
- IB historical option data for 252 days may require a paid data
  subscription on live accounts. Paper account: TBD — worth a
  quick check during backfill.
- Weekend/holiday gap handling — use trading-day index, not
  calendar.
- Fallback if iv_daily has <20 rows for a ticker: skip the gate
  (treat as "insufficient data, allow trade").

---

## 2. ENH-055 — 50% profit-target + 21-DTE hard-exit

### Problem
Today DN trades run to expiry or manual close. projectfinance's
71,417-trade study shows **closing at 50% of max profit and/or
21 DTE produces materially better risk-adjusted returns** than
hold-to-expiration. The bot's exit-manager already monitors
trades; just needs a new exit condition.

### Proposed design

Three new exit reasons added to `strategy/exit_conditions.py`:

```python
def check_dn_profit_target(trade, current_option_pct):
    """Close when net_credit shrinks to 50% of opened_credit."""
def check_dn_hard_21dte(trade, now):
    """Close when DTE <= 21 regardless of P&L."""
```

For multi-leg combo trades, the "current_option_pct" here is the
synthetic collapsed price (already computed by
`multi_leg_sim.synth_price` for backtest + the delta-hedger
uses the same idea live). Wire it into the exit_manager monitor
loop.

### Code impact
- `strategy/exit_conditions.py` — 2 new check functions + register
  in `evaluate_exit` — ~60 LOC
- `strategy/exit_manager.py` — for multi-leg trades, compute net
  current value and pass through — ~40 LOC
- Settings:
  - `DN_PROFIT_TARGET_PCT` default 0.50
  - `DN_HARD_EXIT_DTE` default 21
- `tests/unit/test_dn_exits.py` — 6 cases (hit target, DTE boundary,
  both hit simultaneously, partial close, DTE > 21)

### Effort
1 day.

### Risk
- DTE on multi-leg trades: take the minimum across legs (safest
  for iron condors where all legs share the same expiry anyway).
- The profit-target check fires on *net credit remaining* not
  net P&L — need to persist `opened_credit` on the trade envelope
  at entry. Add column `trades.opened_credit NUMERIC(10,4)`.

---

## 3. ENH-056 — Earnings + FOMC/CPI blackout filter

### Problem
Two worst DN loss archetypes: (1) trade entered within 2 days of
earnings, IV crushes but underlying gaps, (2) trade held through
FOMC / CPI, IV expands but wings aren't wide enough.

### Proposed design

**Entry gate** added to `can_enter()`:

```python
if _is_within_blackout(ticker, entry_time, hold_days=settings.DN_MAX_HOLD_DAYS):
    return False, "blackout window (earnings / FOMC / CPI)"
```

**Two data sources:**

1. **Earnings**: pulled from `client.reqFundamentalData(contract, "CalendarReport")`
   once per day at open, cached in DB `ticker_events`.
2. **Macro events**: a small static CSV `data/macro_events.csv`
   maintained manually for the known FOMC / CPI / NFP dates of
   the quarter. Loaded into `macro_events` DB table.

```python
CREATE TABLE ticker_events (
  ticker VARCHAR(10),
  event_type VARCHAR(20),      -- 'earnings' | 'fomc' | 'cpi' | 'nfp'
  event_date DATE,
  PRIMARY KEY (ticker, event_type, event_date)
);
```

### Code impact
- `db/migrations/015_ticker_events.sql` — new tables
- `strategy/blackout.py` — ~120 LOC (calendar lookup, date math)
- `scripts/refresh_earnings_calendar.py` — ~200 LOC (calls IB daily)
- `data/macro_events.csv` — hand-maintained, 20-30 rows/quarter
- `strategy/trade_entry_manager.py::can_enter` — 5 LOC gate
- Settings:
  - `DN_BLACKOUT_ENABLED` default true
  - `DN_MAX_HOLD_DAYS` default 45
  - `DN_BLACKOUT_BUFFER_DAYS` default 2 (skip entry within N days of event)
- `tests/unit/test_blackout.py` — 5 cases

### Effort
1.5 days (0.5 day code, 1 day populating + verifying earnings
calendar for the active tickers).

### Risk
- IB fundamental data quality varies by ticker (small-caps miss
  entries). Keep static CSV as override.
- Time-zone handling on event dates — use UTC consistently.

---

## Phase A combined delivery plan

| Day | Work |
|-----|------|
| Day 1 | Migration 014 + 015 + iv_daily backfill script (ENH-052 + ENH-056 foundation) |
| Day 2 | `iv_rank.py` + integration + 8 tests + ship ENH-052 |
| Day 3 | ENH-055 exit conditions + multi-leg wiring + 6 tests |
| Day 4 | ENH-056 blackout + earnings calendar refresh script + 5 tests |
| Day 5 | Live validation on paper; populate macro_events.csv for Q2 |

## Rollback switches

Each feature gated by a setting; flip `false` to disable:

| Setting | Default | Disable behavior |
|---------|---------|------------------|
| DN_IVR_MIN | 30 | Set to 0 → no IVR filter |
| DN_PROFIT_TARGET_PCT | 0.50 | Set to 0 → no profit-target exit |
| DN_HARD_EXIT_DTE | 21 | Set to 0 → no DTE exit |
| DN_BLACKOUT_ENABLED | true | Set false → no blackout filter |

## Dependencies

- ENH-035 (BS implied vol lookup) — shipped, blocks ENH-052
- ENH-047 (leg drill-down UI) — shipped, helpful for debug
- ENH-050 (fill-price recovery) — shipped, needed to trust
  `opened_credit` column for ENH-055

## Combined effort
**Phase A total: 5 days** for one engineer, including backfills.
