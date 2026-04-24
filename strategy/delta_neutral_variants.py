"""Delta-neutral variant configuration registry.

Five variants tested side-by-side per the user's 2026-04-24 direction
to implement Phase B + Phase C + a simple hold-to-day baseline and
backtest which one yields the highest profit with lowest drawdown.

Each variant is a frozen dataclass that configures:
  - Expiry selection (weekly vs 45-DTE)
  - Strike selection (ATM+fixed vs delta-targeted)
  - Entry filters (IVR, VIX regime, event blackout)
  - Exit discipline (profit target, hard DTE, EOD)
  - Position sizing (flat vs IVR-bucketed)
  - Delta hedging (on/off)

See `docs/dn_variant_decisions.md` for rationale and
`docs/backtest_report_2026-04-24.md` for outcomes.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class DNVariant:
    name: str
    label: str                # short printable tag: 'V1', 'V2', ...

    # Expiry selection
    target_dte: int = 7
    min_dte: int = 1
    max_dte: int = 14

    # Strike selection
    strike_mode: str = "atm_plus_wing"   # or 'delta_targeted'
    short_delta: float = 0.16
    long_delta: float = 0.05
    wing_width_dollars: float = 10.0
    strike_interval: float = 5.0

    # Entry filters
    ivr_min: float = 0.0          # 0 = disabled
    regime_filter: bool = False   # VIX/VIX3M > 1.0 blocks new entries
    event_blackout: bool = False  # earnings/FOMC within 2 days blocks

    # Exit discipline
    profit_target_pct: float = 0.50  # close at 50% of max profit
    hard_exit_dte: int = 0           # 0 = disabled; typical 21
    eod_close: bool = False          # force close at last bar of day
    hold_days_max: int = 60          # sanity cap

    # Position sizing
    sizing_mode: str = "flat"     # or 'ivr_bucketed'
    base_contracts: int = 1
    ivr_size_map: dict = field(default_factory=lambda: {
        30: 1, 50: 2, 70: 3,
    })
    vix_panic_cap: float = 35.0   # if VIX > this, size × 0.5

    # Risk management
    delta_hedge: bool = False     # simulate the stock hedge
    gamma_vega_caps: bool = False # Phase-C gamma+vega band checking


# ── Five canonical variants for this backtest round ──

V1_BASELINE = DNVariant(
    name="v1_baseline",
    label="V1",
    # Current live behavior: next-Friday weekly, ATM straddle + $10 wings,
    # hold to TP or manual close.
    target_dte=7, min_dte=1, max_dte=14,
    strike_mode="atm_plus_wing",
    wing_width_dollars=10.0,
    profit_target_pct=0.50,
    eod_close=False,
    sizing_mode="flat",
)

V2_HOLD_DAY = DNVariant(
    name="v2_hold_day",
    label="V2",
    # User's hypothesis: intraday theta scalping. Open at signal,
    # force close at EOD. Aggressive weekly expiry.
    target_dte=1, min_dte=1, max_dte=7,
    strike_mode="atm_plus_wing",
    wing_width_dollars=10.0,
    profit_target_pct=0.50,
    eod_close=True,
    sizing_mode="flat",
)

V3_PHASEB = DNVariant(
    name="v3_phaseB",
    label="V3",
    # Phase B sweet-spot entries: 45 DTE + constant-delta strikes.
    # No filters yet — isolate the "entry construction" effect.
    target_dte=45, min_dte=30, max_dte=60,
    strike_mode="delta_targeted",
    short_delta=0.16, long_delta=0.05,
    profit_target_pct=0.50,
    hard_exit_dte=21,
    sizing_mode="flat",
)

V4_FILTERED = DNVariant(
    name="v4_filtered",
    label="V4",
    # Phase B + Phase A filters (IVR, regime, blackout) + exit discipline.
    target_dte=45, min_dte=30, max_dte=60,
    strike_mode="delta_targeted",
    short_delta=0.16, long_delta=0.05,
    ivr_min=30.0,
    regime_filter=True,
    event_blackout=True,
    profit_target_pct=0.50,
    hard_exit_dte=21,
    sizing_mode="flat",
)

V5_HEDGED = DNVariant(
    name="v5_hedged",
    label="V5",
    # V4 + Phase C risk management: delta hedger sim + IVR sizing.
    target_dte=45, min_dte=30, max_dte=60,
    strike_mode="delta_targeted",
    short_delta=0.16, long_delta=0.05,
    ivr_min=30.0,
    regime_filter=True,
    event_blackout=True,
    profit_target_pct=0.50,
    hard_exit_dte=21,
    sizing_mode="ivr_bucketed",
    delta_hedge=True,
    gamma_vega_caps=True,
)

# ENH-061 / D31 — sweep-winning configuration from 2026-04-23 scan of
# 603 V5 parameter combos. Optimum was the 25-delta short / 3-delta
# long / IVR≥50 / 50% profit target / 30-DTE hard exit region; this
# variant locks that in so V5_HEDGED (literature canonical) and V5B
# (regime-optimized) can be compared head-to-head on live paper.
V5B_SWEEP_WINNER = DNVariant(
    name="v5b_sweep_winner",
    label="V5b",
    target_dte=45, min_dte=30, max_dte=60,
    strike_mode="delta_targeted",
    short_delta=0.25, long_delta=0.03,   # sweep-winning deltas
    ivr_min=50.0,                         # only high-IV pockets
    regime_filter=True,
    event_blackout=True,
    profit_target_pct=0.50,
    hard_exit_dte=30,                    # sweep preferred 30 over 21
    sizing_mode="ivr_bucketed",
    delta_hedge=True,
    gamma_vega_caps=True,
)


VARIANTS: list[DNVariant] = [
    V1_BASELINE, V2_HOLD_DAY, V3_PHASEB, V4_FILTERED,
    V5_HEDGED, V5B_SWEEP_WINNER,
]

VARIANT_BY_NAME: dict[str, DNVariant] = {v.name: v for v in VARIANTS}


def get_variant(name: str) -> DNVariant:
    v = VARIANT_BY_NAME.get(name)
    if v is None:
        raise KeyError(f"unknown DN variant: {name!r}; "
                       f"known: {list(VARIANT_BY_NAME)}")
    return v


# ── Tier definitions (backtest universe) ──

TIERS: dict[int, list[str]] = {
    0: ["SPY", "QQQ", "IWM"],
    1: ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META"],
    2: ["TSLA", "AMD", "AVGO", "COIN"],
    3: ["MSTR", "DELL", "INTC", "PLTR", "MU"],
}


def all_tier_tickers() -> list[tuple[int, str]]:
    out = []
    for tier, syms in TIERS.items():
        for s in syms:
            out.append((tier, s))
    return out
