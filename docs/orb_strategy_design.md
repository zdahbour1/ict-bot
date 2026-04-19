# Opening Range Breakout (ORB) — Strategy Design

**Status:** 🟢 Active development — `feature/orb-live` branch.
**Depends on:** `docs/active_strategy_design.md` (strategies table),
`docs/roadmap_schema_extensions.md` (forward-compatible columns).

---

## 1. What ORB is

A classic intraday breakout strategy:

1. The first **N minutes** of the trading session (5 / 15 / 30 / 60)
   define the **opening range** — its high is `range_high`, its low is
   `range_low`, midpoint is `range_mid`.
2. After that window closes, the strategy watches for a **close** that
   moves decisively **above `range_high`** (→ LONG) or **below
   `range_low`** (→ SHORT), optionally with a buffer margin to filter
   marginal pokes.
3. Stop loss sits at the range midpoint. Take profit sits one range
   width beyond the entry (1:1 R:R on the range size).
4. Exactly one signal per direction per day. Once a trade exits, no
   re-entry that same session.

### Why it complements ICT

| Aspect | ICT | ORB |
|---|---|---|
| When it fires | Throughout the session | Only after the range forms (first 15–60 min) |
| Signal type | Liquidity raid + displacement + iFVG/OB | Simple breakout above/below range |
| Complexity | Multi-step confirmation chain | Two conditions (range + breakout close) |
| Best market regime | Trending + ranging | Strong directional open |
| Max trades/day | ~8 (ticker-capped) | 2 (one long, one short) |

They fire on different setups so trade overlap is low. Under the current
"one active strategy at a time" model you can't run them concurrently,
but you can backtest them against the same period and compare.

---

## 2. Backtested performance (public benchmarks)

From published research (all cited in `docs/strategy_plugin_framework.md`):

