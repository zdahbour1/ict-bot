"""
ICT Short Strategy Engine — mirror of ict_long.py for bearish setups.

Implements:
  - Raid detection (price sweeps ABOVE significant high)
  - Raid confirmation (bearish displacement candle closes back below)
  - Signal Type A: Bearish iFVG midpoint close entry
  - Signal Type B: Bearish OB touch entry
  - SL: raid_high + buffer
  - TP: nearest swing low (fractal 2-left-2-right)
"""
import logging
from typing import List, Dict, Optional
import pandas as pd

log = logging.getLogger(__name__)

# ── Default parameters (mirrored from ict_long.py) ──────
RAID_THRESHOLD      = 0.05    # min price penetration above high to qualify as a raid
BODY_MULT           = 1.2     # displacement candle body must be >= this × median
LOOKBACK            = 20      # bars back to compute median body
BOT_FRAC            = 0.5     # close must be in bottom 50% of bar range
N_CONFIRM_BARS      = 2       # max bars after raid to get displacement reclaim
FVG_MIN_SIZE        = 0.10    # minimum FVG size in dollars
OB_MAX_CANDLES      = 3       # max bullish candles before displacement = OB
SL_BUFFER           = 0.05    # buffer ABOVE raid high for stop loss
TP_LOOKBACK         = 40      # bars back to find swing low for TP


# ── Bearish displacement candle ──────────────────────────
def is_bearish_displacement_candle(bars: pd.DataFrame, i: int) -> bool:
    """
    Returns True if bar at index i is a bearish displacement candle:
    1. Close < Open (bearish)
    2. Body size >= BODY_MULT × median body of prior LOOKBACK bars
    3. Close in bottom BOT_FRAC of bar's range
    """
    if i < LOOKBACK:
        return False

    bar = bars.iloc[i]
    body = bar["open"] - bar["close"]
    if body <= 0:
        return False  # Must be bearish

    prior = bars.iloc[i - LOOKBACK:i]
    median_body = (prior["close"] - prior["open"]).abs().median()
    if median_body == 0:
        return False

    if body < BODY_MULT * median_body:
        return False

    bar_range = bar["high"] - bar["low"]
    if bar_range == 0:
        return False
    close_position = (bar["close"] - bar["low"]) / bar_range
    if close_position > BOT_FRAC:
        return False

    return True


# ── Short Raid detection ─────────────────────────────────
def find_raids_short(bars_5m: pd.DataFrame, levels: List[Dict]) -> List[Dict]:
    """
    Scan bars for raid-high events.
    A raid occurs when price trades ABOVE a significant high by >= RAID_THRESHOLD.
    """
    raids = []
    highs_only = [l for l in levels if "HIGH" in l["label"] or "PDH" in l["label"]]

    for i in range(LOOKBACK, len(bars_5m)):
        bar = bars_5m.iloc[i]
        for level in highs_only:
            ref_high = level["price"]
            if bar["high"] >= ref_high + RAID_THRESHOLD:
                raids.append({
                    "bar_idx":      i,
                    "bar_time":     bars_5m.index[i],
                    "raided_level": level["label"],
                    "raided_price": ref_high,
                    "raid_high":    float(bar["high"]),
                })
    return raids


# ── Short raid confirmation: bearish displacement ────────
def confirm_raid_short(bars_5m: pd.DataFrame, raid: Dict) -> Optional[Dict]:
    """
    After a raid, look for a bearish displacement candle that
    closes back BELOW the raided level within N_CONFIRM_BARS.
    """
    start = raid["bar_idx"] + 1
    end   = min(start + N_CONFIRM_BARS, len(bars_5m))

    for i in range(start, end):
        bar = bars_5m.iloc[i]
        if bar["close"] < raid["raided_price"]:
            if is_bearish_displacement_candle(bars_5m, i):
                return {
                    "disp_idx":   i,
                    "disp_time":  bars_5m.index[i],
                    "disp_close": float(bar["close"]),
                }
    return None


