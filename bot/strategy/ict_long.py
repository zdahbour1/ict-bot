"""
ICT Long-Only Strategy Engine (v1).
Implements: Raid → Displacement Reclaim → iFVG mid close OR OB touch.
Operates on 5m execution bars. Uses 1m for context.
All timestamps are UTC internally.
"""
import uuid
from dataclasses import dataclass, field
from typing import Optional
import numpy as np
import pandas as pd
import pytz
from loguru import logger

from bot import config
from bot.strategy.levels import Level, get_significant_lows


PT = pytz.timezone("America/Los_Angeles")


# ─────────────────────────────────────────────────────────────────────────────
# Data Structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FVG:
    bar_time: pd.Timestamp   # timestamp of bar i (the 3rd candle)
    lower: float             # high[i-2]
    upper: float             # low[i]
    midpoint: float          # (lower + upper) / 2
    filled: bool = False


@dataclass
class OrderBlock:
    bar_time: pd.Timestamp   # timestamp of displacement candle
    ob_low: float            # lowest low of bearish cluster
    ob_high: float           # highest open of bearish cluster


@dataclass
class Signal:
    signal_id: str
    signal_type: str         # "LONG_iFVG" or "LONG_OB"
    bar_time: pd.Timestamp   # 5m bar that triggered
    entry: float
    sl: float
    tp: float
    raided_level: Level
    raid_low: float
    displacement_time: pd.Timestamp
    displacement_ratio: float
    # iFVG specific
    fvg: Optional[FVG] = None
    # OB specific
    ob: Optional[OrderBlock] = None
    reasoning: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_in_trade_window(ts: pd.Timestamp) -> bool:
    """Return True if ts (UTC) falls within configured PT trade window."""
    ts_pt = ts.tz_convert(PT)
    start_h, start_m = [int(x) for x in config.TRADE_WINDOW_START_PT.split(":")]
    end_h,   end_m   = [int(x) for x in config.TRADE_WINDOW_END_PT.split(":")]
    start_minutes = start_h * 60 + start_m
    end_minutes   = end_h   * 60 + end_m
    bar_minutes   = ts_pt.hour * 60 + ts_pt.minute
    return start_minutes <= bar_minutes < end_minutes


def _is_displacement_candle(bar: pd.Series, df: pd.DataFrame, idx: int) -> tuple[bool, float]:
    """
    Check if bar at position `idx` is a bullish displacement candle.
    Returns (is_displacement, body_ratio).
    """
    body = bar["close"] - bar["open"]
    if body <= 0:
        return False, 0.0

    lookback = config.DISPLACEMENT_LOOKBACK
    start = max(0, idx - lookback)
    prior_bars = df.iloc[start:idx]
    if prior_bars.empty:
        return False, 0.0

    prior_bodies = (prior_bars["close"] - prior_bars["open"]).abs()
    median_body = float(prior_bodies.median())
    if median_body == 0:
        return False, 0.0

    ratio = body / median_body
    if ratio < config.DISPLACEMENT_BODY_MULT:
        return False, ratio

    # Close in top TOP_FRAC of range
    top_frac = 0.5
    bar_range = bar["high"] - bar["low"]
    if bar_range == 0:
        return False, ratio
    close_position = (bar["close"] - bar["low"]) / bar_range
    if close_position < (1.0 - top_frac):
        return False, ratio

    return True, ratio


def _detect_fvgs(df_5m: pd.DataFrame, after_idx: int) -> list[FVG]:
    """
    Detect bullish FVGs in df_5m starting from bar after_idx.
    Bullish FVG: low[i] > high[i-2] and gap size >= FVG_MIN_SIZE.
    """
    fvgs = []
    for i in range(max(2, after_idx), len(df_5m)):
        low_i      = df_5m.iloc[i]["low"]
        high_i_min2 = df_5m.iloc[i - 2]["high"]
        if low_i > high_i_min2:
            size = low_i - high_i_min2
            if size >= config.FVG_MIN_SIZE:
                mid = (high_i_min2 + low_i) / 2
                fvgs.append(FVG(
                    bar_time=df_5m.index[i],
                    lower=high_i_min2,
                    upper=low_i,
                    midpoint=mid,
                ))
    return fvgs


def _detect_ob(df_5m: pd.DataFrame, disp_idx: int) -> Optional[OrderBlock]:
    """
    Order Block: up to MAX_OB_CANDLES consecutive bearish candles immediately before displacement.
    OB zone = [lowest low of cluster, highest open of cluster].
    """
    max_ob = config.OB_MAX_CANDLES
    ob_bars = []
    for j in range(disp_idx - 1, max(disp_idx - max_ob - 1, -1), -1):
        bar = df_5m.iloc[j]
        if bar["close"] < bar["open"]:  # bearish
            ob_bars.append(bar)
        else:
            break
    if not ob_bars:
        return None
    ob_low  = min(b["low"]  for b in ob_bars)
    ob_high = max(b["open"] for b in ob_bars)
    return OrderBlock(
        bar_time=df_5m.index[disp_idx],
        ob_low=ob_low,
        ob_high=ob_high,
    )


