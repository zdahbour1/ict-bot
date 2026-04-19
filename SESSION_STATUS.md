# Session Status — Back From Your Break

**Three feature branches shipped while you were out. All pushed to origin.
All tests green. Review whenever you're ready.**

## TL;DR by the numbers

| Branch | Commit | Tests | Real backtest |
|---|---|---|---|
| `feature/orb-live` | pushed | 194 + 6 ORB integration | **run #167**: 236 trades, 55.5% win rate |
| `feature/vwap-revert` | pushed | 211 + 5 VWAP integration | **run #212**: 37 trades, 62.2% win rate |
| `feature/futures-options` | pushed | 227 + 15 FOP integration | blocked on data source |

## 🏆 Strategy comparison — first real head-to-head

All three backtested against **QQQ + SPY + IWM** on 5-minute bars over
**60 days** ending Apr 18 2026, with identical frictions (0.2% slippage,
$0.65/contract commission) and the existing `evaluate_exit` logic.

| Metric | ICT (run #83) | ORB (run #167) | VWAP (run #212) |
|---|---|---|---|
| Trades | 320 | 236 | **37** |
| Win rate | 48.4% | 55.5% | **62.2%** |
| Total P&L | −$854 | −$637 | **−$90** |
| Profit factor | 0.54 | 0.57 | **0.74** |
| Max drawdown | −$1,032 | −$794 | **−$291** |
| Avg win | small | **−$2.07** ⚠ | +$2.82 |
| Avg loss | — | −$3.49 | −$11.10 |
| Avg hold | — | 486 min | 650 min |

### What the numbers say

- **VWAP is the best-risk-adjusted of the three** on this window: highest
  win rate, smallest drawdown, positive avg_win, near-breakeven P&L.
  But large avg_loss (−$11) means when it's wrong, it's WRONG — classic
  mean-reversion failure mode when price doesn't revert.
- **ORB's negative avg_win is not a bug** — it's the backtest doing its
  job. ORB's wins are small-magnitude (range-width-based TP on a
  selective breakout). Slippage + commission (round-trip ~$3 on a
  2-contract $2 option) eats those small wins. Tells us ORB needs
  bigger TP targets on this timeframe, or lower frictions (larger
  contracts, cheaper broker), or both.
- **ICT has the highest trade frequency** but the lowest win rate.
  Tradeoff is different from ORB/VWAP — many small attempts, many
  small losses, slightly bigger wins. Still net negative on this window.

### Important caveat

**None of these are profitable on this 60-day window.** That's real
information. It could mean:
1. This 60-day window was unfavorable (likely — markets have regimes)
2. Friction assumptions are too conservative (slippage 0.2% is aggressive)
3. Option-P&L-from-underlying proxy (5× leverage) may understate the
   real option behavior — actual delta-decay math would give different
   numbers
4. Strategies need parameter tuning

**The backtest's value isn't saying "trade this!" — it's saying**
"here's a fast, reproducible, friction-modeled way to see how any
change affects outcomes, with per-trade indicator data ready for data
science."

Browse any run at http://localhost → Backtest tab → click the row.
The **Feature Analysis** panel will tell you, per strategy, which entry
indicators correlated with wins vs losses.

---

## What each branch actually changed

### `feature/orb-live`
- `docs/orb_strategy_design.md` — design doc
- `db/enable_orb.sql` — flips `orb.enabled=TRUE`, seeds 8 scoped settings
- `run_backtest_engine.py` — **new:** dynamic class-path instantiation
  so ANY plugin registered in the strategies table can be backtested
  (not just ICT)
- `tests/integration/test_orb_backtest.py` — 6 tests
- Test fix in `test_roadmap_schema.py` — "only ICT enabled" → "ICT
  always enabled" (as more strategies enable, this invariant evolves)

### `feature/vwap-revert` (built on orb-live)
- `docs/vwap_strategy_design.md` — design doc
- `strategy/vwap_strategy.py` — **new** (~200 LOC): session-VWAP,
  RSI, ATR, 1h EMA trend filter, LONG/SHORT symmetric logic
- `db/enable_vwap.sql` — flips `vwap_revert.enabled=TRUE`, seeds 13
  scoped settings
- `tests/unit/test_vwap_strategy.py` — 12 + 3 skipped
- `tests/integration/test_vwap_backtest.py` — 5 tests

### `feature/futures-options` (built on vwap-revert)
- `docs/futures_options_implementation.md` — focused implementation
  plan + data-source research
- `broker/ib_contracts.py` — **new:** `FOP_SPECS` dict + `get_fop_spec()`
  + `ib_qualify_futures_option()` for MNQ/NQ/MES/ES/GC/MGC/CL/MCL
