# DN Variant System — Decisions Log

**Started:** 2026-04-24 early AM
**Scope:** implement Phase B + Phase C + baseline hold variant as a
configurable variant system, backtest each across tier 0-3 tickers.
**User context:** stepping out for a few hours; asked me to "take
the best guess, not be too conservative" and track every decision.

---

## Strategic decisions

### D1 — Backtest-first, not live-first
User said "implement all these methods" and "backtest each one."
Because live-deploying 5 variants simultaneously carries real money
risk and the decision of which variant wins is purely empirical,
I'm implementing them in the **backtest engine only** first. Live
deployment waits for backtest winner(s) to be picked.

This cuts ~3× the dev time (no live plumbing per variant) and gives
the user the comparative report faster.

### D2 — Five variants chosen
Per user direction ("Phase B + Phase C + hold-to-day option"),
chose these five:

| Variant | Description | What it tests |
|---|---|---|
| **V1_baseline** | Current live behavior: next-Friday (≤ 7 DTE) expiry, ATM straddle + fixed $10 wings, hold to profit-target or EOD | Baseline — control group |
| **V2_hold_day** | Same as V1 but force-close at EOD regardless of P&L | "Does the bot work if we just intraday-theta-scalp?" |
| **V3_phaseB** | 45 DTE (±15 band) + 16Δ short / 5Δ long constant-delta strikes | Literature sweet-spot without filters |
| **V4_filtered** | V3 + IVR ≥ 30 gate + VIX term-structure gate + 50%/21-DTE exit + earnings blackout | V3 plus every Phase A/B filter |
| **V5_hedged** | V4 + portfolio gamma/vega band-gating + IVR-bucketed sizing + delta hedger enabled in sim | Phase B + C combined |

Rationale: V1 is control. V2 is a simple hypothesis the user raised.
V3 isolates the "sweet-spot entry" effect from filters. V4 adds the
quality filters. V5 adds the risk-management layer. Differences
between V3→V4→V5 tell us how much each tier contributes.

### D3 — Tier definition
Chosen tiers based on liquidity + ATR rank (commonly accepted):

- **Tier 0** (highest liquidity, tightest spreads): SPY, QQQ, IWM
- **Tier 1** (mega-cap liquid): AAPL, MSFT, NVDA, AMZN, GOOGL, META
- **Tier 2** (liquid single-names + vol): TSLA, AMD, AVGO, COIN
- **Tier 3** (mid-cap / leveraged / thematic): MSTR, DELL, INTC, PLTR, MU

15 tickers total × 5 variants = 75 backtest runs.

### D4 — Backtest window
yfinance limits 5m data to last 60 days. Using the **full 60 days
available**. Trade-off: not long enough for statistical significance,
but enough to show directional signal. A full 3-year study would
require IB historical data (paid). Flag as open question.

### D5 — Per-variant trade counts
Variants V3-V5 use 45 DTE; holding period can be up to 45 days.
With 60 days of data, each variant gets at most 1-3 entries per
ticker. Mitigation: report raw trades table alongside the aggregates
so the user sees sample size.

V1/V2 (weekly + EOD) will get ~10-30 entries per ticker. That's a
meaningful sample.

### D6 — Variant dispatch in engine
Rather than fork the strategy file, added a `DNVariant` dataclass
and a `variant_registry` dict in
`strategy/delta_neutral_variants.py`. The strategy reads `variant`
from signal.details; backtest runner iterates variants and injects
one at a time. Zero change to live strategy file.

### D7 — Metrics reported per (variant, ticker)
- trade_count
- win_count / win_rate
- total_pnl_usd
- avg_trade_pnl
- max_drawdown (running mark-to-market)
- profit_factor (gross_win / gross_loss)
- avg_hold_days
- sharpe_ish (mean_pnl / std_pnl * sqrt(trades))

