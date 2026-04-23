# ICT Trading Bot — Backlog

## Last Updated: 2026-04-22 (multi-strategy v2 shipped; open items captured)

Branch: `feature/profitability-research` — HEAD `5d90c6a`.

This pass marks shipped items as COMPLETED, updates in-progress items to
reflect actual state, and incorporates the new multi-strategy v2 roadmap
(`docs/multi_strategy_architecture_v2.md`, commit `c8f5dad`).

---

## CRITICAL — Architecture

### ARCH-001: Database is the Single Source of Truth
**Principle**: The PostgreSQL database is the ONLY source of truth for all system state. No process should rely on its in-memory cache for state decisions. Every component reads from the DB. Accuracy is more important than speed — PostgreSQL has its own caching layer.

**Current violations**:
1. `exit_manager.open_trades` (in-memory list) acts as parallel source of truth — trades can exist in memory but not DB, or vice versa
2. `open_trades.json` file is a stale backup of the in-memory list
3. Reconciliation has to sync two sources (IB + DB) but also a third (in-memory list)
4. When bot restarts, it loads from `open_trades.json`, not from DB
5. `add_trade()` writes to in-memory list first, then DB — if DB write fails, state diverges

**Required changes**:
- Exit manager reads open trades from DB every cycle (5s) — no in-memory list as source of truth
- `add_trade()` writes to DB FIRST. If DB write fails, trade is NOT tracked (fail-safe).
- Remove `open_trades.json` entirely — DB is the persistence layer
- On startup, rebuild state from DB, not from JSON file
- All trade state decisions (is this trade open? what's the qty?) query the DB
- Dashboard already reads from DB (correct pattern)

Status: **Implemented** — exit_manager reads from DB, no JSON, DB-first add_trade

### ARCH-002: Row-Level Locking for Trade State Transitions
**Principle**: When changing the state of any trade (open→closed, updating price, rolling), the process MUST use `SELECT ... FOR UPDATE` (row-level locking) to prevent race conditions between parallel components.

Status: **Implemented** — close_trade uses SELECT FOR UPDATE NOWAIT, update_trade_price uses GREATEST

### ARCH-005: Single Close Authority + Graceful Already-Closed Handling
**Principle**: Only ONE code path can close trades — `_atomic_close()` in exit_manager. No other thread, module, or function sends sell orders. All close requests funnel through this single point.

Status: **Implemented and enforced**

### ARCH-006: Single Open Authority — one function opens all trades
**Principle**: Same as ARCH-005 (single close), but for opening trades. Only ONE function creates trade records in the DB. All paths use it — `exit_manager.add_trade()`.

Status: **Implemented** — add_trade() checks DB for existing open trade on same ticker before INSERT

### ARCH-003: Refactor into Clean, Small, Reusable Components
**Principle**: Each module has a single responsibility, is independently testable, and communicates only through the DB. No module should be >200 lines. No duplicated logic.

Status: **Implemented (ib_client split)** — shipped in commit `6d7c32f` (merged from `feature/arch-003-ib-client-split`). `broker/ib_client.py` is now a thin facade over `ib_orders.py`, `ib_market_data.py`, `ib_positions.py`, `ib_contracts.py`. Further decomposition of `exit_manager` / `trade_entry_manager` is a fresh-eyes pass but the hotspots from this item are done.

### ARCH-004: Automated Regression Test Suite
**Principle**: Every bug fix and enhancement must have a corresponding test. No code change ships without passing the full regression suite.

Status: **Implemented** — 26 unit test files (~4.5k LOC) under `tests/unit/`; `test_results` + `test_runs` tables persist runs; dashboard Tests tab ("Run Unit" / "Run Concurrency" / "Run Integration") drives the suite. CLAUDE.md mandates running the suite before declaring any task done.

---

## Multi-Strategy Foundation — SHIPPED (2026-04-22)

### ENH-030: Multi-strategy v2 architecture ✅
Spec: `docs/multi_strategy_architecture_v2.md` (design doc approved 2026-04-22, P&L-aggregation note added for open trades).
Status: **All 6 phases shipped.** Bot running live with 4 active strategies (ICT + ORB + VWAP + delta-neutral).

Implementation commit map:
- **Phase 2a** — DB migration (`6e7ed0a`): slim `trades` envelope + `trade_legs` child table, `trades_pre_legs` frozen snapshot, views `v_trades_with_first_leg` + `v_trades_aggregate_pnl`, retired `ACTIVE_STRATEGY` singleton setting.
- **Phase 2b** — ORM + writer refactor (`3585385`): `Trade` slimmed, new `TradeLeg` model, `insert_trade` writes both tables in one tx.
- **Phase 2c-1** — strategy/ layer (`6cec13d`, `de4757e`): inline SQL in `strategy/reconciliation.py` retargeted to `trade_legs`.
- **Phase 2c-2** — dashboard (`2571c6c`, `2247842`, `515c6d5`): routes + analytics views.
- **Phase 2c-3** — broker orphan cleanup (`1fa664f`).
- **Phase 3** — Settings UI scoping (`0a1f701`, `f847140`) + Tickers UI scoping (`e22a9d8`, `1873059`).
- **Phase 4** — Scanner plugin dispatch (`8200dff`, `ac37e43`, `4eb5dc9`, `b7a4c39`): per-strategy per-ticker threads, plugin `class_path` loading, ICT fast-path preserved.
- **Phase 5** — Thread-owned close (`57ad72c`, `7e5a947`, `fe3900e`, `c522492`): `trades.ib_client_id` stamped at entry, `cancel_order_by_id(preferred_client_id=...)` prefers owner.
- **Phase 6a** — Multi-leg execution (`b812307`, `3be323a`, `4386409`, `230811b`, `35dae3b`): `LegSpec`, `place_multi_leg_order`, `insert_multi_leg_trade`, delta-neutral plugin skeleton.
- **Phase 6b** — Close-across-legs (`65dfc96`): `_execute_multi_leg_exit` iterates each leg with the correct closing order (LONG CALL → sell_call, SHORT CALL → buy_call, etc.).

Follow-up fixes shipped same day:
- Pool-aware `get_all_working_orders` (`23fd21e`) — stopped spurious bracket restoration.
- Strategy prefix on `client_trade_id` (`f9c70da`) — refs like `ict-SPY-260422-01`.
- Per-strategy lock in `get_open_trades_from_db` (`020305a`) — unblocks ORB/VWAP from ICT's ticker slots.
- `thread_status` CHECK constraint widened (`75d3639`).
- `v_trades_aggregate_pnl` now marks open trades via `current_price` (migration 009).

### ARCH-007: Stable clientId pool routing ✅ (Phase 5)
Spec: `docs/ib_db_correlation.md` §11.
Status: **Shipped.** `trades.ib_client_id` populated at entry; close flow prefers that pool slot; permId fan-out retained as fallback.

### ENH-031: Multi-leg trade model ✅ (Phase 6a + 6b)
Spec: `docs/multi_strategy_architecture_v2.md` §2, §6.
Status: **Shipped.** `trade_legs` table, `LegSpec` dataclass, `place_multi_leg_order`, `_execute_multi_leg_exit`.

### ENH-032: Settings + Tickers UI scoped per strategy ✅ (Phase 3)
Spec: `docs/multi_strategy_architecture_v2.md` §5.
Status: **Shipped.** Strategy dropdown on both Settings and Tickers tabs, inherited-vs-override pills, reset-to-global.

### ENH-033: Auto-restart on strategy activation — NOT STARTED
Status: **Deferred polish item.** Today: enabling/disabling a strategy in the UI requires a full bot restart for the scanner spawn loop to pick up the change. Low priority — restart is ~15s.

---

## Multi-Strategy — Deferred / Open Items

Tracked here so they don't fall through the cracks. Work when appropriate.

### ENH-034: Live FOP trading path ✅ SHIPPED (2026-04-22)
Spec: `docs/fop_live_trading_design.md`.
Status: **Shipped** in commits `c49585f` (selector + 26 tests) + `a2fa962` (broker methods + option_selector routing).
Implementation:
- `strategy/fop_selector.py` — liquidity-aware contract picker (quarterly > monthly > weekly preference, hard-rejects on OI/volume/spread).
- `broker/ib_orders.py::place_bracket_order_fop` — places bracket on FuturesOption contract.
- `broker/ib_orders.py::fop_chain` / `fop_quote` — IB probes the selector injects.
- `strategy/option_selector.py` — FOP branch at top of `select_and_enter` / `select_and_enter_put`, sec_type lookup from `tickers` table.
- Config knobs: `FOP_MAX_DTE`, `FOP_MIN_OPEN_INTEREST`, `FOP_MIN_VOLUME`, `FOP_MAX_SPREAD_PCT`, `FOP_EXPIRY_PREF`.
Activation: takes effect on next bot restart + requires adding a ticker row with `sec_type='FOP'` on an enabled strategy (via Tickers tab). Until then behavior unchanged.
Remaining FOP follow-ups (not blocking live use):
- FOP roll logic (currently picks OCC-style symbol; FOP roll needs FuturesOption-aware roll path).
- `USE_BRACKET_ORDERS=False` path unsupported for FOP (aborts with warning — bracket is production default).

### ENH-035: Production IV detection for DeltaNeutralStrategy.detect()
Spec: `docs/delta_neutral_strategy.md` (existing) + v2 doc §8 open questions.
Today: `strategy/delta_neutral_strategy.py` uses a rolling-stddev proxy for IV elevation — rudimentary and doesn't reliably trigger. Production version should consume IB greeks or an external IV feed (e.g. underlying's IV30, VIX structure, or the contract's `modelGreeks.impliedVol` from `reqMktData`).

### ENH-036: Stock-leg close support in multi-leg exit ✅ SHIPPED (2026-04-22)
Status: **Shipped** in commit `82848ae`.
`broker/ib_orders.py::sell_stock` / `buy_stock` helpers added. `_close_action_for_leg` now maps STK LONG → sell_stock, STK SHORT → buy_stock. Delta-neutral trades with a stock hedge now close the hedge alongside the options.

### ENH-037: Cross-strategy exposure caps
Spec: `docs/multi_strategy_architecture_v2.md` §8 open question 7.
Today: no global limit on concurrent strategies' open positions. Possible to have ICT + ORB + VWAP + delta-neutral all long SPY at once → concentrated risk. Proposed: configurable net-delta or gross-notional cap per underlying, checked in `TradeEntryManager.can_enter()`.

### ENH-038: Delta-neutral backtest support ✅ SHIPPED
Status:
- **Part 1** — migration 011 (`backtest_trade_legs` + `backtest_trades.n_legs`), `record_multi_leg_trade` writer, engine router on `trade["_legs"]`. Shipped in `03fe86e`.
- **Part 2** — `backtest_engine/multi_leg_sim.py` (BS + Black-76 per-leg pricing, entry/exit state, synth_price collapse to scalar for evaluate_exit). `_simulate_ticker` now takes the multi-leg branch when `strategy.place_legs()` returns legs. Shipped in `5bbe120`.
- **Tests** — 4 in `test_backtest_multi_leg.py` + 15 in `test_backtest_multi_leg_sim.py` (helpers + DeltaNeutral end-to-end with stubbed fetch/levels/exit).

`python run_backtest_engine.py --strategy delta_neutral ...` now produces real iron-condor backtest results.

### ENH-039: Per-strategy commission accounting
Spec: v2 doc §8.
Today: commissions flow through `trade_closes` but aren't attributed back to a strategy in reporting. Nice-to-have for strategy-ranking analytics.

### ENH-040: Trades-tab strategy provenance polish ✅ PARTIAL (strategy filter shipped)
Status: **Strategy filter dropdown shipped** in commit `82848ae`. Populated from distinct strategy_name values in the loaded trade set. Works alongside existing Status / Ticker / Period filters.
Remaining: per-strategy P&L summary cards at top of Trades tab (total pnl, win-rate, trade count per strategy). Low priority — analytics page already has cross-strategy views.

### ENH-041: Retire main branch / make profitability-research the default ✅ PARTIAL
Status: **main fast-forwarded to trunk** 2026-04-22. `origin/main` and `origin/feature/profitability-research` now point at the same commit. Remaining step: user toggles GitHub repo default branch to `main` in Settings, then `feature/profitability-research` can be deleted. One-click on GitHub.

### ENH-043: Threads tab — strategy filter dropdown
Dashboard Threads view lists every scanner/entry-manager/exit-manager row for every strategy in one big table. With 4 strategies × ~15 tickers that's 60+ rows and it's hard to zero-in on problems for a specific strategy. Add a strategy filter (dropdown populated from distinct strategy_name, same pattern as ENH-040 Trades tab). Multi-select is a nice-to-have; single-select is enough for v1.

### ENH-044: Orphan bracket-order cleanup for closed trades
Observed today (2026-04-23): IB shows multiple AAPL/MSFT bracket orders in "Transmit" status with no corresponding open DB trade. Some carry a strategy `orderRef`, others are ref-less (legacy). Causes: (a) previous `_atomic_close` cancelled the position but the bracket children were left working; (b) reconciliation's `bracket_restored` path creates fresh brackets but doesn't clean up the prior bracket orphans; (c) orders placed pre-ref-tagging era have no way to attribute. Work:
1. On trade close, cancel every working order whose `orderRef` matches the closed trade's `client_trade_id`, not just the two stored bracket ids.
2. Periodic sweep: working SELL/STP orders with no matching open trade AND no matching closed-within-60s trade → cancel them.
3. Ref-less orphans surfaced in the dashboard as a cleanup queue the user can one-click cancel.

### ENH-045: Ref-less order backfill
Related to ENH-044. Orders placed before `orderRef` stamping shipped have no provenance in IB. Either (a) stamp-retroactively via `modifyOrder` when reconciliation discovers them (only works for still-working orders), or (b) document them as legacy and rely on the cleanup sweep in ENH-044.

### ENH-046: Submit multi-leg entries as BAG/combo orders (single order, single bracket)
Today `place_multi_leg_order` submits N independent `MarketOrder`s — 4 separate orders in TWS for an iron condor. Problems observed 2026-04-23:
- Partial-fill risk on entry (solved at entry-level via the OCA fix, but still fragile)
- N separate SL brackets = N separate random-fire risks
- Per-leg SL on a short wing of an iron condor is semantically wrong — max loss for an iron condor is `wing_width × contracts × multiplier − net_credit`, not a per-leg stop
- TWS shows 4 unrelated orders instead of one strategy

Proposed fix: use IB's **BAG** (combo) contract with `comboLegs`, submit as one net-credit/net-debit order:
```python
bag = Contract(secType="BAG", symbol=underlying, currency="USD", exchange="SMART")
bag.comboLegs = [
    ComboLeg(conId=short_call.conId, ratio=1, action="SELL", exchange="SMART"),
    ComboLeg(conId=long_call.conId,  ratio=1, action="BUY",  exchange="SMART"),
    ComboLeg(conId=short_put.conId,  ratio=1, action="SELL", exchange="SMART"),
    ComboLeg(conId=long_put.conId,   ratio=1, action="BUY",  exchange="SMART"),
]
order = LimitOrder("BUY", qty, limit_price=net_debit_or_credit)
```
Benefits:
- One order in TWS UI (expands to 4 legs)
- All-or-nothing fill (no partial condor)
- One bracket (TP on net price, SL on net price) — proper iron-condor risk accounting
- Cleaner `trades` envelope ↔ IB order mapping (1:1)

Work:
1. Build bag contract in `broker/ib_orders.py` — new `place_combo_order(legs, net_price, tp_net, sl_net)` method.
2. Update `TradeEntryManager._enter_multi_leg` to route here when the strategy declares it's a defined-risk spread.
3. Update `trade_legs` persistence — store the combo conId, individual leg conIds, but only one bracket per envelope.
4. Update `strategy/exit_executor.py` multi-leg close path to also use the BAG (buy-to-close at net price).
5. Backtest: `backtest_engine/multi_leg_sim.py` already evaluates on net premium — matches the combo semantics, no change needed.

### ENH-051: IB LimitOrder entry for combo orders (slippage reduction) ✅ SHIPPED (2026-04-23 eve)
`broker/ib_orders.py::_compute_combo_net_limit` now computes a net
mid-price from each leg's quote and submits the BAG order with a
LimitOrder instead of MarketOrder. Gated by `DN_COMBO_AUTO_LIMIT`
(default true) with a configurable slippage buffer
(`DN_COMBO_LIMIT_SLIP_BPS`, default 200 bps = 2%). Falls back to MKT
when any leg quote is unavailable. Unit tests in
`tests/unit/test_combo_net_limit.py` (5 cases).

### ENH-049: Delta-neutral strategy — dynamic stock-hedged delta (staged)
User request 2026-04-23: use stock as the hedge and rebalance to net-zero
delta on a short interval (proposed 30 seconds) by buying or shorting
the underlying. Based on the LinkedIn article referenced in
`docs/delta_neutral_strategy.md` — "Beyond Directional Bets: Building
Systematic Delta" (Bejar-Garcia).

Staged plan:

**Stage 1 — passive monitoring (foundation).** Compute live portfolio
delta from option positions via BS greeks on the current underlying
price + known sigma. Surface per-trade and per-strategy delta on the
Trades tab + a new Delta card. No trading actions yet — just prove the
math + instrumentation are trustworthy. Use `backtest_engine/option_pricer.bs_greeks`
which already exists.

**Stage 2 — periodic rebalance (semi-automatic, paper-only).** Every
30s, compute combined delta of each open DN trade (4 legs) and
subtract the current stock hedge (if any). If abs(net_delta) > threshold
(config `DN_DELTA_BAND_SHARES`, default 10), queue a STK order to
flatten: BUY if delta < 0, SELL/short if delta > 0. Target `DN_REBALANCE_INTERVAL_SEC`
(default 30). Record every rebalance in a new `delta_hedges` table
(trade_id, shares_delta, timestamp, hedge_order_id) so the user can
audit the chain. Gate on a new setting `DN_DELTA_HEDGE_ENABLED`
(default false) so it's opt-in.

**Stage 3 — full dynamic hedging.** Replace the 30s timer with event-driven
recomputation on underlying moves > some basis-points threshold.
Extend to vega + gamma bands per the article. Close the hedge when
the option position closes.

Implementation notes:
- Add a `hedge_shares` column to `trades` envelope (signed: +long /
  -short).
- The hedge is a STK LegSpec with leg_role='delta_hedge'. The existing
  multi-leg infrastructure (place_combo_order accepts STK legs) covers
  it.
- BAG/combo orders already support STK+OPT mixed legs (see
  `_build_leg_contract` — the STK branch is wired up).
- Backtest engine: new module `backtest_engine/delta_hedge.py`
  simulates the same rebalance logic on historical bars for
  apples-to-apples comparisons.

Doc to produce: `docs/delta_neutral_dynamic_hedging.md`.

### ENH-048: MNQ / MES futures scanners not producing activity
Observed 2026-04-23: MNQ and MES threads show as alive in the Threads tab but never emit signals or fills. Possible causes: (a) ticker sec_type=FOP but the FOP data provider isn't wired into the scan path for futures-options; (b) ICT signal detection on futures bars never passes; (c) contract qualification is silently failing for the micro-futures front month. Needs investigation — medium priority, not blocking any current work.
Debug path:
1. Grep `bot.log` for `[MNQ]` / `[MES]` — confirm scanner heartbeat is firing.
2. Confirm `data_provider_ib.fetch_multi_timeframe_ib` is reached for FOP and returns bars.
3. Confirm `scanner.py` dispatches to the right strategy `.detect()` for futures tickers.
4. Check `tickers` table — verify FOP sec_type + correct `contract_month` / `exchange`.

### ENH-047: Trades page — per-leg drill-down for multi-leg trades
`dashboard/frontend/src/components/TradeTable.tsx` renders every trade as one row. Multi-leg trades (n_legs > 1) should render with an expand caret that reveals each leg row (symbol, direction, strike, right, contracts, entry/exit fill, per-leg P&L). Ask the API to surface `/api/trades/{id}/legs` (new endpoint reading from `trade_legs`) so the table can lazy-load the legs on expand.

---

## HIGH — Important for Reliable Operation

### ENH-001: IB Streaming Market Data
Replace snapshot polling with streaming subscriptions for sub-second price updates.
Spec: `docs/production_improvements.md`
Status: Not started

### ENH-024: Strategy Plugin Framework — Multi-Scanner Architecture
Spec: `docs/strategy_plugin_framework.md` + `docs/multi_strategy_architecture_v2.md` Phase 4.
Status: **In Progress.** Partially shipped:
- DB `strategies` table + seed rows, `BaseStrategy` abstract class, `LegSpec`/`Signal` dataclasses — **shipped** (commit `98cf1b4`).
- Backtest engine routes through strategy plugins (`backtest_engine/` uses strategy_id) — **shipped**.
- ORB + VWAP plugins implemented end-to-end against backtest — **shipped** (commits `95133b1`, `7a60228`).
- **LIVE SCANNER DOES NOT YET DISPATCH THROUGH PLUGINS** — still hardcoded to `SignalEngine` at `strategy/scanner.py:96`.

Completion plan: `docs/multi_strategy_architecture_v2.md` Phase 4 replaces the hardcoded `SignalEngine` import with a `StrategyRegistry.get(strategy_id)` lookup in scanner setup.

### ENH-023: Futures Options Support (MNQ, NQ, ES, MES, GC, CL)
Spec: `docs/futures_options_support.md`, `docs/futures_options_implementation.md`, `docs/fop_live_trading_design.md`.
Status: **In Progress.** Partially shipped:
- Backtest FOP works (commit `0dfddde` — sweep FOP support, MES/MNQ-friendly defaults) — **shipped**.
- `FOP_SPECS` registered, contract handling for CME/FOP fixed (commit `ff618d2`) — **shipped**.
- Backtest cache pyarrow + pickle fallback (commit `5d90c6a`) — **shipped**.
- Probe tools under `tools/` — **shipped**.
- **Live trading not yet wired** — `fop_live_trading_design.md` is the plan.

### ENH-050: Combo per-leg fill-price fallback ✅ SHIPPED (2026-04-23 eve)
Four-stage recovery in ``broker/ib_orders.py::_ib_place_combo``:
exec fills → ib.executions() stream → post-fill quote → proportional
split. Every leg tagged with ``price_source`` (exec / quote /
proportional) persisted to ``trade_legs.price_source`` and surfaced
in the drill-down UI as an ``est`` badge on non-exec prices.
Companion one-shot ``scripts/backfill_combo_fill_prices.py`` repairs
existing ``entry_price=0`` rows. Design doc:
``docs/enh_050_combo_leg_fill_price.md``. Tests:
``tests/unit/test_combo_fill_price_recovery.py`` (5 cases).

### ENH-007: Option Rolling Logic — LOW PRIORITY (user 2026-04-23)
Spec: `docs/close_flow_fixes_2026_04_21.md` (same-strike guard + roll-loop fix).
Status: **Shipped.** Close step uses `execute_exit()` (BUG-035). Same-strike guard prevents roll-loop churn (commit `a1f23df`). Stale-cache false-positive in POST-SELL bracket verify fixed (commit `949c7da`). Roll trigger threshold config in settings table (BUG-043).

### ENH-008: TP to Trailing Stop
At 100% TP, move SL to TP level instead of hard exit.
Status: **Shipped** (`strategy/exit_conditions.py::check_tp_to_trail`) — awaiting live validation.

---

## LOW — Nice to Have

### ENH-010: Compact Trade Table
Additional UI polish for the trades tab.
Status: Open.

### ENH-018: Authentication — Login Screen + 2FA
Secure dashboard with username/password login and TOTP two-factor authentication.
Role-based access: admin, trader, viewer. JWT tokens with httpOnly cookies.
Spec: `docs/authentication.md`
Status: Design documented, not started.

### ENH-020: Cloud Deployment — Installable Image
Docker-based deployment to AWS/GCP/Azure. GitHub Actions CI/CD pipeline.
Install script, IB Gateway in Docker, SSL, environment configuration.
Spec: `docs/cloud_deployment.md`
Status: Design documented, not started.

### ENH-025: iOS Native Mobile Application
Native SwiftUI app connecting to existing FastAPI backend. Zero business logic
duplication — server does all computation, app is a thin client for monitoring,
control, and alerts. Push notifications for trade events.
Spec: `docs/ios_mobile_app.md`
Status: Design documented, not started.

### ENH-026: Delta-Neutral Strategy (Iron Condor / Iron Butterfly)
Theta decay strategy for range-bound markets. 0DTE iron condors on SPY/QQQ.
Requires multi-leg combo order support. Complements directional strategies.
Spec: `docs/delta_neutral_strategy.md`
Status: **Blocked on ENH-031 (multi-leg support).** Research complete, design documented.

### ENH-022: Code Profiling & Performance Monitoring
cProfile, line_profiler, memory_profiler integration. Performance monitoring
endpoint. Optimization areas identified: DB cache, IB streaming, exit evaluation.
Spec: `docs/testing_framework.md` (profiling section)
Status: Design documented, not started.

---

## COMPLETED

### Critical Audits (all verified with live market)
- **AUDIT-001**: Comprehensive error handling audit — 51 bare except/pass reduced to 1 intentional
- **AUDIT-002**: Trade lifecycle integrity — timeout recovery, orphan detection, IB fill verification
- **AUDIT-003**: Reconciliation reliability — conId matching, safety checks, direct IB calls on startup
- **AUDIT-004**: Syntax and import verification — all Python files compile, modules import

### Recently Completed (2026-04 refresh)

| Item | Description | Status / evidence |
|------|-------------|-------------------|
| **ARCH-003** | `ib_client.py` split into orders/market_data/positions/contracts facade | Shipped, commit `6d7c32f` (merged from `feature/arch-003-ib-client-split`) |
| **ARCH-004** | Automated test suite + Tests tab UI + DB persistence | Shipped — 26 unit test files, `test_runs`/`test_results` tables |
| **ENH-011** | Trade Notes — inline editor in Trades tab | Shipped (`trades.notes` column + UI) |
| **ENH-012** | Export to Excel / CSV | Shipped — openpyxl route in API |
| **ENH-013** | Mobile Responsive Design | Shipped — Tailwind responsive classes |
| **ENH-019** | Backtest Framework | Shipped — `backtest_engine/`, BacktestTab UI, sweep launcher, analytics (commits `6d554fb`, `c20ce7b`, `d042044`, `74bbfe4`, `3819f46`, `18020da`) |
| **ENH-021** | Automated Testing Framework | Shipped — see ARCH-004 |
| **BUG-042** | Option rolling leaves old trade open in DB | Fixed — SQL syntax error in finalize_close; reconciliation no longer adopts negative positions |
| **BUG-043** | Rolling config should be in settings table | Fixed — ROLL_ENABLED, ROLL_THRESHOLD, TP_TO_TRAIL, STOP_LOSS, PROFIT_TARGET, USE_BRACKET_ORDERS, RECONCILIATION_INTERVAL_MIN all in settings, exposed in Settings tab |
| **BUG-044** | Exit reason values inconsistent | Fixed — standardized to TP, SL, TRAIL_STOP, ROLL, TIME_EXIT, EOD_EXIT, EXPIRED, UI_CLOSE, RECONCILE. Variable detail in `exit_enrichment` JSONB |
| **BUG-045** | Orphaned "transmit" state orders stuck in IB | Fixed — `cleanup_orphaned_orders()` runs on startup + orphan bracket detector (PASS 3 of reconciliation, commit `dee91e2`) cancels stragglers |
| **BUG-046** | Scanner + exit manager race on same ticker | Fixed — ARCH-006 DB-level duplicate guard |
| **BUG-047** | Double-close from bracket order + exit_manager racing | Fixed — strict bracket cancel verification (commit `363380f`), sell-first close mode (commit `fcd0051`) |

### Recent Infrastructure / Reliability Work (2026-04-15 → 2026-04-21)

- **IB ↔ DB correlation via `client_trade_id`** (commits `2771e0a`, `f9c70da`, `6d01036`) — human-readable `TICKER-YYMMDD-NN` with strategy-short-name prefix; migrations `005_client_trade_id.sql` + `006_client_trade_id_widen.sql`. Spec: `docs/ib_db_correlation.md`.
- **System architecture doc** (commit `9b4ca98`) — `docs/system_architecture.md`.
- **Close-flow fixes 2026-04-21** (commits `a1f23df`, `949c7da`) — roll-loop churn (same-strike) + stale-cache POST-SELL bracket verify. Post-mortem: `docs/close_flow_fixes_2026_04_21.md`.
- **Sell-first close mode** (commit `fcd0051`) — works around IB cross-client cancel asymmetry.
- **Market-hours guards** (commit `fad09c4`) — EOD sweep + hard cutoff on exits + entries. Spec: `docs/market_hours_guards.md`, `docs/market_hours_validation.md`.
- **Bracket rollback** (commit `ce55dce`) — compensating transaction on unprotected positions. Spec: `docs/bracket_rollback_semantics.md`, `docs/bracket_cancel_strict_verification.md`.
- **Orphan bracket detector** (commits `0071dd4`, `dee91e2`, `456b3d5`, `bf522c2`, `1a15d50`) — PASS 3 of reconciliation, per-scan inventory log, cross-client cancel fan-out, IB error 201 fast-path. Spec: `docs/orphan_bracket_detector.md`, `docs/thread_owned_close.md`.
- **MSFT short regression fix** (commit `363380f`) — strict cancel verification + negative-position recovery.
- **Adopted trades with padded OCC symbols** (commit `088e494`) — silently unmonitored; fixed.
- **Roll/close flow bugs** (commit `3eda3b8`) — stray IWM short + TSLA orphan. Docs: `docs/roll_close_bug_fixes.md`.
- **Trade audit trail** (commit `b2ee7c5`) — full who-did-what-when per db_id. Spec: `docs/logging_and_audit.md`.
- **UI: Trades page** — ID column with rich troubleshooting tooltip + click-through details modal (commits `f02c388`, `894dddb`). Threads page surfaces entry-manager activity (commit `0ddd345`).
- **UI: Close via bot queue** (commit `6ccf238`) — UI Close / Close All routed through safe pool-aware path.
- **Live-trading log visibility** (commit `20287ac`) — timestamps, signal→order, bracket, reconcile.
- **Backtest analytics** — cross-run feature importance (commit `c20ce7b`), exit-indicator correlation (commit `d042044`), sweep launch UI (commit `74bbfe4`), server-side per-column filters (commit `3819f46`), 1m-resolution validation of top runs (commit `18020da`). Spec: `docs/backtest_analytics_design.md`.

### Bug Fixes (BUG-001 through BUG-040)

| Bug | Description | Root Cause | Fix | Status |
|-----|-------------|------------|-----|--------|
| BUG-001–021 | Various early bugs | Multiple | Multiple | Fixed |
| BUG-022 | Double-sell (bracket + exit manager) | Exit manager and IB bracket both closing the same position | Exit flow: cancel brackets → verify position → sell | Fixed, verified live |
| BUG-027 | Reconciliation false closes | `get_ib_positions_raw` returned `[]` on timeout instead of raising | Raises on failure, safety check aborts on 0 positions with DB trades | Fixed, verified live |
| BUG-028 | Scanners auto-start on restart | `scans_active=true` left in DB from previous session | Bot resets `scans_active=false` on every startup | Fixed, verified live |
| BUG-029 | Phantom DB trades (Meta/Microsoft) | `option_selector.py` returned trade dict even when IB order status was Cancelled/Inactive | Gate on order status: FAILED_STATUSES return `None` | Fixed |
| BUG-030 | Missing DB records (Google) | Trade filled on IB but `insert_trade()` failed silently during reconciliation adoption | Reconciliation verifies adopted orphans have `db_id`, retries `insert_trade()` if missing | Fixed |
| BUG-031 | IB error reasons silently lost | Zero `errorEvent` handlers registered | Registered `_on_ib_error` callback | Fixed |
| BUG-032 | Orphaned IB fills not adopted into DB | 17 tickers simultaneous — IB queue backed up >60s | `_check_orphaned_fills()` calls `add_trade()` immediately. Timeout 30s→60s, recovery 5s→10s. | Fixed |
| BUG-033 | Duplicate open trades in DB (GOOGL) | Reconciliation adopted same IB position twice | Reconciliation queries DB for open trades by `ib_con_id` before adopting | Fixed |
| BUG-034 | Negative positions / double-close (regression of BUG-022) | `execute_roll()` bypassed `execute_exit()` | Rewrote `execute_roll()` to use `execute_exit()` | Fixed |
| BUG-035 | Option rolling conflicts with bracket orders | `execute_roll()` had its own close logic | Rewrote `execute_roll()` to call `execute_exit()` first | Fixed |
| BUG-036 | MSFT option contract qualification fails (code 200) | Chain strikes don't exist for 0DTE | Try ATM + 6 nearest candidate strikes | Fixed |
| BUG-037 | Reconciliation not syncing DB with IB | Pass 1 only checked memory; symbol matching fragile | Two-pass rewrite using conId matching | Fixed |
| BUG-038 | QQQ 634 Call closed 3x after rolling | Double-call between execute_exit + execute_roll | Split logic: rolling calls execute_roll only | Fixed |
| BUG-039 | No position check before sell orders | Raw SELL without checking position | `get_ib_position_qty()` before every sell; `max_qty` param | Fixed |
| BUG-040 | Option symbol shows raw OCC with spaces | IB returns `"GOOGL 260415P00332500"` | Strip spaces before regex; added Call/Put label | Fixed |

### Regression fixes (REG-001 through REG-006)

| Reg | Description | Root Cause | Fix | Status |
|-----|-------------|------------|-----|--------|
| REG-001 | IB event loop blocked by DB writes | `_on_ib_error` did DB writes on IB thread | Log-only on IB thread | Fixed |
| REG-002 | 6x IB calls per contract lookup | SPY multi-exchange loop in hot path | Try SMART first, fall back individually | Fixed |
| REG-003 | Timeout recovery completely broken | `ThreadPoolExecutor` closed too early | Keep executor alive during 35s window | Fixed |
| REG-004 | Exit manager heartbeat too frequent | DB write every 5s | Every 30s (6 cycles) | Fixed |
| REG-005 | Stale bot state after crash | DB showed `status='running'` after crash | Manual cleanup; sidecar required | Fixed (manual) |
| REG-006 | IB pool connections all timing out | `ib_async` event loop thread affinity | `connect()` + event loop on same dedicated thread | Fixed |

### Lessons Learned from Regressions

> **REG-001 through REG-005 were all introduced in the same session (2026-04-14/15).** Root causes:
>
> 1. **Calling blocking code on the IB event loop thread** — The IB error callback MUST be non-blocking.
> 2. **Refactoring scope too large without incremental testing.**
> 3. **Loop amplification** — Adding a 6-exchange loop to a hot-path function.
> 4. **No integration test after multi-file changes.**

### Enhancements (ENH-002 through ENH-015)
- **ENH-002**: Heartbeat monitoring — exit manager (30s), bot main (30s), scanner (60s) heartbeats to thread_status
- **ENH-003**: Error pipeline — connected `log_error()` to populate errors table
- **ENH-004**: System status — stale/dead detection in ThreadsTab, system log viewer panel
- **ENH-005**: Analytics v2 — 3 new charts, 2 new SQL views, drilldown support
- **ENH-006**: Separate signal engine from trade management
- **ENH-009**: SPY option chain fix — prefer 0DTE chain, multi-exchange qualification
- **ENH-014**: Button loading states — Start/Stop Bot and Scans show pulse while in progress
- **ENH-015**: Trade count summary badges — total/open/closed/errored counts

### Infrastructure
- Database schema (14 tables + analytics views + system_log + trade_closes + trade_commands + test_runs/results)
- Bot DB integration (dual-write)
- FastAPI backend
- React frontend (Trades, Analytics, Threads, Tickers, Settings, Backtest, Tests tabs)
- Docker Compose deployment (PostgreSQL, API, Frontend, pgAdmin)
- Bot manager sidecar (port 9000)
- IB trade ID integration (permId, conId, client_trade_id)
- DB-based state management (replaced file-based)
- Batch IB pricing
- Centralized error handler (handle_error + safe_call)
- IB errorEvent handler (non-blocking, log-only)
- IB connection pool (4 slots: exit-mgr + scanner A/B/C) with thread-affine event loops
