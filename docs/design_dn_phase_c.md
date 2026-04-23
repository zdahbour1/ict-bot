# Design — DN Phase C refinements (ENH-058 + ENH-059)

**Status:** Proposed. Not urgent.
**Prereq:** Phases A + B shipped and validated in paper for 2+ weeks
**Builds on:** `docs/enh_026_dn_research_and_design.md`,
`docs/design_dn_phase_b.md`

## Purpose

Phase C is refinement. Phase A+B give the bot the published edges
(IV rank + DTE window + delta strikes + regime filter + discipline).
Phase C tunes **how much** to trade each signal and introduces
independent gamma/vega risk loops so the portfolio can't quietly
accumulate unsized exposure.

## 1. ENH-058 — Gamma + vega independent loops

### Problem

Today's delta-hedger flattens delta every 30 s or on 0.30 % moves.
But iron condors leak through **two other greeks**:

1. **Gamma** — near-the-money, short gamma grows fast as the
   underlying drifts toward the short strikes. Unchecked, a condor
   that started delta-neutral ends up with +200 gamma at expiration
   week (= any 1 % move becomes +$200 delta drift per minute).
2. **Vega** — even a net-theta positive condor is short vega. A
   single VIX up-move wipes out weeks of theta collection.

No loop currently bounds portfolio gamma or vega.

### Proposed design

Extend `delta_hedger.py` with **two additional periodic checks**:

```python
# Runs on the same 30 s cadence, separate bands.

def _check_gamma_band(client, trade):
    """Sum BS gamma × sign × contracts × mult across legs.
       If abs(net_gamma) > DN_GAMMA_MAX (default 30),
       queue a defensive roll: close the worst-gamma leg +
       reopen one wider."""

def _check_vega_band(client, trade):
    """Sum BS vega. If abs(net_vega) > DN_VEGA_MAX (default 100),
       emit warning + optional scale-down new entries."""
```

### New rebalance actions

Unlike delta (which rebalances via stock), gamma and vega can't
be hedged with shares — they require option-leg adjustments:

- **Gamma roll**: close the threatened short wing (the one closest
  to current price) and reopen it one strike further OTM. Moves
  the short wing back to ~0.16Δ.
- **Vega cap**: pause new DN entries; existing positions ride
  out. Log warning to `system_log`.

### Code impact

- `strategy/delta_hedger.py` — 2 new helpers + dispatch — ~120 LOC
- `strategy/dn_rolls.py` new — gamma roll logic (close + re-enter
  single leg) — ~180 LOC
- Need `place_single_leg_order` + a new `replace_short_wing`
  helper in `broker/ib_orders.py` — ~80 LOC
- Settings:
  - `DN_GAMMA_MAX` default 30
  - `DN_VEGA_MAX` default 100
  - `DN_GAMMA_ROLL_ENABLED` default true (start monitoring-only)
- `tests/unit/test_dn_gamma_vega.py` — 10 cases (each band: trigger,
  skip, rebalance action, roll wiring)
- `delta_hedges` audit table already flexible — add `action_kind`
  column (`delta` | `gamma_roll` | `vega_cap`) so chain is auditable.

### Effort

3-4 days. Most of the cost is the roll logic + making sure the
close+reopen is atomic (combo orders help — can submit as a two-leg
BAG: close old wing + open new wing in one order).

### Risk

- Gamma rolls incur additional commissions; need to ensure roll
  doesn't fire more often than it saves. Enforce a cooldown
  (`DN_ROLL_COOLDOWN_MIN` default 60).
- Vega cap is monitoring-only in v1 to avoid being too aggressive
  during IV crush (which is actually beneficial for short-vol).

---

## 2. ENH-059 — IVR-bucketed position sizing

### Problem

Today every DN entry opens `DN_CONTRACTS` contracts, regardless of
conditions. Best practice (Sinclair, tastytrade) is **risk-parity-lite**
— trade *more* when IVR is high (edge is stronger) and *less* when
low. Current uniform sizing means a low-IVR trade risks the same
capital as a high-IVR trade with 3× the edge.

