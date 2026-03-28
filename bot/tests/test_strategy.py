"""
Unit tests for ICT strategy logic.
Uses synthetic bar sequences to verify:
- Raid + displacement + iFVG → LONG_iFVG signal
- Raid + displacement + OB touch → LONG_OB signal
- Raid without reclaim → no signal
"""
import pytest
import pandas as pd
import pytz

from bot.strategy.ict_long import ICTLongStrategy, _is_displacement_candle, _detect_fvgs
from bot.strategy.levels import Level

UTC = pytz.utc
PT  = pytz.timezone("America/Los_Angeles")


def _ts(hour: int, minute: int, day: int = 15) -> pd.Timestamp:
    """Create a UTC timestamp during the NY morning session (07:00 PT = 15:00 UTC)."""
    return pd.Timestamp(f"2024-01-{day:02d} {hour:02d}:{minute:02d}:00", tz=UTC)


def _make_df(rows: list[dict]) -> pd.DataFrame:
    """
    Build a bar DataFrame from a list of dicts with keys:
    ts, open, high, low, close, volume
    """
    records = []
    idx = []
    for r in rows:
        idx.append(r["ts"])
        records.append({
            "open":   r.get("open",  r["close"]),
            "high":   r.get("high",  r["close"] + 0.10),
            "low":    r.get("low",   r["close"] - 0.10),
            "close":  r["close"],
            "volume": r.get("volume", 1000),
        })
    df = pd.DataFrame(records, index=pd.DatetimeIndex(idx, name="timestamp"))
    return df


class MockStrategy(ICTLongStrategy):
    """Override level detection to use a fixed injected level."""
    def __init__(self, df_1m, fixed_level: Level):
        super().__init__(df_1m)
        self._fixed_level = fixed_level

    def _get_lows(self, bar_time):
        return [self._fixed_level]


# Patch get_significant_lows in the strategy module for testing
import bot.strategy.ict_long as ict_module


def _run_scenario(df_5m: pd.DataFrame, sig_low: float, level_name: str = "PDL") -> list:
    """Run the strategy engine with a fixed level injected."""
    level = Level(name=level_name, price=sig_low, level_type="low")

    # Patch module-level function
    original = ict_module.get_significant_lows
    ict_module.get_significant_lows = lambda df, ts: [level]

    try:
        engine = ICTLongStrategy(df_1m=df_5m)
        signals = []
        for i in range(len(df_5m)):
            signals.extend(engine.process_bar(df_5m, i))
    finally:
        ict_module.get_significant_lows = original

    return signals


# ── Trade window: 07:00-09:00 PT = 15:00-17:00 UTC on 2024-01-15 ────────────

def test_ifvg_signal_generated():
    """
    Scenario: Raid low → displacement reclaim → FVG forms → price returns and closes above mid.
    Expected: LONG_iFVG signal emitted.
    """
    sig_low = 400.0

    # Build 25 "normal" bars — small but non-zero bodies so median_body > 0
    normal = [{"ts": _ts(14, i), "open": 400.95, "high": 401.2, "low": 400.8, "close": 401.00}
              for i in range(25)]

    # Bar 25: Raid — low goes below sig_low by more than RAID_THRESHOLD (0.05)
    raid_bar = {"ts": _ts(15, 1), "open": 400.5, "high": 400.5, "low": 399.9, "close": 400.2}

    # Bar 26: Displacement — big bullish candle (body=1.4, ratio=28x) closes above sig_low (400.0)
    disp_bar = {"ts": _ts(15, 6), "open": 400.0, "high": 401.5, "low": 399.95, "close": 401.4}

    # Bar 27: Creates FVG — low[27] > high[25]
    # high[25] = 400.5, so low[27] must be > 400.5
    fvg_bar = {"ts": _ts(15, 11), "open": 401.0, "high": 402.5, "low": 400.6, "close": 402.4}

    # Bar 28: Price trades into FVG zone [400.5, 400.6] and closes above mid (400.55)
    # but we need a realistic scenario: price dips into FVG then closes above midpoint
    entry_bar = {"ts": _ts(15, 16), "open": 401.5, "high": 401.6, "low": 400.52, "close": 400.58}

    rows = normal + [raid_bar, disp_bar, fvg_bar, entry_bar]
    df = _make_df(rows)

    signals = _run_scenario(df, sig_low=sig_low)
    ifvg_signals = [s for s in signals if s.signal_type == "LONG_iFVG"]
    assert len(ifvg_signals) >= 1, f"Expected LONG_iFVG signal, got: {signals}"


