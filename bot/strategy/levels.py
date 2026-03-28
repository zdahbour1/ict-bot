"""
Significant High/Low levels (Liquidity Pools).
Computes: PDH/PDL, Rolling 1H/4H, Asia session H/L, London session H/L.
All inputs are UTC-indexed DataFrames. PT timezone passed as config.
"""
import pandas as pd
import pytz
from dataclasses import dataclass
from typing import Optional


PT = pytz.timezone("America/Los_Angeles")


@dataclass
class Level:
    name: str          # e.g. "PDL", "Asia Low", "1H Low"
    price: float
    level_type: str    # "low" or "high"


def _to_pt(df: pd.DataFrame) -> pd.DataFrame:
    """Return copy with index converted to PT."""
    return df.copy().tz_convert(PT)


def prior_day_levels(df_1m: pd.DataFrame, as_of: pd.Timestamp) -> list[Level]:
    """PDH and PDL as of `as_of` timestamp."""
    df_pt = _to_pt(df_1m)
    as_of_pt = as_of.tz_convert(PT)
    today_pt = as_of_pt.normalize()
    yesterday_pt = today_pt - pd.Timedelta(days=1)

    # Walk back to find a trading day with data (handles weekends/holidays)
    for offset in range(1, 8):
        day = today_pt - pd.Timedelta(days=offset)
        mask = (df_pt.index >= day) & (df_pt.index < day + pd.Timedelta(days=1))
        day_data = df_pt[mask]
        if not day_data.empty:
            return [
                Level("PDH", float(day_data["high"].max()), "high"),
                Level("PDL", float(day_data["low"].min()), "low"),
            ]
    return []


def rolling_levels(df: pd.DataFrame, as_of: pd.Timestamp, hours: int) -> list[Level]:
    """Rolling N-hour high/low from bar data ending at `as_of`."""
    cutoff = as_of - pd.Timedelta(hours=hours)
    window = df[(df.index >= cutoff) & (df.index <= as_of)]
    if window.empty:
        return []
    tag = f"{hours}H"
    return [
        Level(f"{tag} High", float(window["high"].max()), "high"),
        Level(f"{tag} Low",  float(window["low"].min()),  "low"),
    ]


def session_levels(df_1m: pd.DataFrame, as_of: pd.Timestamp,
                   session_name: str, start_hour_pt: int, end_hour_pt: int) -> list[Level]:
    """
    High/Low for a named session (Asia or London) on the same calendar date as as_of (PT).
    Asia PT: 14:00-21:00 (previous PT calendar day crosses midnight)
    London PT: 00:00-05:00
    """
    df_pt = _to_pt(df_1m)
    as_of_pt = as_of.tz_convert(PT)

    # Determine which calendar date to use for each session
    # Asia session 14:00-21:00 PT → look at previous day's 14:00 to current day's 21:00
    # London 00:00-05:00 PT → look at today's midnight-5am
    today_pt = as_of_pt.normalize()

    if session_name == "Asia":
        # Asia session is 14:00-21:00 PT on the PREVIOUS calendar day
        sess_start = (today_pt - pd.Timedelta(days=1)).replace(hour=start_hour_pt)
        sess_end   = (today_pt - pd.Timedelta(days=1)).replace(hour=end_hour_pt)
    else:
        sess_start = today_pt.replace(hour=start_hour_pt)
        sess_end   = today_pt.replace(hour=end_hour_pt)

    mask = (df_pt.index >= sess_start) & (df_pt.index <= sess_end)
    sess_data = df_pt[mask]
    if sess_data.empty:
        return []
    return [
        Level(f"{session_name} High", float(sess_data["high"].max()), "high"),
        Level(f"{session_name} Low",  float(sess_data["low"].min()),  "low"),
    ]


def get_all_significant_levels(df_1m: pd.DataFrame, as_of: pd.Timestamp) -> list[Level]:
    """
    Compute all significant levels as of `as_of`.
    Returns list of Level objects (both highs and lows).
    """
    levels: list[Level] = []
    levels += prior_day_levels(df_1m, as_of)
    levels += rolling_levels(df_1m, as_of, hours=1)
    levels += rolling_levels(df_1m, as_of, hours=4)
    levels += session_levels(df_1m, as_of, "Asia",   start_hour_pt=14, end_hour_pt=21)
    levels += session_levels(df_1m, as_of, "London", start_hour_pt=0,  end_hour_pt=5)
    return levels


def get_significant_lows(df_1m: pd.DataFrame, as_of: pd.Timestamp) -> list[Level]:
    """Return only the low levels (for long-only raid detection)."""
    return [lv for lv in get_all_significant_levels(df_1m, as_of) if lv.level_type == "low"]