def _swing_high_tp(df_5m: pd.DataFrame, from_idx: int) -> Optional[float]:
    """
    Nearest swing high using 2-left-2-right fractal.
    Falls back to highest high in last TP_LOOKBACK bars.
    """
    # Fractal method: need at least 4 bars ahead
    for i in range(from_idx + 2, len(df_5m) - 2):
        h = df_5m.iloc[i]["high"]
        if (h > df_5m.iloc[i-1]["high"] and h > df_5m.iloc[i-2]["high"] and
                h > df_5m.iloc[i+1]["high"] and h > df_5m.iloc[i+2]["high"]):
            return h

    # Fallback: highest high in TP_LOOKBACK bars before from_idx
    start = max(0, from_idx - config.TP_LOOKBACK)
    window = df_5m.iloc[start:from_idx]
    if window.empty:
        return None
    return float(window["high"].max())


def _compute_sl(entry: float, raid_low: float) -> float:
    """
    SL = min(technical stop below raid low, entry - fixed risk).
    Technical stop = raid_low - SL_BUFFER.
    """
    technical_sl = raid_low - config.SL_BUFFER
    return min(technical_sl, entry - config.SL_BUFFER)


# ─────────────────────────────────────────────────────────────────────────────
# Main Strategy Engine
# ─────────────────────────────────────────────────────────────────────────────