- **60-minute ORB on SPY 0DTE**: 89.4% win rate, profit factor 1.44
  ([QuantifiedStrategies](https://www.quantifiedstrategies.com/opening-range-breakout-strategy/))
- **5-minute ORB on SPY 0DTE**: 40–42% win rate but **positive
  expectancy**: +$14,860 over 303 trades
  ([Options Cafe](https://options.cafe/blog/0dte-opening-range-breakout-strategy-spy-backtested-results/))
- Real-money reports on liquid ETFs claim triple-digit annual returns
  with strict risk controls — take with salt; those numbers come from
  narrow windows

The backtest run in this branch will produce **our own numbers** against
yfinance 5m bars using the same `evaluate_exit` logic as live trading.
Published numbers are orientation, not validation.

---

## 3. Implementation status

| Piece | Status | Where |
|---|---|---|
| Plugin class `ORBStrategy` | ✅ implemented | `strategy/orb_strategy.py` (on enh-024) |
| `BaseStrategy` inheritance | ✅ | includes `name`, `description`, `detect`, `configure`, `reset_daily`, `mark_used` |
| Registered with `StrategyRegistry` | ✅ | `@StrategyRegistry.register` decorator |
| Unit tests | ✅ 11 passing | `tests/unit/test_orb_strategy.py` — synthetic bar data |
| Strategies-table row | ✅ seeded `enabled=FALSE` | via `db/roadmap_schema.sql` |
| Backtest runner uses plugin path | ❌ → fixing in this branch | `run_backtest_engine.py` currently passes `strategy=None` regardless of which strategy_id was picked |
| Real-data backtest | ❌ → running in this branch | Never exercised against yfinance bars |
| Live scanner wiring | ⏳ deferred | Comes with rollouts #2-#4 of `active_strategy_design.md` — the scanner still hardcodes `SignalEngine(ticker)` today |

---

## 4. What this branch delivers

### Scope
1. **Strategy activation:** flip `orb.enabled=TRUE` in `strategies` table via
   an idempotent SQL seed (`db/enable_orb.sql`). Now `orb` shows up as a
   selectable option in the backtest launch dialog and
   `/api/backtests/strategies`.

2. **Dynamic strategy instantiation in the runner:**
   `run_backtest_engine.py` reads `class_path` from the `strategies` row
   and uses `importlib` to instantiate the class. Non-ICT strategies are
   passed via the plugin path in `run_backtest(strategy=instance, ...)`;
   ICT keeps its legacy fast path (still equivalent behavior, but no
   dynamic import overhead for the most-run strategy).

3. **Engine levels pre-calc skip:** ORB doesn't need session levels /
   raids. Engine already catches per-strategy exceptions in
   `get_all_levels()` — no change needed there, but confirmed.

4. **Real ORB backtest:** a saved run targeting QQQ+SPY+IWM over 60 days
   with the default ORB config (15-min range, 0.1% buffer, 1:1 R:R).
   Results stored in the DB, visible in the dashboard.

5. **Integration test**: a full `launch → sidecar → runner → engine →
   DB` cycle that proves the dynamic-strategy path works.

### Out of scope
- **Live wiring** — still blocked on rollouts #2-#4 of
  `active_strategy_design.md` (scanner needs to accept a `BaseStrategy`
  via `ACTIVE_STRATEGY` setting, not via hardcoded `SignalEngine(ticker)`
  in `main.py`).
- **ORB parameter tuning** — the existing defaults (15-min range, 0.1%
  buffer) are the starting point. Follow-up can sweep.
- **ORB extensions** (fade-the-breakout, range-bound stop-out) — not today.

---

## 5. The ORB config surface

These are strategy-scoped settings per the `active_strategy_design.md`
overlay pattern. They'll be added to the `settings` table with
`strategy_id = <orb_id>` by the enable-orb script:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `ORB_RANGE_MINUTES` | int | `15` | Length of the opening range |
| `ORB_BREAKOUT_BUFFER` | float | `0.001` | Extra cushion past high/low (fraction, 0.1% default) |
| `ORB_MAX_TRADES_PER_DAY` | int | `2` | One long + one short possible |
| `PROFIT_TARGET` | float | `1.00` | Option-price TP% (inherited from ICT convention) |
| `STOP_LOSS` | float | `0.60` | Option-price SL% |
| `COOLDOWN_MINUTES` | int | `15` | Between consecutive entries |

Strategy-scoped so ORB can have its own `PROFIT_TARGET` without changing
ICT's.

---

## 6. Tests (per CLAUDE.md principle)

**Already passing** (from enh-024):
- `tests/unit/test_orb_strategy.py` — 11 tests for the plugin itself
- `tests/unit/test_base_strategy.py` — StrategyRegistry + BaseStrategy contract

**New in this branch:**
- `tests/integration/test_orb_backtest.py`:
  - `orb` row is enabled after the seed
  - Dynamic strategy instantiation: `run_backtest_engine.py` resolves
    `strategy.orb_strategy.ORBStrategy` from the class_path column
  - End-to-end: run a tiny ORB backtest (2 days, 1 ticker) with mocked
    yfinance bars that force a breakout; verify at least one trade lands
    with `signal_type='ORB_BREAKOUT_LONG'`
  - Dynamic import failure path: a broken class_path raises the right
    error (doesn't silently fall back to ICT)

**Regression gate:** full `pytest tests/ -q` green (≥ 188 + new ORB tests).

---

## 7. How the dashboard flow changes

Before this branch: Strategy dropdown in Launch dialog shows `ict` only
(because only `ict` was `enabled=TRUE`). Picking `orb` was impossible
because the `/api/backtests/strategies` endpoint filters `enabled_only=TRUE`.

After this branch: the dropdown shows `ict` and `orb`. User can launch an
ORB backtest just like ICT. Results appear in the runs list with
`strategy_name='orb'` and its own Feature Analysis panel.

No UI code changes — the dashboard already queries strategies by name.
The only change is which rows the query returns.

---

## 8. Rollback

```sql
UPDATE strategies SET enabled = FALSE WHERE name = 'orb';
-- Optional: also delete ORB-scoped settings rows if we regret
DELETE FROM settings
WHERE strategy_id = (SELECT strategy_id FROM strategies WHERE name = 'orb');
```

No data migration involved. Historical backtest runs that used ORB stay
in the DB (strategy_id FK still valid, even with the strategy disabled).

---

## 9. Next steps after this branch merges

1. **VWAP implementation** — same pattern, next branch
2. **Rollouts #2-#4** of `active_strategy_design.md` — so ORB can be
   selected at bot-start time for live trading, not just backtest
3. **Parameter sweep backtest** — run ORB across 5/15/30/60 min ranges,
   compare results via a new "compare runs" view (or just side-by-side
   in the existing Backtest tab)