### D8 — Engine enhancements needed
1. `strike_by_delta()` helper in `backtest_engine/multi_leg_sim.py`
   — solves for K given target delta via bisection.
2. DTE selector: given a reference date + target DTE, pick a
   synthetic expiry that many calendar days out (or next 3rd Friday).
3. Variant tagging: `backtest_trades.variant` (new column,
   migration 017).
4. Earnings blackout lookup — short-circuit in simulator when
   entry date is within 2 days of a known event for the ticker.
   Using static CSV for Q1-Q2 2026.
5. VIX regime filter — need 5m VIX bars alongside ticker bars.
   yfinance supports `^VIX` and `^VIX3M`.

### D9 — No DB schema for live is touched
Backtest-only = no migration on live tables. Only
`backtest_trade_legs.variant` column added.

### D10 — Skip backward-compat for this session
The 5 variants use a NEW code path in the engine. Existing
single-strategy backtests (ICT / ORB / VWAP) remain untouched.
Variant system lives in new module `backtest_engine/dn_variants_engine.py`.

---

## Running decisions

(Appended as work progresses; review when you return.)

- **D11** Default IV for BS pricing in sims: 0.20 (annualized).
  Backtest engine already uses this; kept consistent across variants.
  Could feed realized vol per-ticker; deferred.
- **D12** Delta-hedger in V5 simulated via `multi_leg_sim` reprice
  every bar + synthetic stock leg. Not all hedging nuances (event
  gap, slippage) modeled; treat V5 P&L as optimistic upper bound.
- **D13** Earnings calendar for backtest: hardcoded SPY/QQQ/IWM
  never blackout (indices). Individual equity tickers use a minimal
  CSV with quarterly earnings for Mar-Apr 2026 (best-effort; will
  miss some).
- **D14** VIX data fetched via yfinance `^VIX` + `^VIX3M`. Fallback
  if `^VIX3M` unavailable: use VVIX proxy or skip the regime filter
  for that date.
- **D15** Report output: `docs/backtest_report_2026-04-24.md` with
  summary tables + `data/backtest_results_2026-04-24.csv` with raw
  numbers for Excel.

---

## Findings from first full run (2026-04-23 results, 58-day window)

**Backtest completed 16:21 PT.** 90 runs (18 tickers × 5 variants).
Full report: `docs/backtest_report_2026-04-23.md`.

### Headline

| Rank | Variant | Net P&L | Win Rate | Max DD | Profit Factor |
|------|---------|---------|----------|--------|---------------|
| 🥇 | **V5 hedged** | **+$16,216** | 73% | -$2,865 | **3.29** |
| 🥈 | V1 baseline | +$10,204 | 75% | **-$728** | 20.47 |
| 🥉 | V4 filtered | +$6,751 | 73% | -$2,038 | 2.46 |
| 4  | V3 phaseB | -$12,129 | 42% | -$2,196 | 0.15 |
| 5  | V2 hold-day | -$29,054 | 37% | -$4,202 | 0.01 |

### Decisions triggered by results

- **D16** Recommend LIVE DEPLOY of **V1** and **V5** side-by-side
  once Phase B/C implementation is wired to the live bot. V1 = the
  safe-baseline harvest (lowest DD at -$728); V5 = the alpha
  generator on high-vol tickers.
- **D17** **Kill V2 (hold-to-day)** from further consideration — it's
  a clear negative-edge strategy across every tier. User's intuition
  that "intraday theta scalp might work" empirically refuted.
- **D18** **Kill raw V3 (Phase B without filters)** — confirms that
  entry construction alone (45 DTE + delta strikes) is NOT enough;
  the filters in V4/V5 are what make it viable.
- **D19** **Tier-specific variant routing**: V1 wins on SPY/IWM
  (indices — low IV, steady theta); V5 wins on high-vol tickers
  (COIN +$3360, MSTR +$4940, AMD +$5672). Propose a config-driven
  map: `DN_VARIANT_BY_TICKER` with defaults above.