### Proposed design

Replace `contracts = self.contracts` in `place_legs` with:

```python
def _size_by_ivr(base: int, ivr: float, vix: float) -> int:
    """Risk-parity-lite contract sizing:
       IVR 30-50: 1× base
       IVR 50-70: 2× base
       IVR 70+:   3× base
       VIX > 35 (panic): 0.5× base (cap upside, preserve capital)
    """
```

Wired into `place_legs` via the signal's `details["iv_rank"]`
(piggybacks on ENH-052).

### Code impact

- `strategy/dn_sizing.py` new — ~60 LOC
- `strategy/delta_neutral_strategy.py::place_legs` — 5 LOC change
- Settings:
  - `DN_SIZE_IVR_BUCKET_LOW / MID / HIGH` as multipliers (1.0 / 2.0 / 3.0)
  - `DN_SIZE_VIX_PANIC_CAP` default 0.5
- `tests/unit/test_dn_sizing.py` — 6 cases (each bucket + panic cap
  + boundary rounding down to at least 1 contract)

### Effort

1 day.

### Risk

- Total portfolio concentration: multiplier 3× + user has 10 open
  DN trades = 30× base allocation. Cross-strategy cap (ENH-037)
  already protects the per-underlying side; may need a
  **portfolio-level gross-notional cap** too:
  `MAX_PORTFOLIO_GROSS_NOTIONAL_USD`. Track as open question.

---

## Phase C combined delivery plan

| Day | Work |
|-----|------|
| Day 1 | ENH-058 gamma/vega greeks compute + band gates (monitoring-only) |
| Day 2 | ENH-058 gamma-roll logic via two-leg BAG |
| Day 3 | ENH-058 tests + rollout to monitor-only in live paper |
| Day 4 | ENH-059 sizing module + tests + ship |
| Day 5 | Paper validation of combined behavior |

## Rollback switches

| Setting | Default | Disable |
|---------|---------|---------|
| DN_GAMMA_ROLL_ENABLED | false initially | true after validation |
| DN_GAMMA_MAX | 30 | set to very high (1e9) to disable |
| DN_VEGA_MAX | 100 | same |
| DN_SIZE_IVR_BUCKET_* | 1/2/3 | set all to 1.0 → uniform sizing |

## Dependencies

- ENH-052 IVR gate — directly feeds ENH-059 sizing
- ENH-054 constant-delta strikes — required for sensible gamma rolls
  (rolling an ATM straddle doesn't make sense)
- ENH-057 regime filter — works in concert: backwardation pauses
  new entries, vega cap pauses regardless of regime
- ENH-049 delta hedger — Phase C extends its loop with gamma/vega
  checks on the same 30-s tick

## Combined effort

**Phase C total: 5 days** implementation, 2-3 weeks of paper
validation before enabling rolls in live trading.

## Open questions

1. **Portfolio-level gross-notional cap** — need a new setting
   `MAX_PORTFOLIO_GROSS_NOTIONAL_USD`? What's the right default
   as a fraction of account equity?
2. **Gamma-roll vs. exit** — when gamma is blown out AND the
   trade is at a loss, is rolling the wing worse than just
   closing? Needs backtest-driven decision.
3. **Intraday IVR refresh** — today IVR is computed at entry;
   for a held trade, does intraday IVR change modify the exit
   decision? Sinclair suggests yes.

---

## What Phase C does NOT do

- Does not replace the 30 s delta loop — it ADDS parallel checks.
- Does not introduce options-on-options (no gamma-neutralizing with
  straddles). That's Phase D+ territory, deferred until we see
  whether Phase A+B+C gets profitability.
- Does not adopt Bejar-Garcia's "cost-benefit" exit calculus —
  projectfinance's 50 % / 21 DTE rule (shipped in Phase A as
  ENH-055) remains the exit discipline.
