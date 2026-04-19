# VWAP Mean Reversion — Strategy Design

**Status:** 🟢 Active development — `feature/vwap-revert` branch.
**Depends on:** `feature/orb-live` (dynamic strategy loader in
`run_backtest_engine.py`), `docs/roadmap_schema_extensions.md`
(sec_type/multiplier columns), `docs/active_strategy_design.md`
(strategies table).

---

## 1. What VWAP Mean Reversion is

VWAP (Volume-Weighted Average Price) is the intraday fair-value anchor
most institutional flow pivots around. A **mean-reversion** strategy
assumes price that has drifted away from VWAP has a statistical
tendency to return to it — so you enter **against** the pullback in
the direction of the prevailing trend, with VWAP as the target / anchor.

### Entry logic (the rules this branch ships)

Two conditions must both be true on the current bar:

| Leg | Condition |
|---|---|
| **Trend filter** | `1h` EMA(20) trend agrees with the side you want to trade. Bullish EMA slope → only take LONGs. |
| **Trigger** | Price is within `VWAP_TOUCH_THRESHOLD` (default 0.1%) of session VWAP from the "pullback" side. LONGs fire when price pulls DOWN to VWAP from above; SHORTs fire when price rallies UP to VWAP from below. |
| **RSI confirmation** | LONG fires only when RSI(14) < `RSI_OVERSOLD` (default 35); SHORT fires only when RSI > `RSI_OVERBOUGHT` (default 65). Filters out the "stuck at VWAP" meandering case. |

### Exit logic

Uses the shared `evaluate_exit()` — same TP/SL/trail/time/EOD framework
as ICT. TP default `+2 × ATR` from entry (measured in option price),
SL default `-1 × ATR`. Cooldown 15 min between entries.

### Why it complements ICT + ORB

| Aspect | ICT | ORB | VWAP Reversion |
|---|---|---|---|
| Market type | Any | Strong directional open | **Trending with pullbacks** |
| Entry style | Liquidity raid + displacement | Range breakout | **Mean reversion** |
| Frequency | Multiple / day | Max 2 / day (one long + one short) | Multiple per day (whenever price touches VWAP) |
| Best time | 07:00-09:00 PT | First 15-60 min | Mid-morning through afternoon |
| R:R | Variable (1:1 typical) | 1:1 on range width | **2:1 (2×ATR TP / 1×ATR SL)** |

Together they cover three different market regimes: displacement (ICT),
momentum (ORB), and controlled retracement (VWAP).

---

## 2. Published benchmarks

From research cited in `docs/strategy_plugin_framework.md`:

