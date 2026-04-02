"""
ICT Long Strategy Engine — full implementation per PDF spec.

Implements:
  - Raid detection (price sweeps below significant low)
  - Raid confirmation (displacement candle closes back above)
  - Signal Type A: iFVG mid-close entry
  - Signal Type B: OB touch entry
  - SL: min(fixed risk stop, raid_low - buffer)
  - TP: nearest swing high (fractal 2-left-2-right)
"""
import logging
from typing import List, Dict, Optional
import pandas as pd
import numpy as np

log = logging.getLogger(__name__)

# ── Default parameters (from PDF) ───────────────────────
RAID_THRESHOLD      = 0.05    # min price penetration to qualify as a raid
BODY_MULT           = 1.2     # displacement candle body must be >= this × median
LOOKBACK            = 20      # bars back to compute median body
TOP_FRAC            = 0.5     # close must be in top 50% of bar range
N_CONFIRM_BARS      = 2       # max bars after raid to get displacement reclaim
FVG_MIN_SIZE        = 0.10    # minimum FVG size in dollars
OB_MAX_CANDLES      = 3       # max bearish candles before displacement = OB
SL_BUFFER           = 0.05    # buffer below raid low for stop loss
TP_LOOKBACK         = 40      # bars back to find swing high for TP
MAX_ALERTS_PER_DAY  = 999     # no practical limit


# ── Displacement candle detection ────────────────────────
def is_displacement_candle(bars: pd.DataFrame, i: int) -> bool:
    """
    Returns True if bar at index i is a bullish displacement candle:
    1. Close > Open (bullish)
    2. Body size >= BODY_MULT × median body of prior LOOKBACK bars
    3. Close in top TOP_FRAC of bar's range
    """
    if i < LOOKBACK:
        return False

    bar = bars.iloc[i]
    body = bar["close"] - bar["open"]
    if body <= 0:
        return False  # Must be bullish

    # Median body of prior bars
    prior = bars.iloc[i - LOOKBACK:i]
    median_body = (prior["close"] - prior["open"]).abs().median()
    if median_body == 0:
        return False

    if body < BODY_MULT * median_body:
        return False

    # Close must be in top TOP_FRAC of range
    bar_range = bar["high"] - bar["low"]
    if bar_range == 0:
        return False
    close_position = (bar["close"] - bar["low"]) / bar_range
    if close_position < (1 - TOP_FRAC):
        return False

    return True


# ── Raid detection ───────────────────────────────────────
def find_raids(bars_5m: pd.DataFrame, levels: List[Dict]) -> List[Dict]:
    """
    Scan bars for raid-low events.
    A raid occurs when price trades below a significant low by >= RAID_THRESHOLD.
    Returns list of raid events.
    """
    raids = []
    lows_only = [l for l in levels if "LOW" in l["label"] or "PDL" in l["label"]]

    for i in range(LOOKBACK, len(bars_5m)):
        bar = bars_5m.iloc[i]
        for level in lows_only:
            ref_low = level["price"]
            # Price trades below the level by at least RAID_THRESHOLD
            if bar["low"] <= ref_low - RAID_THRESHOLD:
                raids.append({
                    "bar_idx":      i,
                    "bar_time":     bars_5m.index[i],
                    "raided_level": level["label"],
                    "raided_price": ref_low,
                    "raid_low":     float(bar["low"]),
                })
    return raids


# ── Raid confirmation: displacement reclaim ──────────────
def confirm_raid(bars_5m: pd.DataFrame, raid: Dict) -> Optional[Dict]:
    """
    After a raid, look for a bullish displacement candle that
    closes back ABOVE the raided level within N_CONFIRM_BARS.
    Returns the confirmation bar info, or None if not confirmed.
    """
    start = raid["bar_idx"] + 1
    end   = min(start + N_CONFIRM_BARS, len(bars_5m))

    for i in range(start, end):
        bar = bars_5m.iloc[i]
        # Must close above the raided level
        if bar["close"] > raid["raided_price"]:
            if is_displacement_candle(bars_5m, i):
                return {
                    "disp_idx":  i,
                    "disp_time": bars_5m.index[i],
                    "disp_close": float(bar["close"]),
                }
    return None