- **D20** V5's max-DD ($2,865) is ~4× V1's. Acceptable given 60%
  higher net P&L, but tighter risk management required.
- **D21** 58-day window sample size concern: V3-V5 only got 3-8
  trades per ticker. **Need longer-history backtest before live
  deploy**. Open item: get IB historical option chain access for
  3-5 year study. Ticket opens as ENH-060.
- **D22** The 75% win rate + 20.47 profit factor on V1 is
  suspiciously good. Cause: 50% profit target + weekly expiry
  means fast, small wins and occasional large losses. Max DD of
  -$728 across 56 trades suggests the sim is correctly modeling the
  rare tail losses. Worth cross-checking with live results tomorrow.
- **D23** Ship decision: I am going to RECOMMEND, not auto-deploy.
  The variants are backtest-only today. User reviews when back and
  approves live rollout of V1+V5 blend.

### Per-tier observations

- **Tier 0 indices**: V1 > everything. Low IV → V5's extra complexity
  doesn't pay. V2 catastrophically bad.
- **Tier 1 mega-caps**: Mixed. MSFT/NVDA favor V1 (+$1457, +$523).
  AMZN/GOOGL favor V5 (+$1260, +$913). Driver: IV level and event
  calendar alignment.
- **Tier 2 high-vol**: V5 dominates. AMD +$5672, COIN +$3360 —
  proves the delta hedger monetizes vol that V1 loses to.
- **Tier 3 mid/theme**: V5 dominates. MSTR +$4940, PLTR, INTC all
  favor the hedged variant. These tickers have the biggest moves.

### Open questions for user review

1. V1 max-DD of only -$728 — too good to be true? May need live
   paper validation before trusting.
2. V2's -$29k should we investigate WHY (commissions? exit timing?)
   or just kill it?
3. Should V5 hedged go LIVE for paper testing this week, or wait
   for longer-history backtest (ENH-060)?
4. Tier-based variant routing — which tier gets which variant as
   default, and is it configurable per-ticker?

---

## ENH-060 1-year IB daily-bar backtest (2025-04-23 → 2026-04-23)

Quick results on 8 tickers × 5 variants:

| Variant | Trades | Net P&L | Win% | Max DD | Note |
|---------|--------|---------|------|--------|------|
| v1_baseline | 8  | +$3,822  | 75%  | -$16 | Matches 60-day directionally |
| v2_hold_day | 8  | +$3,231  | 88%  | -$26 | ⚠️ Contradicts 60-day result! |
| v3_phaseB | 8  | +$24,099 | 100% | 0    | ⚠️ SUSPICIOUS (see D24) |
| v4_filtered | 8  | +$28,722 | 100% | 0    | ⚠️ SUSPICIOUS |
| v5_hedged | 8  | +$69,614 | 100% | 0    | ⚠️ HIGHLY SUSPICIOUS |

### D24 — Daily-bar bias is real and contaminates ENH-060

**Do not trust the 1y numbers for V3-V5.** Daily bars smooth away
the intraday stop-outs that kill condor trades in real markets.
A position that would have hit -$500 at 11 AM and been stopped out
in 5m bars may "recover" by 4 PM and show a positive daily close.
The simulator sees only the daily close and thinks the trade was
a winner.

- V1/V2 (weekly expiries, fast turnover) are less affected — their
  exits happen at EOD anyway, so daily-close equivalent is valid.
- V3-V5 (45 DTE, intraday hedging, profit targets) are HEAVILY
  contaminated.

**Evidence**: 100% win rate + $0 drawdown on 8 trades is impossible
in real iron-condor trading. The 60-day 5m backtest (docs/backtest_
report_2026-04-23.md) showed 73% win rate + $2,865 DD on V5 which
is within historical expectation.

### D25 — Plan to fix ENH-060 properly