class ICTLongStrategy:
    """
    Stateful strategy engine. Feed 5m bars one at a time (or replay in backtest).
    Emits Signal objects when a setup completes.
    """

    def __init__(self, df_1m: pd.DataFrame):
        self.df_1m = df_1m
        self._emitted_ids: set[str] = set()
        self._daily_alert_count: dict[str, int] = {}  # date_str -> count
        # Active raids awaiting confirmation
        self._active_raids: list[dict] = []
        # Active confirmed setups awaiting entry
        self._active_setups: list[dict] = []

    def _alert_allowed(self, bar_time: pd.Timestamp) -> bool:
        date_key = bar_time.tz_convert(PT).date().isoformat()
        return self._daily_alert_count.get(date_key, 0) < config.MAX_ALERTS_PER_DAY

    def _record_alert(self, bar_time: pd.Timestamp):
        date_key = bar_time.tz_convert(PT).date().isoformat()
        self._daily_alert_count[date_key] = self._daily_alert_count.get(date_key, 0) + 1

    def process_bar(self, df_5m: pd.DataFrame, bar_idx: int) -> list[Signal]:
        """
        Process one 5m bar. Returns list of new signals (0, 1, or more).
        df_5m must be the full dataframe up to and including bar_idx.
        """
        if bar_idx < 2:
            return []

        bar = df_5m.iloc[bar_idx]
        bar_time = df_5m.index[bar_idx]

        if not _is_in_trade_window(bar_time):
            return []

        signals: list[Signal] = []

        # ── Step 1: Detect new raids ──────────────────────────────────────────
        sig_lows = get_significant_lows(self.df_1m, bar_time)
        for level in sig_lows:
            raid_price = level.price
            if bar["low"] < raid_price - config.RAID_THRESHOLD:
                raid_id = f"{level.name}_{raid_price:.4f}"
                # Avoid duplicate raid tracking
                existing = [r for r in self._active_raids if r["raid_id"] == raid_id]
                if not existing:
                    logger.debug(f"Raid detected: {level.name} @ {raid_price:.4f} at {bar_time}")
                    self._active_raids.append({
                        "raid_id": raid_id,
                        "level": level,
                        "raid_low": float(bar["low"]),
                        "raid_bar_idx": bar_idx,
                        "confirmed": False,
                        "disp_bar_idx": None,
                        "disp_ratio": None,
                        "fvgs": [],
                        "ob": None,
                    })

        # ── Step 2: Confirm raids with displacement reclaim ───────────────────
        for raid in self._active_raids:
            if raid["confirmed"]:
                continue
            bars_since_raid = bar_idx - raid["raid_bar_idx"]
            if bars_since_raid > config.N_CONFIRM_BARS:
                continue  # Expired
            # Check: current bar is bullish displacement AND closes above raided level
            is_disp, ratio = _is_displacement_candle(bar, df_5m, bar_idx)
            level_price = raid["level"].price
            if is_disp and bar["close"] > level_price:
                raid["confirmed"] = True
                raid["disp_bar_idx"] = bar_idx
                raid["disp_ratio"] = ratio
                # Detect FVGs formed AFTER displacement (in subsequent bars)
                # and OB immediately before displacement
                raid["ob"] = _detect_ob(df_5m, bar_idx)
                logger.info(
                    f"Raid CONFIRMED: {raid['level'].name} | "
                    f"disp ratio={ratio:.2f} | bar={bar_time}"
                )

        # ── Step 3: Check entry triggers on confirmed raids ───────────────────
        for raid in self._active_raids:
            if not raid["confirmed"]:
                continue

            disp_idx = raid["disp_bar_idx"]
            if disp_idx is None or bar_idx <= disp_idx:
                continue

            # Update FVGs (detect any that formed after displacement, up to now)
            raid["fvgs"] = _detect_fvgs(df_5m, disp_idx + 1)

            # ── Signal A: iFVG mid close ──────────────────────────────────────
            for fvg in raid["fvgs"]:
                if fvg.filled:
                    continue
                fvg_bar_idx = df_5m.index.get_loc(fvg.bar_time) if fvg.bar_time in df_5m.index else None
                if fvg_bar_idx is None or bar_idx <= fvg_bar_idx:
                    continue
                # Price trades into FVG zone and closes above midpoint
                price_in_zone = bar["low"] <= fvg.upper and bar["high"] >= fvg.lower
                closes_above_mid = bar["close"] > fvg.midpoint
                if price_in_zone and closes_above_mid:
                    sig_id = f"LONG_iFVG_{bar_time.isoformat()}_{raid['raid_id']}_{fvg.bar_time.isoformat()}"
                    if sig_id not in self._emitted_ids and self._alert_allowed(bar_time):
                        entry = bar["close"]
                        sl    = _compute_sl(entry, raid["raid_low"])
                        tp    = _swing_high_tp(df_5m, bar_idx) or entry * 1.005
                        sig = Signal(
                            signal_id=sig_id,
                            signal_type="LONG_iFVG",
                            bar_time=bar_time,
                            entry=entry,
                            sl=sl,
                            tp=tp,
                            raided_level=raid["level"],
                            raid_low=raid["raid_low"],
                            displacement_time=df_5m.index[disp_idx],
                            displacement_ratio=raid["disp_ratio"],
                            fvg=fvg,
                            reasoning=(
                                f"Raid of {raid['level'].name} ({raid['level'].price:.4f}), "
                                f"reclaimed via displacement (ratio={raid['disp_ratio']:.2f}x). "
                                f"FVG [{fvg.lower:.4f}-{fvg.upper:.4f}] mid={fvg.midpoint:.4f}. "
                                f"Close {bar['close']:.4f} > mid."
                            ),
                        )
                        self._emitted_ids.add(sig_id)
                        self._record_alert(bar_time)
                        fvg.filled = True
                        signals.append(sig)
                        logger.info(f"SIGNAL {sig.signal_type} @ {entry:.4f} | SL={sl:.4f} TP={tp:.4f}")

            # ── Signal B: OB touch ────────────────────────────────────────────
            ob = raid.get("ob")
            if ob is not None:
                price_touches_ob = bar["high"] >= ob.ob_low and bar["low"] <= ob.ob_high
                if price_touches_ob:
                    sig_id = f"LONG_OB_{bar_time.isoformat()}_{raid['raid_id']}"
                    if sig_id not in self._emitted_ids and self._alert_allowed(bar_time):
                        entry = bar["open"]  # enter at open of touch bar
                        sl    = _compute_sl(entry, raid["raid_low"])
                        tp    = _swing_high_tp(df_5m, bar_idx) or entry * 1.005
                        sig = Signal(
                            signal_id=sig_id,
                            signal_type="LONG_OB",
                            bar_time=bar_time,
                            entry=entry,
                            sl=sl,
                            tp=tp,
                            raided_level=raid["level"],
                            raid_low=raid["raid_low"],
                            displacement_time=df_5m.index[disp_idx],
                            displacement_ratio=raid["disp_ratio"],
                            ob=ob,
                            reasoning=(
                                f"Raid of {raid['level'].name} ({raid['level'].price:.4f}), "
                                f"reclaimed via displacement (ratio={raid['disp_ratio']:.2f}x). "
                                f"OB zone [{ob.ob_low:.4f}-{ob.ob_high:.4f}] touched."
                            ),
                        )
                        self._emitted_ids.add(sig_id)
                        self._record_alert(bar_time)
                        signals.append(sig)
                        logger.info(f"SIGNAL {sig.signal_type} @ {entry:.4f} | SL={sl:.4f} TP={tp:.4f}")

        # Clean up expired raids (older than N_CONFIRM_BARS and not confirmed)
        self._active_raids = [
            r for r in self._active_raids
            if r["confirmed"] or (bar_idx - r["raid_bar_idx"]) <= config.N_CONFIRM_BARS
        ]

        return signals