# ── Bearish FVG detection ────────────────────────────────
def find_bearish_fvg(bars_5m: pd.DataFrame, disp_idx: int) -> Optional[Dict]:
    """
    Find a bearish FVG formed at or after the displacement candle.
    Bearish FVG at bar i: high[i] < low[i-2]
    Gap zone = [high[i], low[i-2]]
    """
    search_end = min(disp_idx + 10, len(bars_5m))
    for i in range(max(disp_idx, 2), search_end):
        high_i  = bars_5m.iloc[i]["high"]
        low_im2 = bars_5m.iloc[i - 2]["low"]
        if high_i < low_im2:
            gap_size = low_im2 - high_i
            if gap_size >= FVG_MIN_SIZE:
                return {
                    "fvg_lower":   float(high_i),
                    "fvg_upper":   float(low_im2),
                    "fvg_mid":     float((high_i + low_im2) / 2),
                    "fvg_bar_idx": i,
                    "fvg_size":    float(gap_size),
                }
    return None


# ── Bearish iFVG entry check ─────────────────────────────
def check_bearish_ifvg_entry(bars_5m: pd.DataFrame, fvg: Dict, search_from: int) -> Optional[Dict]:
    """
    After bearish FVG forms, wait for price to return INTO the FVG zone
    and a candle to close BELOW the FVG midpoint.
    """
    search_end = min(search_from + 20, len(bars_5m))
    for i in range(search_from, search_end):
        bar = bars_5m.iloc[i]
        if bar["high"] >= fvg["fvg_lower"] and bar["low"] <= fvg["fvg_upper"]:
            if bar["close"] < fvg["fvg_mid"]:
                return {
                    "entry_price": float(bar["close"]),
                    "entry_bar":   i,
                    "entry_time":  bars_5m.index[i],
                    "signal_type": "SHORT_iFVG",
                }
    return None


# ── Bearish OB detection ─────────────────────────────────
def find_bearish_ob(bars_5m: pd.DataFrame, disp_idx: int) -> Optional[Dict]:
    """
    Bearish Order Block: the last 1-3 BULLISH candles immediately before displacement.
    OB zone = [lowest open, highest high] of those candles.
    """
    ob_candles = []
    for i in range(disp_idx - 1, max(disp_idx - 1 - OB_MAX_CANDLES, -1), -1):
        bar = bars_5m.iloc[i]
        if bar["close"] > bar["open"]:  # bullish
            ob_candles.append(bar)
        else:
            break

    if not ob_candles:
        return None

    ob_df = pd.DataFrame(ob_candles)
    return {
        "ob_low":  float(ob_df["open"].min()),
        "ob_high": float(ob_df["high"].max()),
    }


# ── Bearish OB entry check ───────────────────────────────
def check_bearish_ob_entry(bars_5m: pd.DataFrame, ob: Dict, search_from: int) -> Optional[Dict]:
    """
    Price touches (overlaps) the bearish OB zone → short entry signal.
    """
    search_end = min(search_from + 20, len(bars_5m))
    for i in range(search_from, search_end):
        bar = bars_5m.iloc[i]
        if bar["high"] >= ob["ob_low"] and bar["low"] <= ob["ob_high"]:
            return {
                "entry_price": float(bar["close"]),
                "entry_bar":   i,
                "entry_time":  bars_5m.index[i],
                "signal_type": "SHORT_OB",
            }
    return None


# ── Stop Loss (short) ────────────────────────────────────
def compute_sl_short(raid: Dict, fixed_risk_dollars: float = 200.0,
                     shares: int = 1) -> float:
    """
    SL = raid_high + buffer (above the wick that swept the high)
    """
    technical_sl = raid["raid_high"] + SL_BUFFER
    fixed_sl     = raid["raided_price"] + (fixed_risk_dollars / shares)
    return min(technical_sl, fixed_sl)  # lower price = tighter stop for short


