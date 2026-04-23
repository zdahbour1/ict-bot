# Design: Systematic Delta-Neutral Options Selling — Research + Recommendations

*ENH-026 — research companion to the existing `delta_neutral_strategy.py` / `delta_hedger.py` stack.*

## Executive summary

The bot has the *plumbing* for systematic short-vol (ATM straddle + wings, 30 s hedge loop, BS backtest, per-leg drill-down) but lacks the *edge filters* literature identifies as dominant P/L drivers: an IV-rank gate, a 30–60 DTE window (not ≤7), constant-delta strikes (~16Δ short), an earnings/FOMC blackout, a 50 %-max-profit exit, and a VIX term-structure regime filter. This doc distils the Bejar-Garcia framework, tastytrade / projectfinance / CBOE research, and five OSS repos into a phased ENH roadmap and a parameter-sweep search space for the existing backtest engine.

## 1. Framework from the Bejar-Garcia LinkedIn piece

Bejar-Garcia frames DN trading as three decoupled control loops rather than one entry/exit model:

1. **Scanner** — "systematically scan thousands of combinations… to identify optimal strike prices", gated by *liquidity* (spread ratio, OI, volume), not discretionary IV views. Range-bound names favour calendars/butterflies; trending ones favour strangles.
2. **Delta loop** — monitor every **30 s**; if delta deviates by **±0.05** trigger a hedge via underlying *or* roll strikes/expiries. Matches the repo's current cadence.
3. **Gamma / vega loop (separate from delta)** — "gamma neutrality deteriorates as expiration nears; implement automated triggers to roll or hedge gamma spikes." Vega is an *independent* sensitivity (example strangle vega = 2501.9) that must be re-sized as IV shifts.

Exits are qualitative: "track theta decay and calculate cost-benefit of closing early." No profit-target, DTE, or IV-contraction numbers are given — **the doc's weakest area**; tastytrade/projectfinance numbers fill the gap. **Repo implication**: #2 already exists; loop #3 (vega/gamma rolls) is absent.

## 2. Academic + practitioner consensus

| Dimension | Consensus finding | Source |
|---|---|---|
| **IV metric** | IV Rank ≥ 50 is the single most important filter; below IVR 30 the edge thins. IV percentile is a cross-check. | tastytrade, VolatilityBox |
| **DTE window** | **30–60 DTE**, enter ~45. Steepest theta while giving room to manage. Repo's ≤7 DTE is gamma-dominated, high variance. | projectfinance (71,417 trades) |
| **Strike delta** | **16Δ short / 5Δ long** → 78–83 % win rate on SPY 2005-2019 at 50 % mgmt. 30Δ variant best at 25-50 %. | projectfinance |
| **Hedging cadence** | Hybrid: fixed-time baseline + delta-band override. Pros hedge 4-8×/day; ±0.05 delta is a common trigger. Txn-cost drag dominates below. | VolatilityBox, Sinclair |
| **Exit** | **50 % of max credit** default (win-rate 64→82 %; time 27→14 days). 25 % for 30Δ, 75 % for 16Δ. Hard exit at 21 DTE. | tastytrade, projectfinance |
| **Regime** | Trade only in VIX contango and VIX 15-25. **Avoid backwardation** (front > M2) — wings get blown through. | VolatilityBox |
| **Event filter** | Skip earnings inside hold window; blackout ±1 day around FOMC/CPI. Pre-event vega expansion kills; post-event crush is fine. | tastytrade |
| **VRP** | SPX IV avg 19.3 % vs RV 15.1 % (1990-2018) → 4.2 ppt premium. CBOE PUT: β 0.56, vol 9.95 %, monthly α ≈ 0.2 %. Structural tailwind. | CBOE PUT white paper |
| **Sinclair** | *Volatility Trading* 2e ch. 4-6: optimal hedge cadence trades gamma P/L (∝ ½·Γ·ΔS²) vs theta + txn cost; Whalley-Wilmott utility band is classical. | Sinclair 2013 |

## 3. GitHub implementations survey

| Repo | Entry gate | Hedge cadence | Exit | Notable |
|---|---|---|---|---|
| **alpacahq/gamma-scalping** | Score = (|Θ|·w + txn) / Γ; OI ≥ 100; 30-90 DTE | Price ±$0.05 *or* 5 s heartbeat; delta threshold 2.0 | **None** (user-defined) | Cleanest reference for hybrid time+price hedge trigger |
| **aicheung/0dte-trader** | 0 DTE; IBKR; IC / IB modes | Intraday | Time-of-day based | Opposite DTE regime to what literature recommends — useful as anti-pattern |
| **themichaelusa/bestcondor** | Risk/reward ratio optimiser over candidate ICs | N/A (selection only) | N/A | Good strike-picker template |
| **alpacahq/alpaca-py iron-condor** | Static delta selection | None | At expiry | Minimal reference impl |
| **ishapiro/lumibot\_backtesting\_machine** | Param-driven: delta, width, DTE | N/A (backtest) | Time / delta / price / IV-based | Closest to *our* backtest model — exit-logic menu is worth mimicking |

Pattern across all five: **none** implement an IV-rank or VIX-term-structure gate — that is the most underbuilt piece in the OSS landscape and the biggest opportunity for differentiation.

## 4. Gap analysis — what's missing in this repo vs best practice