# ── FVG detection ────────────────────────────────────────
def find_fvg_after_displacement(bars_5m: pd.DataFrame, disp_idx: int) -> Optional[Dict]:
    """
    Find a bullish FVG formed at or after the displacement candle.
    Bullish FVG at bar i: low[i] > high[i-2]
    Gap zone = [high[i-2], low[i]]
    """
    search_end = min(disp_idx + 10, len(bars_5m))
    for i in range(max(disp_idx, 2), search_end):
        low_i    = bars_5m.iloc[i]["low"]
        high_im2 = bars_5m.iloc[i - 2]["high"]
        if low_i > high_im2:
            gap_size = low_i - high_im2
            if gap_size >= FVG_MIN_SIZE:
                return {
                    "fvg_lower":   float(high_im2),
                    "fvg_upper":   float(low_i),
                    "fvg_mid":     float((high_im2 + low_i) / 2),
                    "fvg_bar_idx": i,
                    "fvg_size":    float(gap_size),
                }
    return None


# ── iFVG entry check ─────────────────────────────────────
def check_ifvg_entry(bars_5m: pd.DataFrame, fvg: Dict, search_from: int) -> Optional[Dict]:
    """
    After FVG forms, wait for price to return into the FVG zone
    and a candle to close above the FVG midpoint.
    """
    search_end = min(search_from + 20, len(bars_5m))
    for i in range(search_from, search_end):
        bar = bars_5m.iloc[i]
        # Price enters FVG zone
        if bar["low"] <= fvg["fvg_upper"] and bar["high"] >= fvg["fvg_lower"]:
            # Close above midpoint
            if bar["close"] > fvg["fvg_mid"]:
                return {
                    "entry_price": float(bar["close"]),
                    "entry_bar":   i,
                    "entry_time":  bars_5m.index[i],
                    "signal_type": "LONG_iFVG",
                }
    return None


# ── OB detection ─────────────────────────────────────────
def find_ob(bars_5m: pd.DataFrame, disp_idx: int) -> Optional[Dict]:
    """
    Order Block: the last 1-3 bearish candles immediately before the displacement.
    OB zone = [lowest low, highest open] of those candles.
    """
    ob_candles = []
    for i in range(disp_idx - 1, max(disp_idx - 1 - OB_MAX_CANDLES, -1), -1):
        bar = bars_5m.iloc[i]
        if bar["close"] < bar["open"]:  # bearish
            ob_candles.append(bar)
        else:
            break  # stop at first non-bearish candle

    if not ob_candles:
        return None

    ob_df = pd.DataFrame(ob_candles)
    return {
        "ob_low":  float(ob_df["low"].min()),
        "ob_high": float(ob_df["open"].max()),
    }


# ── OB entry check ───────────────────────────────────────
def check_ob_entry(bars_5m: pd.DataFrame, ob: Dict, search_from: int) -> Optional[Dict]:
    """
    Price touches (overlaps) the OB zone → entry signal.
    """
    search_end = min(search_from + 20, len(bars_5m))
    for i in range(search_from, search_end):
        bar = bars_5m.iloc[i]
        if bar["high"] >= ob["ob_low"] and bar["low"] <= ob["ob_high"]:
            return {
                "entry_price": float(bar["close"]),
                "entry_bar":   i,
                "entry_time":  bars_5m.index[i],
                "signal_type": "LONG_OB",
            }
    return None


# ── Stop Loss computation ────────────────────────────────
def compute_sl(raid: Dict, fixed_risk_dollars: float = 200.0,
               shares: int = 1) -> float:
    """
    SL = min(fixed risk stop, raid_low - buffer)
    """
    technical_sl = raid["raid_low"] - SL_BUFFER
    fixed_sl     = raid["raided_price"] - (fixed_risk_dollars / shares)
    return max(technical_sl, fixed_sl)   # higher price = tighter stop