# ── Take Profit: nearest swing low ──────────────────────
def compute_tp_short(bars_5m: pd.DataFrame, entry_bar: int) -> float:
    """
    Nearest swing LOW using 2-left-2-right fractal below entry.
    Falls back to lowest low in TP_LOOKBACK bars.
    """
    lows = bars_5m["low"]
    entry_price = bars_5m.iloc[entry_bar]["close"]

    swing_lows = []
    start = max(2, entry_bar - TP_LOOKBACK)
    end   = min(entry_bar + 1, len(bars_5m) - 2)
    for i in range(start, end):
        l = lows.iloc[i]
        if (l < lows.iloc[i-1] and l < lows.iloc[i-2] and
                l < lows.iloc[i+1] and l < lows.iloc[i+2]):
            if l < entry_price:
                swing_lows.append(l)

    if swing_lows:
        return float(max(swing_lows))  # nearest (highest) swing low below entry

    lookback_bars = bars_5m.iloc[max(0, entry_bar - TP_LOOKBACK):entry_bar]
    return float(lookback_bars["low"].min())


# ── Main short strategy runner ───────────────────────────
def run_strategy_short(bars_5m: pd.DataFrame,
                       bars_1h: pd.DataFrame,
                       bars_4h: pd.DataFrame,
                       levels: List[Dict],
                       alerts_today: int = 0,
                       max_alerts: int = 10) -> List[Dict]:
    """
    Full ICT short strategy scan.
    Returns list of signal dicts with entry/SL/TP.
    """
    signals = []
    seen_setups = set()

    if bars_5m.empty or len(bars_5m) < LOOKBACK + 5:
        log.warning("Not enough bars to run short strategy.")
        return signals

    raids = find_raids_short(bars_5m, levels)
    log.info(f"Found {len(raids)} short raid events to evaluate.")

    for raid in raids:
        if alerts_today + len(signals) >= max_alerts:
            log.info("Max alerts per day reached. Stopping short scan.")
            break

        confirmation = confirm_raid_short(bars_5m, raid)
        if not confirmation:
            continue

        disp_idx = confirmation["disp_idx"]

        # ── Signal Type A: Bearish iFVG ──────────────────
        fvg = find_bearish_fvg(bars_5m, disp_idx)
        if fvg:
            entry = check_bearish_ifvg_entry(bars_5m, fvg, fvg["fvg_bar_idx"] + 1)
            if entry:
                setup_id = "SHORT_iFVG_" + raid["raided_level"] + "_" + str(raid["bar_time"])
                if setup_id not in seen_setups:
                    seen_setups.add(setup_id)
                    sl = compute_sl_short(raid)
                    tp = compute_tp_short(bars_5m, entry["entry_bar"])
                    signal = {
                        **entry,
                        "sl":           sl,
                        "tp":           tp,
                        "raid":         raid,
                        "confirmation": confirmation,
                        "fvg":          fvg,
                        "setup_id":     setup_id,
                        "direction":    "SHORT",
                    }
                    signals.append(signal)
                    log.info(
                        f"SHORT SIGNAL {entry['signal_type']} | "
                        f"Entry={entry['entry_price']:.2f} "
                        f"SL={sl:.2f} TP={tp:.2f} | "
                        f"Raided: {raid['raided_level']}"
                    )

        # ── Signal Type B: Bearish OB ────────────────────
        ob = find_bearish_ob(bars_5m, disp_idx)
        if ob:
            entry = check_bearish_ob_entry(bars_5m, ob, disp_idx + 1)
            if entry:
                setup_id = "SHORT_OB_" + raid["raided_level"] + "_" + str(raid["bar_time"])
                if setup_id not in seen_setups:
                    seen_setups.add(setup_id)
                    sl = compute_sl_short(raid)
                    tp = compute_tp_short(bars_5m, entry["entry_bar"])
                    signal = {
                        **entry,
                        "sl":           sl,
                        "tp":           tp,
                        "raid":         raid,
                        "confirmation": confirmation,
                        "ob":           ob,
                        "setup_id":     setup_id,
                        "direction":    "SHORT",
                    }
                    signals.append(signal)
                    log.info(
                        f"SHORT SIGNAL {entry['signal_type']} | "
                        f"Entry={entry['entry_price']:.2f} "
                        f"SL={sl:.2f} TP={tp:.2f} | "
                        f"Raided: {raid['raided_level']}"
                    )

    return signals