- [QuantifiedStrategies VWAP](https://www.quantifiedstrategies.com/vwap-trading-strategy/) — "VWAP bounce" strategy, 713% cumulative return over 3 years in one backtest (~200% annualized). Assumes unleveraged equity. Options version will have higher frequency but also higher frictions.
- [QuantVPS VWAP Python backtest](https://www.quantvps.com/blog/backtest-vwap-trading-strategy-python) — open-source implementation with ES/NQ futures
- [GitHub VwapProject](https://github.com/hedge0/VwapProject) — production VWAP bot for ES/NQ futures

All three treat VWAP as a pullback anchor in trending markets, not a
standalone signal. Same framing as this design.

Our own numbers come from the real backtest in this branch.

---

## 3. The indicator layer

This branch introduces no new global indicator primitives — everything
it needs already exists:

- **VWAP**: already implemented in `backtest_engine/indicators.py::vwap`
  (session-reset via groupby-day). Matches what the live bot would
  compute via the same math.
- **RSI**: already implemented in the same module.
- **EMA(20)**: one line with `pandas.ewm(span=20).mean()`.
- **ATR(14)**: already implemented.

Strategy module is pure orchestration — calls those, combines signals.

---

## 4. Config surface (strategy-scoped settings)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `VWAP_TOUCH_THRESHOLD` | float | `0.001` | Within 0.1% of VWAP counts as a "touch" |
| `VWAP_TREND_EMA` | int | `20` | EMA period on 1h bars for trend filter |
| `VWAP_RSI_PERIOD` | int | `14` | RSI lookback |
| `VWAP_RSI_OVERSOLD` | int | `35` | LONG fires below this |
| `VWAP_RSI_OVERBOUGHT` | int | `65` | SHORT fires above this |
| `VWAP_ATR_PERIOD` | int | `14` | ATR lookback for stops |
| `VWAP_TP_ATR_MULT` | float | `2.0` | TP = entry ± VWAP_TP_ATR_MULT × ATR |
| `VWAP_SL_ATR_MULT` | float | `1.0` | SL = entry ± VWAP_SL_ATR_MULT × ATR |
| `COOLDOWN_MINUTES` | int | `15` | Between entries |
| `PROFIT_TARGET` | float | `1.00` | Option TP% (fallback; the ATR stops override for VWAP) |
| `STOP_LOSS` | float | `0.60` | Option SL% (fallback) |

Scoped to the `vwap_revert` strategy_id so ICT + ORB keep their own
values untouched.

---

## 5. What this branch delivers

### Scope
1. **`strategy/vwap_strategy.py`** — VWAPStrategy(BaseStrategy)
   implementation. Registered with StrategyRegistry.
2. **Unit tests** — `tests/unit/test_vwap_strategy.py`: trend filter,
   RSI confirmation, VWAP touch detection, config round-trip, mark_used
   state, reset_daily state, empty-bars safety.
3. **`db/enable_vwap.sql`** — idempotent. Flips `vwap_revert.enabled=TRUE`,
   seeds scoped settings.
4. **Integration test** — `tests/integration/test_vwap_backtest.py`: row
   enabled, dynamic loader resolves class_path, full backtest smoke with
   synthetic bars.
5. **Real backtest** — QQQ+SPY+IWM over 60 days, 5m bars. Results
   in the DB, visible in the UI.

### Out of scope
- Live scanner wiring (blocked on rollouts #2-#4, same as ORB).
- Parameter sweeps (RSI thresholds, ATR multipliers).
- Higher-timeframe VWAP (weekly / monthly VWAP). Session-only for now.

---

## 6. Tests plan

Unit (pure, no DB):
- VWAP strategy instantiation + config roundtrip
- Trend filter: no signal when 1h EMA slope disagrees with direction
- RSI confirmation: no signal when RSI not oversold/overbought
- VWAP touch detection: signal fires inside threshold, not outside
- LONG/SHORT symmetry
- mark_used / reset_daily idempotence
- Empty bars handling
- Short-history handling (insufficient bars for RSI/ATR → no signal, no crash)

Integration (needs DB):
- vwap_revert row enabled after seed
- class_path resolvable via importlib
- End-to-end synthetic backtest produces ≥1 VWAP_REVERT_* trade
- Real 60-day backtest completes without error (checked post-commit)

---

## 7. Rollback

```sql
UPDATE strategies SET enabled=FALSE WHERE name='vwap_revert';
DELETE FROM settings
WHERE strategy_id = (SELECT strategy_id FROM strategies WHERE name='vwap_revert');
```

Same pattern as ORB. Historical backtest runs that used VWAP stay
intact — FK still resolves.

---

## 8. Open questions (not blocking this branch, worth discussing later)

1. Session definition — should VWAP reset at US cash-market open (09:30
   ET) or at UTC midnight? Current impl resets per calendar day in UTC.
   For intraday trading this is usually fine but edge cases matter for
   pre-market bars.
2. Should the trend filter use the 1h or the 4h EMA? Current: 1h EMA(20).
3. VWAP anchor variants — VWAP-from-yesterday-close, VWAP-from-prior-swing?
   Today's impl is strict session VWAP; variants are a follow-up.