# ── Take Profit: nearest swing high ─────────────────────
def compute_tp(bars_5m: pd.DataFrame, entry_bar: int) -> float:
    """
    Nearest swing high using 2-left-2-right fractal.
    Falls back to highest high in TP_LOOKBACK bars.
    """
    highs = bars_5m["high"]
    entry_price = bars_5m.iloc[entry_bar]["close"]

    # Fractal swing high: high[i] > high[i-1], high[i-2], high[i+1], high[i+2]
    swing_highs = []
    start = max(2, entry_bar - TP_LOOKBACK)
    end   = min(entry_bar + 1, len(bars_5m) - 2)
    for i in range(start, end):
        h = highs.iloc[i]
        if (h > highs.iloc[i-1] and h > highs.iloc[i-2] and
                h > highs.iloc[i+1] and h > highs.iloc[i+2]):
            if h > entry_price:
                swing_highs.append(h)

    if swing_highs:
        return float(min(swing_highs))  # nearest (lowest) swing high above entry

    # Fallback: highest high in lookback window
    lookback_bars = bars_5m.iloc[max(0, entry_bar - TP_LOOKBACK):entry_bar]
    fallback = lookback_bars["high"].max()
    if pd.isna(fallback):
        fallback = bars_5m.iloc[entry_bar]["close"] * 1.02  # 2% above entry
        log.warning(f"compute_tp: no swing high found, using 2% above entry: {fallback:.2f}")
    return float(fallback)


# ── Main strategy runner ─────────────────────────────────
def run_strategy(bars_5m: pd.DataFrame,
                 bars_1h: pd.DataFrame,
                 bars_4h: pd.DataFrame,
                 levels: List[Dict],
                 alerts_today: int = 0) -> List[Dict]:
    """
    Full ICT long strategy scan.
    Returns list of signal dicts with entry/SL/TP.
    """
    signals = []
    seen_setups = set()   # dedup

    if bars_5m.empty or len(bars_5m) < LOOKBACK + 5:
        log.warning("Not enough bars to run strategy.")
        return signals

    # Find all raids
    raids = find_raids(bars_5m, levels)
    log.info(f"Found {len(raids)} raid events to evaluate.")

    for raid in raids:
        if alerts_today + len(signals) >= MAX_ALERTS_PER_DAY:
            log.info("Max alerts per day reached. Stopping scan.")
            break

        # Confirm raid with displacement reclaim
        confirmation = confirm_raid(bars_5m, raid)
        if not confirmation:
            continue

        disp_idx = confirmation["disp_idx"]

        # ── Signal Type A: iFVG ──────────────────────────
        fvg = find_fvg_after_displacement(bars_5m, disp_idx)
        if fvg:
            entry = check_ifvg_entry(bars_5m, fvg, fvg["fvg_bar_idx"] + 1)
            if entry:
                setup_id = f"iFVG_{raid['raided_level']}_{raid['bar_time']}"
                if setup_id not in seen_setups:
                    seen_setups.add(setup_id)
                    sl = compute_sl(raid)
                    tp = compute_tp(bars_5m, entry["entry_bar"])
                    signal = {
                        **entry,
                        "sl":            sl,
                        "tp":            tp,
                        "raid":          raid,
                        "confirmation":  confirmation,
                        "fvg":           fvg,
                        "setup_id":      setup_id,
                    }
                    signals.append(signal)
                    log.info(f"SIGNAL {entry['signal_type']} | "
                             f"Entry={entry['entry_price']:.2f} "
                             f"SL={sl:.2f} TP={tp:.2f} | "
                             f"Raided: {raid['raided_level']}")

        # ── Signal Type B: OB Touch ──────────────────────
        ob = find_ob(bars_5m, disp_idx)
        if ob:
            entry = check_ob_entry(bars_5m, ob, disp_idx + 1)
            if entry:
                setup_id = f"OB_{raid['raided_level']}_{raid['bar_time']}"
                if setup_id not in seen_setups:
                    seen_setups.add(setup_id)
                    sl = compute_sl(raid)
                    tp = compute_tp(bars_5m, entry["entry_bar"])
                    signal = {
                        **entry,
                        "sl":            sl,
                        "tp":            tp,
                        "raid":          raid,
                        "confirmation":  confirmation,
                        "ob":            ob,
                        "setup_id":      setup_id,
                    }
                    signals.append(signal)
                    log.info(f"SIGNAL {entry['signal_type']} | "
                             f"Entry={entry['entry_price']:.2f} "
                             f"SL={sl:.2f} TP={tp:.2f} | "
                             f"Raided: {raid['raided_level']}")

    return signals
