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
    stop_loss_pct: float = 0.0    # 0 = disabled; typical 0.25 (close at 25% loss of credit)

    # ── ZDN-series extensions (2026-04-24, ZDN = zero-delta-neutral) ──
    # Structural:
    structure: str = "iron_condor"          # or 'iron_butterfly' (shorts both ATM)
    # Expiry override — when set, bypasses target_dte and selects by
    # explicit rule. Options:
    #   'target_dte' (default; uses target_dte window)
    #   '0dte'       — today's expiry if listed, else nearest weekly
    #   'weekly'     — next Friday
    #   'monthly'    — 3rd Friday this month (or next if past)
    #   'next_month' — 3rd Friday of next calendar month
    expiry_mode: str = "target_dte"
    # Time gates (America/New_York). None = no gate.
    entry_time_et: str | None = None        # e.g., "10:00" = don't enter before 10am ET
    exit_before_close_min: int = 0          # e.g., 15 = close 15 min before 16:00 ET
    # Liquidity floor for leg selection. 0 = disabled.
    min_option_volume: int = 0
    # Tight delta hedging (per-variant override). When > 0, overrides the
    # global DN_DELTA_BAND_SHARES from settings for THIS variant's trades.
    # ZDN uses a tight band (10 shares) for aggressive gamma-scalping.
    # 0 = use the global setting (current V5/V5b behavior).
    hedge_delta_band_shares: int = 0


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


# ── ZDN-series (zero-delta-neutral gamma-scalping) 2026-04-24 ──
#
# User hypothesis: open an ATM iron butterfly after the morning chop
# settles, then aggressively hedge delta with stock (tight ±10-share
# band, 30s rebalance). Structure is short call + short put both
# at-the-money (same strike = classic butterfly). Wings keep the
# structure defined-risk.
#
# Four variants — same logic, different expiry:
#   ZDN_0DTE       — same-day expiry (0 DTE) — maximum theta, max gamma
#   ZDN_WEEKLY     — next Friday weekly — balanced
#   ZDN_MONTHLY    — 3rd Friday this month — slower bleed
#   ZDN_NEXT_MONTH — 3rd Friday next month — lowest gamma, less hedging
#
# All ZDN variants share:
#   - Entry: 10:00 ET earliest (skip open chop)
#   - Close: 15 min before market close (3:45 ET)
#   - TP: 50% of credit, SL: 25% of credit
#   - Hedge: ±10 shares, every 30s
#   - Liquidity floor on leg selection

_ZDN_COMMON = dict(
    structure="iron_butterfly",
    strike_mode="atm_plus_wing",
    wing_width_dollars=5.0,           # narrow wings = high theta, high gamma
    entry_time_et="10:00",
    exit_before_close_min=15,
    profit_target_pct=0.50,
    stop_loss_pct=0.25,
    delta_hedge=True,
    hedge_delta_band_shares=10,        # tight — 10 shares ≈ 0.1 delta per contract
    sizing_mode="flat",
    base_contracts=1,
    # Modest IV gate so we only trade when premium is meaningful
    ivr_min=0.0,
    regime_filter=False,
    event_blackout=True,
    # Liquidity floor: skip contracts with volume < 100 (tunable via settings)
    min_option_volume=100,
)

ZDN_0DTE = DNVariant(
    name="zdn_0dte", label="ZDN-0",
    **_ZDN_COMMON,
    expiry_mode="0dte",
    target_dte=0, min_dte=0, max_dte=1,
)

ZDN_WEEKLY = DNVariant(
    name="zdn_weekly", label="ZDN-W",
    **_ZDN_COMMON,
    expiry_mode="weekly",
    target_dte=5, min_dte=1, max_dte=10,
)

ZDN_MONTHLY = DNVariant(
    name="zdn_monthly", label="ZDN-M",
    **_ZDN_COMMON,
    expiry_mode="monthly",
    target_dte=20, min_dte=10, max_dte=45,
)

ZDN_NEXT_MONTH = DNVariant(
    name="zdn_next_month", label="ZDN-N",
    **_ZDN_COMMON,
    expiry_mode="next_month",
    target_dte=45, min_dte=30, max_dte=60,
)


VARIANTS: list[DNVariant] = [
    V1_BASELINE, V2_HOLD_DAY, V3_PHASEB, V4_FILTERED,
    V5_HEDGED, V5B_SWEEP_WINNER,
    ZDN_0DTE, ZDN_WEEKLY, ZDN_MONTHLY, ZDN_NEXT_MONTH,
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
