# Futures Options — Implementation Plan

**Status:** 🟡 Branch `feature/futures-options` — foundation only,
real backtests blocked pending historical-data source.
**See also:** `docs/futures_options_support.md` — the broader design
document (instrument list, contract specs, margin considerations).

---

## 1. What this branch actually delivers

The bigger design in `futures_options_support.md` spans broker code,
data providers, strategy adaptation, and dashboard UI — that's weeks
of work across multiple branches. This branch ships the **minimum
viable foundation** so subsequent branches have a place to build on:

### ✅ In scope (this commit)

1. **FOP-aware contract qualification in `broker/ib_contracts.py`** —
   a new helper `qualify_futures_option()` that assembles an IB
   `FuturesOption` contract from (underlying, expiry, strike, right,
   exchange), qualifies it via IB, and caches the result. The live
   bot never calls this path today; it's there so the futures
   feature branch can wire it up incrementally.

2. **`sec_type='FOP'` flows through `insert_trade()`** — already works
   because of the roadmap-schema commit. This branch adds an
   integration test that proves a FOP trade row with non-default
   multiplier, exchange, and currency roundtrips cleanly.

3. **Seed tickers for the 6 anchor products** — MNQ, NQ, MES, ES, GC,
   CL. All start `is_active=FALSE` so the scanner never picks them;
   they become discoverable in the dashboard Tickers list with the
   right `sec_type='FOP'` + `multiplier` + `exchange` values so
   anyone enabling them gets the whole set correctly configured.

4. **Research note** on historical-data options — spelled out below
   in §4 so the "data provider for FOP backtests" can start from an
   informed place.

### ⛔ Explicitly deferred

- **FOP strategy adaptation** — the ICT strategy's point-value math
  (`ict_entry/ict_sl/ict_tp`) assumes tick-size of 0.01 on equities.
  FOP tick sizes vary (ES = 0.25, NQ = 0.25, GC = 0.10, CL = 0.01).
  Strategy needs to learn the correct tick rounding per instrument
  before it can generate valid FOP signals.
- **Live FOP order placement** — the IB flow works the same in
  principle but TWS permissions, margin requirements, and allowed
  hours differ. Needs a paper-trading canary session before any live
  code touches FOP.
- **Futures options backtest** — blocked on historical data; see §4.
- **Dashboard UI for FOP** — the existing Tickers tab displays them
  fine (the roadmap schema columns are in place). A future branch
  should add a FOP-specific Ticker add-form with helper dropdowns
  for exchange/multiplier.

---

## 2. Why no immediate strategy/backtest

ICT, ORB, and VWAP all compute signals from OHLCV bars and exit
decisions from `evaluate_exit()`. Nothing in that path specifically
requires equity options — but several subtleties need attention
before FOP trades can be generated meaningfully:

1. **Strike-selection logic.** `strategy/option_selector.py` picks ATM
   strikes for equity options rounded to the nearest 50-cent or dollar
   strike. FOP strikes come in different intervals per contract (ES
   every 5 points, NQ every 25 points, GC every 1 point). The selector
   needs per-instrument strike-interval awareness.
2. **Expiration calendar.** Equity options list daily (0DTE on QQQ,
   SPY). FOPs list Mondays/Wednesdays/Fridays weekly with quarterly
   expirations. Need per-instrument expiry calendars.
3. **Session hours.** FOPs trade nearly 24/5. Current bot windows are
   PT cash-market hours. Either FOP-specific time windows or a config
   flag to disable session-hour filtering.

Each of these deserves its own focused branch with tests. Doing them
all here would balloon the scope beyond "foundation."

---

## 3. What gets tested here

Integration tests only — all of them exercise the schema columns +
the IB contract helper with mocked IB responses (we don't need a live
IB connection to verify the contract object is built correctly).

- Seed FOP ticker rows for MNQ / NQ / MES / ES / GC / CL with their
  correct sec_type / multiplier / exchange / currency values
- Verify `qualify_futures_option()` builds a `FuturesOption`-shaped
  object with the right fields (tested against a MagicMock IB)
- Verify `insert_trade()` with sec_type='FOP' + multiplier=20 +
  exchange='CME' + underlying='MNQ' round-trips through the DB
- Verify `list_strategies` is independent of tickers (FOP is about
  instruments, not strategies)

Unit regression gate: full `pytest tests/ -q` must stay green.

---

## 4. The historical-data research — where future branches start

The unfortunate reality: **free historical FOP data is scarce.**
Catalogued here so the next branch isn't rediscovering.

| Source | FOP chains | FOP bars | Cost | Notes |
|---|---|---|---|---|
| yfinance | ❌ | ❌ | free | No futures options at all |
| IB `reqHistoricalData` | ✅ (live chain lookup) | ✅ | free with funded account | Needs active TWS; limited lookback (~1 year for bars) |
| CBOE DataShop | ✅ | ✅ | $$$ (paid, per-month-per-symbol) | Authoritative equity + index options; limited futures coverage |
| Polygon.io | ❌ for FOP directly | partial (underlying futures only) | $79+/mo Developer tier | Good for equity options; futures options not the main product |
| dxFeed | ✅ | ✅ | enterprise pricing | Professional-grade; overkill for this bot |
| CME DataMine | ✅ | ✅ | paid per dataset | Gold-standard source; 5+ years history; $$$ |
| ORATS | ✅ | partial | paid | Focused on vol surfaces |
| Databento | ✅ | ✅ | paid, per-data-package | Growing coverage, API-first, better pricing model |

### Recommended next-branch approach

1. **IB-based backtest** (most pragmatic): use IB's own historical
   data via `reqHistoricalData(useRTH=False, durationStr=...)` against
   the specific FOP contracts you want to test. Works with your
   existing paper account. Limited to ~1 year lookback for 1-min bars,
   which is more than enough for validation. No additional cost.
2. **Databento** for longer lookback if the bot moves to production
   FOP trading. Clean API, sensible pricing, FOP coverage improving.
3. **CBOE / CME DataMine** if an academic-grade long-history backtest
   is required. Pricey but canonical.

Architecturally: add `backtest_engine/data_provider_ib.py` alongside
the current `data_provider.py` (yfinance). The `run_backtest` function
would pick a provider based on `sec_type` on the ticker row. This
keeps yfinance as the free path for equity-options backtests and gives
FOP backtests an IB-backed alternative without losing the existing
setup.

---

## 5. Rollback

```sql
-- Remove seeded FOP tickers if unwanted
DELETE FROM tickers WHERE sec_type = 'FOP' AND symbol IN
    ('MNQ', 'NQ', 'MES', 'ES', 'GC', 'CL');
```

`broker/ib_contracts.py` extension is purely additive — nothing
currently calls `qualify_futures_option()` so removing it would only
affect code we haven't written yet.

---

## 6. What the next futures branch should do

In order of dependency:

1. **IB-based data provider** (`backtest_engine/data_provider_ib.py`)
   so FOP backtests are actually possible with your paper account
2. **Per-instrument tick-size + strike-interval metadata** (new
   `contract_specs` table or JSONB column on tickers)
3. **FOP-aware `option_selector.py`** using (2)
4. **First FOP backtest** — MNQ or MES (micros are cheap to test,
   commissions and margin are tolerable for paper trading)
5. **Live FOP canary** — one ticker, one contract, one day. Only
   after the backtest looks reasonable.