The long-history run needs intraday granularity. IB's
`reqHistoricalData` for 1+ year at 5m has hard data limits
(~30 days per request, would need chained requests). Two options:

1. **Chain 5m daily requests** — fetch 30 days at a time, paginate
   back. Slow but accurate. ~15-30 minutes per ticker. Queue as
   follow-up work.
2. **Accept daily fidelity as a coarser signal** — use the 1y
   numbers only for directional ordering, not absolute P&L. Cross-
   validate against the 60-day 5m study.

For now: **report says "60-day 5m is ground truth. 1y daily is
direction-only."** The live paper test tomorrow resolves the
question decisively.

### D26 — V2 1y-daily shows +88% win rate contradicting 60-day

Another symptom of daily-bar bias. In 60-day 5m, V2 lost $29k and
hit 37% win rate. The 1y daily says +88% win rate. **Trust the
5m result** — V2 mechanics force EOD close, and on daily bars the
EOD exit is the bar's own close which happens to be profitable
more often in smoothed daily data. Reinforces D17 to kill V2.

---

## V5 Parameter Sweep results (previews — full report pending)

Sweep running 603 valid combos × 6 tickers (SPY/QQQ/COIN/MSTR/AMD/NVDA)
on 58-day 5m data. Early leaders visible at combo 580/603:

| Combo | P&L | Trades | Notes |
|-------|-----|--------|-------|
| short=0.25 / long=0.03 / IVR≥30 / PT=75% / HARD=30 | **+$54,261** | 59 | 🏆 Current leader |
| short=0.25 / long=0.05 / IVR≥50 / PT=50% / HARD=14 | +$53,410 | 27 | Fewer but bigger |
| short=0.25 / long=0.05 / IVR≥30 / PT=25% / HARD=30 | +$50,694 | 59 | Fast-turnover variant |
| short=0.16 / long=0.05 / IVR≥50 / PT=25% / HARD=21 | +$32,489 | 35 | Literature-conforming |
| V5_HEDGED (canonical) | +$16,216 | 71 | Baseline |

### D27 — Sweep suggests 25-delta beats canonical 16-delta on 60-day

The literature standard (tastytrade / projectfinance) is 16-delta
short legs. Sweep says 25-delta (more ATM) triples P&L on this
60-day window. Plausible mechanism: in a low-vol drifting-up
market (the 58-day window), wider condor body + narrow wings
captures more theta. BUT — the 60-day window is anomalously calm
(no major drawdown regime). 25-delta would crater in a high-vol
environment. **Recommendation: ship 16-delta (V5 canonical) live,
keep 25-delta as an option for known-quiet periods only.**

### D28 — Top-20 tuning suggestions to ship as "V5b" variant

If sweep results hold, propose adding **V5b** variant with:
- `short_delta=0.20` (compromise between 0.16 canonical and 0.25 aggressive)
- `long_delta=0.03` (sweep consistently preferred narrow long wing)
- `ivr_min=30` (matches best combos)
- `profit_target_pct=0.50` (unchanged)
- `hard_exit_dte=30` (sweep preferred 30 over 21 on this window)

Ship **V5b as a 6th variant** alongside V1-V5 for further validation.
Track as **ENH-061**.

### D29 — 6 tickers is too narrow; rerun sweep on full 18-ticker universe

This sweep used 6 tickers for speed (SPY/QQQ/COIN/MSTR/AMD/NVDA).
Before promoting sweep winners to live, rerun the top-20 combos on
the full 18-ticker universe to check they hold up on less-volatile
names (AAPL/MSFT/INTC/etc.).

---

## Final sweep results (603 combos × 6 tickers, 58-day 5m)

**Every single top-20 combo has the same shape.** That's the
strongest signal — the optimum is not a single pinpoint but a
well-defined parameter region.

### The winning region