def test_ob_signal_generated():
    """
    Scenario: Raid low → displacement reclaim → price touches OB.
    Expected: LONG_OB signal emitted.
    """
    sig_low = 400.0
    normal = [{"ts": _ts(14, i), "open": 400.95, "high": 401.2, "low": 400.8, "close": 401.00}
              for i in range(25)]

    raid_bar = {"ts": _ts(15, 1), "open": 400.5, "high": 400.5, "low": 399.9, "close": 400.2}

    # Bearish bar just before displacement (forms OB): open > close, so bearish
    ob_bar = {"ts": _ts(15, 6), "open": 401.0, "high": 401.0, "low": 400.2, "close": 400.3,
              "volume": 1000}

    # Displacement: bullish, big body (1.5), closes above sig_low. N_CONFIRM_BARS=2: raid@25, ob@26, disp@27 → ok
    disp_bar = {"ts": _ts(15, 11), "open": 400.3, "high": 402.0, "low": 400.25, "close": 401.8}

    # Price touches OB zone (ob_low <= 400.2, ob_high <= 401.0 [highest open of bearish cluster])
    touch_bar = {"ts": _ts(15, 16), "open": 401.5, "high": 401.5, "low": 400.25, "close": 401.3}

    rows = normal + [raid_bar, ob_bar, disp_bar, touch_bar]
    df = _make_df(rows)

    signals = _run_scenario(df, sig_low=sig_low)
    ob_signals = [s for s in signals if s.signal_type == "LONG_OB"]
    assert len(ob_signals) >= 1, f"Expected LONG_OB signal, got: {signals}"


def test_no_signal_without_displacement_reclaim():
    """
    Scenario: Raid occurs but no displacement candle reclaims the level.
    Expected: No signal.
    """
    sig_low = 400.0
    normal = [{"ts": _ts(14, i), "open": 401.0, "high": 401.2, "low": 400.8, "close": 401.0}
              for i in range(25)]

    raid_bar = {"ts": _ts(15, 1), "open": 400.5, "high": 400.5, "low": 399.9, "close": 400.2}

    # Small recovery — does NOT close above sig_low 400.0
    weak_bar = {"ts": _ts(15, 6), "open": 399.9, "high": 399.95, "low": 399.8, "close": 399.92}

    rows = normal + [raid_bar, weak_bar]
    df = _make_df(rows)

    signals = _run_scenario(df, sig_low=sig_low)
    assert len(signals) == 0, f"Expected no signals, got: {signals}"


def test_displacement_candle_detection():
    """Unit test for displacement candle logic."""
    # Build 20 small-body bars, then 1 big bullish bar
    rows = [{"ts": _ts(14, i), "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.05}
            for i in range(20)]
    rows.append({"ts": _ts(15, 1), "open": 100.0, "high": 101.5, "low": 99.95, "close": 101.4})
    df = _make_df(rows)

    is_disp, ratio = _is_displacement_candle(df.iloc[20], df, 20)
    assert is_disp, f"Should be displacement, ratio={ratio}"
    assert ratio > 1.2


def test_fvg_detection():
    """Unit test for FVG detection: low[i] > high[i-2]."""
    rows = [
        {"ts": _ts(15, 0),  "open": 100.0, "high": 100.5, "low": 99.8,  "close": 100.3},  # i-2
        {"ts": _ts(15, 5),  "open": 100.3, "high": 100.8, "low": 100.2, "close": 100.7},  # i-1
        {"ts": _ts(15, 10), "open": 100.7, "high": 101.5, "low": 100.7, "close": 101.4},  # i: low=100.7 > high[i-2]=100.5 ✓
    ]
    df = _make_df(rows)
    fvgs = _detect_fvgs(df, after_idx=0)
    assert len(fvgs) == 1
    assert abs(fvgs[0].lower - 100.5) < 0.001
    assert abs(fvgs[0].upper - 100.7) < 0.001