| Missing piece | Today in repo | Proposed ENH |
|---|---|---|
| IV-rank / IV-percentile gate | Entry uses rolling-std IV proxy + one flat threshold | **ENH-052**: compute 252-day IVR from ENH-035 BS-backed ATM IV; require IVR ≥ 30 (tunable) |
| DTE window | Next-Friday ≤ 7 DTE | **ENH-053**: select expiry closest to 45 DTE, clamp 30-60 |
| Constant-delta strikes | ATM straddle + fixed-dollar wings | **ENH-054**: solve for 16Δ short / 5Δ long each entry (use existing BS greeks) |
| Profit-target exit | None (runs to expiry/close) | **ENH-055**: close at 50 % of credit received; hard-exit at 21 DTE |
| Event calendar filter | None | **ENH-056**: earnings within hold-window + FOMC/CPI blackout table |
| VIX term-structure / regime | None | **ENH-057**: pull VIX + VIX3M; disable new entries when VIX1M/VIX3M > 1.0 (backwardation) |
| Vega / gamma independent loop | Only delta hedged | **ENH-058**: portfolio vega & gamma limits; roll/scale when breached |
| Position sizing per IVR | Fixed contracts | **ENH-059**: size ∝ IVR bucket and VIX level (risk-parity-lite) |
| Hybrid hedge trigger | Time(30 s) + price(0.30 %) | Already covered; add delta-band override (already wired via `DN_DELTA_BAND_SHARES`) — just needs tuning sweep |

## 5. Proposed multi-phase roadmap

**Phase A — this week (safety + biggest alpha levers):**
- ENH-052 IV-rank gate (replaces simple threshold). 1-day task given ENH-035 already backs out true BS IV.
- ENH-055 50 % profit-target exit in exit-manager.
- ENH-056 earnings/FOMC blackout list (CSV + IB fundamentals).

**Phase B — next 2 weeks (move entries into the literature's sweet spot):**
- ENH-053 DTE window 30-60 (this is a *big* backtest-surface change — rerun param sweeps).
- ENH-054 constant-delta strike selector.
- ENH-057 VIX term-structure regime filter.
- Full sweep over A+B on 3-5 years of SPY/IWM/QQQ history.

**Phase C — month+ (refinement):**
- ENH-058 gamma/vega loops (roll triggers, vega caps).
- ENH-059 IVR-bucketed position sizing.
- Extend beyond equity options to /ES FOPs once live P/L on equity side confirms edge.
- Walk-forward cross-validation; regime-conditional parameters.

**Deviation from Bejar-Garcia**: he is silent on profit targets and DTE; we *override* his qualitative "cost-benefit" exit with the projectfinance-backed **50 % / 21-DTE** rule because the empirical evidence (71k trades) is strong and the repo needs a deterministic exit for the exit-manager plumbing.

## 6. Parameters to optimise via the existing parameter-sweep engine

Feed these into `backtest_engine/multi_leg_sim.py` sweeps:

| Param | Setting key | Search range | Step |
|---|---|---|---|
| IV Rank threshold | `DN_IV_RANK_MIN` | 20 – 60 | 5 |
| IV lookback days | `DN_IV_RANK_LOOKBACK` | 126, 189, 252 | 3 values |
| Entry DTE target | `DN_TARGET_DTE` | 21, 30, 35, 45, 55 | — |
| Entry DTE tolerance | `DN_DTE_TOLERANCE` | ±5, ±10 | 2 |
| Short-strike delta | `DN_SHORT_DELTA` | 0.10 – 0.30 | 0.025 |
| Wing delta | `DN_LONG_DELTA` | 0.03 – 0.10 | 0.01 |
| Profit-target % | `DN_PROFIT_TGT_PCT` | 25, 40, 50, 60, 75 | — |
| Hard-exit DTE | `DN_EXIT_DTE` | 14, 21, 28 | — |
| Hedge delta band | `DN_DELTA_BAND_SHARES` | 3, 5, 8, 12, 20 | — |
| Hedge time interval | `DN_REBALANCE_INTERVAL_SEC` | 15, 30, 60, 120 | — |
| Event price-move trigger | `DN_EVENT_TRIGGER_BPS` | 20, 30, 50, 75 | — |
| VIX term-structure gate | `DN_VIX_TS_MAX` | 0.95, 1.00, 1.05 (off) | — |
| Position size per IVR% | `DN_SIZE_CURVE` | linear, step, flat | 3 |

Priority axes for the first sweep: **IVR × DTE × short-delta × profit-target** (4-D grid, ~2 k points) — these dominate P/L in every published study.

## 7. Citations / further reading

- Bejar-Garcia, "Beyond Directional Bets: Building Systematic Delta-Neutral Strategies", LinkedIn.
- Sinclair, E. *Volatility Trading*, 2nd ed., Wiley 2013 — ch. 4 (hedging), ch. 6 (dynamic hedging P/L), ch. on VRP.
- projectfinance, "Iron Condor Management Results from 71,417 Trades" — https://www.projectfinance.com/iron-condor-management/
- tastytrade / tastylive, SPY 45-DTE IC study (2005-2019, 4,872 trades).
- CBOE, "PUT Index White Paper — Volatility Risk Premium" — https://www.cboe.com/insights/posts/white-paper-shows-volatility-risk-premium-facilitated-higher-risk-adjusted-returns-for-put-index/
- VolatilityBox, "Iron Condor in High Volatility" & "Volatility Regimes Explained".
- GitHub: alpacahq/gamma-scalping, alpacahq/alpaca-py (iron-condor notebook), aicheung/0dte-trader, themichaelusa/bestcondor, ishapiro/lumibot\_backtesting\_machine.
- Velasquez C., "Detecting VIX Term Structure Regimes", Medium.
