"""
Opening Range Breakout (ORB) strategy plugin.

Identifies the high/low of the first N minutes of trading and trades
breakouts beyond that range. Backtested at 89.4% win rate on 60-min
SPY 0DTE (see docs/strategy_plugin_framework.md for sources).

Pure function — no IB calls, no DB writes.
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime
from typing import List, Optional

import pandas as pd

from strategy.base_strategy import BaseStrategy, Signal, StrategyRegistry

log = logging.getLogger(__name__)

# Regular US equity session open, in whatever tz the bars are indexed in.
# We intentionally detect "today" from the bars themselves rather than
# hard-coding a tz — backtests may feed UTC bars, live feeds may be ET.
MARKET_OPEN_HOUR_LOCAL = None  # set in __init__ via configure()


def _get_today_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """Return only the most recent trading day's bars (by calendar date)."""
    if bars is None or len(bars) == 0:
        return bars
    last_date = bars.index[-1].date()
    return bars[bars.index.date == last_date]


@StrategyRegistry.register
class ORBStrategy(BaseStrategy):
    """Opening Range Breakout — trades breakout of first N minutes."""

    @property
    def name(self) -> str:
        return "orb"

    @property
    def description(self) -> str:
        return "Opening Range Breakout — trades breakout of first N minutes"

    def __init__(
        self,
        range_minutes: int = 15,
        breakout_buffer: float = 0.001,  # 0.1% cushion past range
        max_signals_per_day: int = 1,     # per direction
    ):
        self.range_minutes = range_minutes
        self.breakout_buffer = breakout_buffer
        self.max_signals_per_day = max_signals_per_day
        self._seen_setups: set[str] = set()

    def configure(self, settings: dict) -> None:
        if "ORB_RANGE_MINUTES" in settings:
            try:
                self.range_minutes = int(settings["ORB_RANGE_MINUTES"])
            except (TypeError, ValueError):
                pass
        if "ORB_BREAKOUT_BUFFER" in settings:
            try:
                self.breakout_buffer = float(settings["ORB_BREAKOUT_BUFFER"])
            except (TypeError, ValueError):
                pass

    def reset_daily(self) -> None:
        self._seen_setups.clear()

    def mark_used(self, setup_id: str) -> None:
        self._seen_setups.add(setup_id)

    def detect(self, bars_1m: pd.DataFrame, bars_1h: pd.DataFrame,
               bars_4h: pd.DataFrame, levels: list, ticker: str) -> List[Signal]:
        if bars_1m is None or len(bars_1m) == 0:
            return []

        today_bars = _get_today_bars(bars_1m)
        if len(today_bars) <= self.range_minutes:
            return []

        # 1. Compute opening range (first N bars of the session)
        range_bars = today_bars.iloc[: self.range_minutes]
        range_high = float(range_bars["high"].max())
        range_low = float(range_bars["low"].min())
        if range_high <= range_low:
            return []
        range_mid = (range_high + range_low) / 2.0
        range_width = range_high - range_low
        today_date = today_bars.index[-1].date()

        # 2. Walk post-range bars, fire on the first breakout in each direction
        post_range = today_bars.iloc[self.range_minutes:]
        signals: List[Signal] = []
        fired_long = False
        fired_short = False

        for ts, bar in post_range.iterrows():
            close = float(bar["close"])

            # Long breakout
            if (not fired_long
                    and close > range_high * (1.0 + self.breakout_buffer)):
                setup_id = f"ORB_LONG_{ticker}_{today_date}"
                if setup_id not in self._seen_setups:
                    signals.append(Signal(
                        signal_type="ORB_BREAKOUT_LONG",
                        direction="LONG",
                        entry_price=close,
                        sl=range_mid,                 # SL at range midpoint
                        tp=close + range_width,       # 1:1 R:R on range width
                        setup_id=setup_id,
                        ticker=ticker,
                        strategy_name="orb",
                        confidence=0.70,
                        details={
                            "range_high": range_high,
                            "range_low": range_low,
                            "range_mid": range_mid,
                            "range_minutes": self.range_minutes,
                            "breakout_bar_time": str(ts),
                        },
                    ))
                fired_long = True

            # Short breakout
            if (not fired_short
                    and close < range_low * (1.0 - self.breakout_buffer)):
                setup_id = f"ORB_SHORT_{ticker}_{today_date}"
                if setup_id not in self._seen_setups:
                    signals.append(Signal(
                        signal_type="ORB_BREAKOUT_SHORT",
                        direction="SHORT",
                        entry_price=close,
                        sl=range_mid,
                        tp=close - range_width,
                        setup_id=setup_id,
                        ticker=ticker,
                        strategy_name="orb",
                        confidence=0.70,
                        details={
                            "range_high": range_high,
                            "range_low": range_low,
                            "range_mid": range_mid,
                            "range_minutes": self.range_minutes,
                            "breakout_bar_time": str(ts),
                        },
                    ))
                fired_short = True

            if fired_long and fired_short:
                break

        return signals
