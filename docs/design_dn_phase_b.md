# Design — DN Phase B sweet-spot entries (ENH-053 + ENH-054 + ENH-057)

**Status:** Proposed
**Prereq:** Phase A shipped; IB historical IV data flowing
**Builds on:** `docs/enh_026_dn_research_and_design.md`,
`docs/design_dn_phase_a.md`

## Purpose

Phase A shipped *when* to enter (IV rank, event blackout, exit
discipline). Phase B ships *how* to construct the condor so it
matches the literature's sweet-spot: **30-60 DTE**, **16Δ short
wings / 5Δ long wings**, and a **VIX regime filter** that pauses
entries during vol-expansion regimes.

## 1. ENH-053 — 30-60 DTE window (replace next-Friday weeklies)

### Problem
Today's `_next_expiry_yyyymmdd()` picks the next Friday ≥ 1 DTE.
That lands the bot in **1-7 DTE weeklies** — the opposite of the
theta-harvest sweet spot. Academic + projectfinance consensus:
**45 DTE** entries + close at 21 DTE produce the best risk-adjusted
returns. Weeklies have inflated gamma and event-risk per unit of
premium.

### Proposed design

Rewrite `_next_expiry_yyyymmdd()` → `_select_target_expiry()`:

```python
def _select_target_expiry(
    underlying: str,
    target_dte: int = 45,
    min_dte: int = 30,
    max_dte: int = 60,
    client = None,
) -> str:
    """Return YYYYMMDD of the listed expiry closest to target_dte
    within [min_dte, max_dte]. Uses IB option chain param query.
    Falls back to the monthly 3rd-Friday if quarterly chains aren't
    available for the underlying."""
```

**Chain query**: use existing `ib_contracts.ib_get_atm_symbol`
pattern to list the option chain's `expirations` set. Filter to the
DTE window, pick closest to target.

### Code impact
- `strategy/delta_neutral_strategy.py::_select_target_expiry` —
  replace the helper — ~60 LOC
- `broker/ib_contracts.py` — add `list_chain_expirations(underlying)`
  — ~40 LOC
- Settings:
  - `DN_TARGET_DTE` default 45
  - `DN_MIN_DTE` default 30
  - `DN_MAX_DTE` default 60
- `tests/unit/test_expiry_selection.py` — 6 cases (target hit, fallback
  to nearest in window, outside window reject, empty chain, monthly
  fallback, clamping)

### Effort
1-2 days.

### Risk
- **Backtest-surface change**: 45-DTE trades need ~6 weeks of
  historical data per trade; current backtest range may need extending.
  Re-run existing sweeps on 3-5 years of IB historical option data.
- Weekly-only tickers (IWM, small caps) may lack monthly chains —
  emit warning + skip rather than crash.

---

## 2. ENH-054 — 16Δ short / 5Δ long constant-delta strikes

### Problem
Today's `place_legs` builds strikes at:
- short legs: `_round_to_interval(current_price, strike_interval)` = ATM
- long legs: `ATM ± wing_width` (fixed dollars)

This is brittle — ATM delta is ~50Δ, way too aggressive for a
short-vol condor. Standard practice across tastytrade / projectfinance
/ Sinclair: **select strikes BY DELTA**, not by price offset.
16Δ short means ~1 standard deviation OTM — only a 16% probability
of being breached at expiration.

### Proposed design

New helper `strike_by_delta()`:

```python
def strike_by_delta(
    underlying_price: float,
    target_delta: float,
    dte_days: float,
    sigma: float,
    right: str,
    strike_interval: float = 5.0,
) -> float:
    """Solve for the strike K such that BS delta(S, K, T, r, sigma, right)
    ≈ target_delta. Uses bisection on strike grid; returns the
    listed strike closest to the solution."""
```

Use `backtest_engine.option_pricer.bs_greeks` for the delta calc
(already imported in `delta_hedger.py`). For short_call target
is +0.16, for short_put target is -0.16, long wings at ±0.05.

Wire in `place_legs`:

```python
sigma = details.get("iv_source_sigma") or 0.20
dte   = _dte_from_expiry(expiry)
sc_k = strike_by_delta(current_price, 0.16, dte, sigma, "C",
                        strike_interval)
lc_k = strike_by_delta(current_price, 0.05, dte, sigma, "C",
                        strike_interval)
sp_k = strike_by_delta(current_price, -0.16, dte, sigma, "P",
                        strike_interval)
lp_k = strike_by_delta(current_price, -0.05, dte, sigma, "P",
                        strike_interval)
```

### Code impact
- `strategy/delta_neutral_strategy.py` — replace strike math
  (~40 LOC change)
- `strategy/dn_strikes.py` new module — `strike_by_delta` ~60 LOC
- Settings:
  - `DN_SHORT_DELTA` default 0.16
  - `DN_LONG_DELTA` default 0.05
