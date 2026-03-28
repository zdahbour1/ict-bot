"""
Levels — computes all significant high/low levels used for raid detection.
Per PDF spec:
  - Prior Day High/Low (PDH/PDL)
  - Rolling 1H High/Low
  - Rolling 4H High/Low
  - Asia session High/Low  (14:00-21:00 PT)
  - London session High/Low (00:00-05:00 PT)
"""
import logging
from datetime import datetime, date, timedelta
from typing import List, Dict
import pandas as pd
import pytz

log = logging.getLogger(__name__)

PT = pytz.timezone("America/Los_Angeles")


def _to_pt(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of df with PT-localized index."""
    idx = df.index
    if idx.tzinfo is None:
        idx = idx.tz_localize("UTC")
    return df.set_index(idx.tz_convert(PT))


def compute_pdh_pdl(bars_1m: pd.DataFrame) -> List[Dict]:
    """Previous Day High and Low levels."""
    levels = []
    df = _to_pt(bars_1m)
    df["_date"] = df.index.date
    today = df["_date"].iloc[-1]

    prev_day = df[df["_date"] < today]
    if prev_day.empty:
        return levels

    last_date = prev_day["_date"].iloc[-1]
    prev = prev_day[prev_day["_date"] == last_date]
    levels.append({"label": "PDH", "price": float(prev["high"].max())})
    levels.append({"label": "PDL", "price": float(prev["low"].min())})
    log.debug(f"PDH={levels[0]['price']:.2f}  PDL={levels[1]['price']:.2f}")
    return levels


def compute_rolling_levels(bars_1h: pd.DataFrame, bars_4h: pd.DataFrame) -> List[Dict]:
    """Rolling 1H and 4H high/low from most recent completed bar."""
    levels = []
    for label, df in [("1H", bars_1h), ("4H", bars_4h)]:
        if df.empty or len(df) < 2:
            continue
        last = df.iloc[-1]
        levels.append({"label": f"{label}_HIGH", "price": float(last["high"])})
        levels.append({"label": f"{label}_LOW",  "price": float(last["low"])})
    return levels


def compute_session_levels(bars_1m: pd.DataFrame) -> List[Dict]:
    """
    Asia session  (PT 14:00-21:00) and
    London session (PT 00:00-05:00) high/low from the most recent session.
    """
    levels = []
    df = _to_pt(bars_1m)

    sessions = {
        "ASIA":   (14, 21),
        "LONDON": (0,  5),
    }
    for name, (start_h, end_h) in sessions.items():
        mask = (df.index.hour >= start_h) & (df.index.hour < end_h)
        session_bars = df[mask]
        if session_bars.empty:
            continue
        # Use the most recent session's last day
        last_date = session_bars.index.date[-1]
        recent = session_bars[session_bars.index.date == last_date]
        if recent.empty:
            continue
        levels.append({"label": f"{name}_HIGH", "price": float(recent["high"].max())})
        levels.append({"label": f"{name}_LOW",  "price": float(recent["low"].min())})
    return levels


def compute_opening_range(bars_1m: pd.DataFrame) -> List[Dict]:
    """
    Opening Range High/Low — first 15 minutes of today's session (6:30–6:45 AM PT).
    """
    levels = []
    df     = _to_pt(bars_1m)
    today  = df.index.date[-1]

    or_mask = (
        (df.index.date == today) &
        (df.index.hour == 6) &
        (df.index.minute >= 30) &
        (df.index.minute < 45)
    )
    or_bars = df[or_mask]
    if or_bars.empty:
        return levels

    levels.append({"label": "OR_HIGH", "price": float(or_bars["high"].max())})
    levels.append({"label": "OR_LOW",  "price": float(or_bars["low"].min())})
    log.debug(f"OR_HIGH={levels[0]['price']:.2f}  OR_LOW={levels[1]['price']:.2f}")
    return levels


def compute_prev_week_levels(bars_1m: pd.DataFrame) -> List[Dict]:
    """
    Previous Week High/Low.
    """
    from datetime import timedelta
    levels = []
    df     = _to_pt(bars_1m)
    today  = df.index.date[-1]

    week_start = today - timedelta(days=today.weekday() + 7)
    week_end   = week_start + timedelta(days=4)

    pw_mask = (df.index.date >= week_start) & (df.index.date <= week_end)
    pw_bars = df[pw_mask]
    if pw_bars.empty:
        return levels

    levels.append({"label": "PWH", "price": float(pw_bars["high"].max())})
    levels.append({"label": "PWL", "price": float(pw_bars["low"].min())})
    log.debug(f"PWH={levels[0]['price']:.2f}  PWL={levels[1]['price']:.2f}")
    return levels


def get_all_levels(bars_1m: pd.DataFrame,
                   bars_1h: pd.DataFrame,
                   bars_4h: pd.DataFrame) -> List[Dict]:
    """
    Returns all significant levels as a list of dicts:
      [{"label": "PDL", "price": 573.45}, ...]
    """
    levels = []
    levels += compute_pdh_pdl(bars_1m)
    levels += compute_rolling_levels(bars_1h, bars_4h)
    levels += compute_session_levels(bars_1m)
    levels += compute_opening_range(bars_1m)
    levels += compute_prev_week_levels(bars_1m)

    # Filter out zero/invalid prices
    levels = [l for l in levels if l["price"] > 0]
    level_strs = ["{label}={price:.2f}".format(**l) for l in levels]
    log.info(f"Computed {len(levels)} significant levels: {level_strs}")
    return levels