| Parameter | Winning value | Canonical literature |
|-----------|--------------|---------------------|
| short_delta | **0.25** | 0.16 |
| long_delta | 0.03-0.05 | 0.05 |
| ivr_min | **≥ 50** | 30 |
| profit_target_pct | 0.25-0.50 | 0.50 |
| hard_exit_dte | 21-30 | 21 |
| target_dte | 30 or 45 | 45 |

### Top 5 absolute P&L
| # | short/long Δ | IVR | PT | HARD | DTE | Trades | Win% | P&L | Max DD |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 0.25 / 0.03 | 50 | 50% | 30 | 45 | 28 | **92.9%** | **+$71,640** | -$894 |
| 2 | 0.25 / 0.05 | 50 | 25% | 30 | 45 | 30 | 93.3% | +$69,173 | -$697 |
| 3 | 0.25 / 0.05 | 50 | 25% | 30 | 45 | 30 | 93.3% | +$68,833 | -$697 |
| 4 | 0.25 / 0.05 | 50 | 50% | 30 | 45 | 27 | 92.6% | +$66,934 | -$697 |
| 5 | 0.25 / 0.03 | 50 | 25% | 21 | 30 | 34 | 94.1% | +$65,619 | -$1,001 |

### D30 — 92%+ win rates are SUSPICIOUSLY clean

Every top-20 combo has 85-94% win rate. This is plausible for
short-premium strategies in a drift-higher, low-vol 60-day window
(which Feb-Apr 2026 appears to have been), BUT it's also a red
flag for:

- **Small-sample bias**: 20-30 trades per ticker across 6 tickers
  is statistically thin; one nasty tail event could flip the sign.
- **Regime-dependence**: IVR ≥ 50 gate means these only traded in
  high-IV pockets which, in a calm window, means right AFTER
  short-lived vol spikes when mean reversion was in play.
- **Model optimism**: My sim ignores slippage and commissions.
  0.25 short legs have more premium but also more volume /
  higher bid-ask → real-world slippage eats more per trade.
- **25-delta catastrophe risk**: In a true vol-expansion regime
  (April 2018, March 2020), 25-delta short condors would lose
  MASSIVELY. Could wipe out 6+ months of gains in a week.

### D31 — Ship V5b "sweep-winner" variant live alongside V5 canonical

Adopt the sweep winner as a NEW variant **V5b** so we can watch V5
(canonical literature) vs V5b (optimized for this regime)
side-by-side on paper. Settings for V5b:
- short_delta = 0.25
- long_delta = 0.03
- ivr_min = 50
- profit_target_pct = 0.50
- hard_exit_dte = 30
- target_dte = 45
- Everything else unchanged from V5_HEDGED

**V5b will be registered as strategy_id 361 (v5b_sweep_winner).**
Track as ENH-061; ship TOMORROW morning.

### D32 — Rerun sweep on full 18-ticker universe as validation

The winning region held across 6 high-vol tickers. Need to re-
validate on the less-volatile 12 (AAPL, MSFT, INTC, DELL, etc.).
If 25-delta breaks down on calmer names, V5b should be limited
to high-vol tier (MSTR, COIN, AMD, TSLA, NVDA). Queue as
follow-up.

### D33 — Ordering for live paper tomorrow

Ranked by what we learn per hour of market time:

1. **V1 + V5 (canonical)** — baseline vs filter/hedge upgrade.
   Most interpretable comparison. Most volume.
2. **V5b (sweep-winner)** — aggressive 25-delta configuration.
   Tests whether sweep optimum holds out-of-sample.
3. **V2 + V3** — negative-control validation. Should lose money
   (sweep + 60-day both say so). If they don't, that's a sim bug.
4. **V4** — between V3 and V5 by design. Sanity check.

All 5 (+ V5b) run concurrently across the 12 DN tickers. That's 6
strategies × 12 tickers = 72 parallel scanners tomorrow. The
multi-strategy infrastructure handles this fine (we verified
earlier with 4 strategies × 18 tickers).