- `tests/unit/test_strike_by_delta.py` — 8 cases (short call, short put,
  long call, long put, rounding to interval, IV sensitivity, boundary
  at deep-OTM, degenerate 0-delta)

### Effort
1.5 days.

### Risk
- `sigma` input: prefer `iv_source_sigma` from ENH-035's ATM IV
  backout. Fallback to 0.20 if absent.
- Strike interval varies by underlying ($1 for IWM, $5 for SPY, $10
  for some). Already in tickers table as `strike_interval`; read
  from there.
- On a low-liquidity ticker the 5Δ strike may have no listed option.
  Degrade gracefully: widen long-delta target until a listed strike
  exists.

---

## 3. ENH-057 — VIX / VIX3M regime filter

### Problem
Delta-neutral condors lose badly in vol-expansion regimes (March
2020, Oct 2022). The most reliable heuristic: **VIX term-structure
backwardation** — when `VIX1M/VIX3M > 1.0` the market is pricing
near-term turmoil. Entering new condors here is pouring gas on a fire.

### Proposed design

New entry gate in `delta_neutral_strategy.detect()`:

```python
if settings.DN_REGIME_FILTER_ENABLED:
    ratio = _vix_term_structure_ratio(client)   # VIX / VIX3M
    if ratio >= settings.DN_REGIME_MAX_RATIO:   # default 1.0
        return []                                # skip entry
```

`_vix_term_structure_ratio` caches the quote for 60s so we don't
hammer IB:

```python
def _vix_term_structure_ratio(client) -> float:
    """Returns VIX last / VIX3M last. Values > 1 → backwardation."""
    cached = _REGIME_CACHE.get('ratio')
    if cached and (time.time() - cached[1]) < 60:
        return cached[0]
    vix = client.get_index_price('VIX')       # CBOE
    vix3m = client.get_index_price('VIX3M')
    ratio = vix / vix3m if vix3m > 0 else 0.0
    _REGIME_CACHE['ratio'] = (ratio, time.time())
    return ratio
```

### Code impact
- `broker/ib_market_data.py::get_index_price` — ~30 LOC (Contract
  type=IND, exchange=CBOE)
- `strategy/dn_regime.py` — ~80 LOC
- `strategy/delta_neutral_strategy.py::detect` — 5 LOC gate
- Settings:
  - `DN_REGIME_FILTER_ENABLED` default true
  - `DN_REGIME_MAX_RATIO` default 1.0 (backwardation threshold)
  - `DN_REGIME_COOLDOWN_MIN` default 120 (how long after regime
     clears before new entries resume — avoids flip-flopping)
- `tests/unit/test_dn_regime.py` — 5 cases (contango allows, backward.
  blocks, cache hit, missing VIX3M degrade gracefully, cooldown expiry)

### Effort
1 day.

### Risk
- VIX + VIX3M are index quotes (SPX-derived) — ensure IB index
  data entitlement is available on the user's paper account. Cheap;
  fallback is to skip the filter (setting=false) if unavailable.
- Signal "latch": once tripped, keep blocking entries for the cooldown
  so we don't enter-exit-enter during a ratio-crossing-1.0 chop.

---

## Phase B combined delivery plan

| Day | Work |
|-----|------|
| Day 1 | `list_chain_expirations` + `_select_target_expiry` + tests |
| Day 2 | Re-run backtests on 3-5y history with 45-DTE window — calibrate |
| Day 3 | `strike_by_delta` + `place_legs` rewrite + tests |
| Day 4 | VIX regime filter + `get_index_price` + tests + ship Phase B |

## Rollback switches

| Setting | Default | Disable |
|---------|---------|---------|
| DN_TARGET_DTE | 45 | n/a (reverting means restoring `_next_expiry_yyyymmdd`) |
| DN_SHORT_DELTA / DN_LONG_DELTA | 0.16 / 0.05 | Set to 0.50 / 0.25 emulates old ATM/fixed behavior |
| DN_REGIME_FILTER_ENABLED | true | false |

## Dependencies

- ENH-052 IVR (Phase A) — interaction: in backwardation, IV usually
  jumps; IVR gate would pass, but regime filter supersedes — both
  must pass independently.
- ENH-054 needs live sigma from ENH-035 ATM IV; a reliable BS-backed
  IV is the foundation for delta-targeted strike math.
- ENH-038 backtest engine — already supports per-leg BS pricing;
  needs small update to pass the *selected* strikes through the
  backtest simulation instead of the fixed-interval strikes (~40 LOC
  in `backtest_engine/multi_leg_sim.py`).

## Combined effort
**Phase B total: 4 days** implementation + 1-2 days paper validation.