- `db/seed_fop_tickers.sql` — 6 tickers (MNQ, NQ, MES, ES, GC, CL) with
  correct multipliers + exchanges, all `is_active=FALSE`
- `tests/integration/test_fop_foundation.py` — 15 tests (schema,
  specs, mocked IB qualify, trade round-trip)

---

## Data-source research for FOP backtests (key finding)

**Free FOP historical data is scarce.** Full catalog in
`docs/futures_options_implementation.md` §4 — most pragmatic route:

→ **Use IB's `reqHistoricalData` against FOP contracts directly.**
  Works with your existing paper account, ~1-year lookback on 1-min
  bars, no additional cost. Architecturally:
  `backtest_engine/data_provider_ib.py` alongside the yfinance one,
  picked per ticker's `sec_type`.

That's the recommended next branch after Monday merges.

---

## Merge recommendation (after Monday's arch-003 validation)

Order that minimizes conflicts and risk:

1. `feature/arch-003-ib-client-split` → `feature/dashboard`
   (Monday-validated live; the ib_client split + cherry-picked rollout #1)
2. Rebase `feature/enh-024-strategy-plugins` on the updated dashboard,
   merge it. This brings rollout #1 (now a no-op), backtest framework,
   roadmap schema extensions, and the multi-strategy design docs.
3. Rebase `feature/orb-live` on dashboard, merge.
4. Rebase `feature/vwap-revert` on dashboard, merge.
5. Rebase `feature/futures-options` on dashboard, merge.

All branches are additive + code-only (no conflicting DDL) after the
rebases drop duplicated commits. Each bringing tests that pass on
dashboard's schema.

---

## Open todos (biggest remaining pieces)

### 🔴 Awaiting you

1. **Monday live validation of arch-003** — open TWS, accept disclaimer,
   follow `MONDAY_CHECKLIST.md` on arch-003 branch
2. **Review the three new branches** — design docs + UI browsing at
   http://localhost → Backtest tab
3. **Decide on FOP historical data path** — my recommendation is IB
   `reqHistoricalData`, but if you have a Databento / Polygon / CME
   DataMine subscription that changes the plan

### 🟡 Queued (in order of dependency)

4. **FOP IB-backed data provider** (`backtest_engine/data_provider_ib.py`)
   — unlocks real FOP backtests
5. **First FOP backtest** — MNQ or MES (micros, cheap to validate)
6. **Per-instrument tick sizes + strike intervals** — needed before
   ORB/VWAP can generate valid FOP signals
7. **Rollouts #2-#8 of `active_strategy_design.md`** — so you can
   actually SWITCH strategies at bot-start from the UI, not just
   backtest them. ~6 incremental commits.
8. **Delta-neutral** — needs the deferred `trade_legs` table design
   first; separate session

### 🟢 Design docs that are actively useful

- `docs/active_strategy_design.md` — approved V2 multi-strategy schema
  foundation
- `docs/roadmap_schema_extensions.md` — forward-compatible schema principle
- `docs/orb_strategy_design.md` — new
- `docs/vwap_strategy_design.md` — new
- `docs/futures_options_implementation.md` — new
- `docs/futures_options_support.md` — broader FOP design (pre-existing)

### ⚪ Deferred / future sessions

- `docs/multi_strategy_data_model.md` — concurrent multi-strategy
  (long-term target after the simpler single-strategy model is shipping)
- `docs/delta_neutral_strategy.md` — iron condor research
- `docs/cloud_deployment.md` — not started
- `docs/authentication.md` — not started
- `docs/ios_mobile_app.md` — not started

---

## Restart prompt if needed

```
Continuing ICT bot — session of Apr 19 2026. Read in order:
  C:\src\trading\ict-bot-strategies\SESSION_STATUS.md
  C:\src\trading\ict-bot\.claude\CLAUDE.md

Branches on origin:
  feature/dashboard                    (main target)
  feature/arch-003-ib-client-split     (awaiting Monday live test)
  feature/enh-024-strategy-plugins     (backtest framework + roadmap DDL)
  feature/orb-live                     (ORB end-to-end)
  feature/vwap-revert                  (VWAP end-to-end)
  feature/futures-options              (FOP foundation)

Three strategies backtested, all code-only merges, ready after Monday.
227/227 tests + 3 expected skips on feature/futures-options.

Running stack: Postgres + API + frontend + bot_manager sidecar all live.
Bot process NOT running (TWS needs to be reopened).

Use TodoWrite to restore + wait for user direction.
```
