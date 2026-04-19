"""
VWAP Mean Reversion strategy plugin.

Trades pullbacks to session VWAP in trending markets. See
docs/vwap_strategy_design.md for the full rationale.

Entry conditions (all must be true on the same bar):
  1. Current price is within VWAP_TOUCH_THRESHOLD of session VWAP
  2. The 1h EMA(VWAP_TREND_EMA) slope agrees with the trade direction
  3. RSI(VWAP_RSI_PERIOD) is oversold (for LONG) or overbought (for SHORT)

Exits use the shared evaluate_exit() pipeline — TP/SL with ATR-based
levels, trailing, roll, time, EOD.

Pure function — no IB calls, no DB writes.
"""
from __future__ import annotations

import logging
from typing import List, Optional

import numpy as np
import pandas as pd

from strategy.base_strategy import BaseStrategy, Signal, StrategyRegistry

log = logging.getLogger(__name__)


def _get_today_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """Return only the most recent trading day's bars."""
    if bars is None or len(bars) == 0:
        return bars
    last_date = bars.index[-1].date()
    return bars[bars.index.date == last_date]


def _session_vwap(bars: pd.DataFrame) -> pd.Series:
    """Session-reset volume-weighted average price."""
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3.0
    vol_price = typical * bars["volume"]
    day = bars.index.normalize()
    cum_vp = vol_price.groupby(day).cumsum()
    cum_v = bars["volume"].groupby(day).cumsum()
    return cum_vp / cum_v.replace(0, 1e-10)


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


def _atr(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = bars["close"].shift(1)
    tr = pd.concat([
        bars["high"] - bars["low"],
        (bars["high"] - prev_close).abs(),
        (bars["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


@StrategyRegistry.register
class VWAPStrategy(BaseStrategy):
    """VWAP Mean Reversion — buy pullbacks to session VWAP in bullish
    trends, sell rallies to VWAP in bearish trends."""

    @property
    def name(self) -> str:
        return "vwap_revert"

    @property
    def description(self) -> str:
        return ("VWAP Mean Reversion — trade pullbacks to session VWAP "
                "in trending markets with RSI confirmation")

    def __init__(
        self,
        touch_threshold: float = 0.001,   # within 0.1% of VWAP
        trend_ema: int = 20,              # EMA period on 1h bars
        rsi_period: int = 14,
        rsi_oversold: float = 35.0,
        rsi_overbought: float = 65.0,
        atr_period: int = 14,
        tp_atr_mult: float = 2.0,
        sl_atr_mult: float = 1.0,
    ):
        self.touch_threshold = touch_threshold
        self.trend_ema = trend_ema
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.atr_period = atr_period
        self.tp_atr_mult = tp_atr_mult
        self.sl_atr_mult = sl_atr_mult
        self._seen_setups: set[str] = set()

    def configure(self, settings: dict) -> None:
        def _get(key: str, cast):
            if key in settings:
                try:
                    return cast(settings[key])
                except (TypeError, ValueError):
                    return None
            return None

        t = _get("VWAP_TOUCH_THRESHOLD", float)
        if t is not None:
            self.touch_threshold = t
        t = _get("VWAP_TREND_EMA", int)
        if t is not None:
            self.trend_ema = t
        t = _get("VWAP_RSI_PERIOD", int)
        if t is not None:
            self.rsi_period = t
        t = _get("VWAP_RSI_OVERSOLD", float)
        if t is not None:
            self.rsi_oversold = t
        t = _get("VWAP_RSI_OVERBOUGHT", float)
        if t is not None:
            self.rsi_overbought = t
        t = _get("VWAP_ATR_PERIOD", int)
        if t is not None:
            self.atr_period = t
        t = _get("VWAP_TP_ATR_MULT", float)
        if t is not None:
            self.tp_atr_mult = t
        t = _get("VWAP_SL_ATR_MULT", float)
        if t is not None:
            self.sl_atr_mult = t

    def reset_daily(self) -> None:
        self._seen_setups.clear()

    def mark_used(self, setup_id: str) -> None:
        self._seen_setups.add(setup_id)

    # ── The core ──────────────────────────────────────────────

    def detect(self, bars_1m: pd.DataFrame, bars_1h: pd.DataFrame,
               bars_4h: pd.DataFrame, levels: list,
               ticker: str) -> List[Signal]:
        # Minimum history: need enough for RSI + ATR + meaningful VWAP
        min_bars = max(self.rsi_period, self.atr_period, self.trend_ema) + 5
        if bars_1m is None or len(bars_1m) < min_bars:
            return []

        today = _get_today_bars(bars_1m)
        if len(today) < 30:
            return []

        # ── Session VWAP + current distance ──
        vwap = _session_vwap(today)
        current_price = float(today["close"].iloc[-1])
        current_vwap = float(vwap.iloc[-1])
        if current_vwap <= 0:
            return []

        distance_pct = (current_price - current_vwap) / current_vwap
        abs_distance = abs(distance_pct)
        if abs_distance > self.touch_threshold:
            return []  # Not close enough to VWAP

        # ── Trend filter from 1h bars ──
        if bars_1h is None or len(bars_1h) < self.trend_ema + 1:
            return []
        ema = bars_1h["close"].ewm(span=self.trend_ema, adjust=False).mean()
        trend_bullish = bool(bars_1h["close"].iloc[-1] > ema.iloc[-1])
        trend_bearish = not trend_bullish

        # ── RSI on the base timeframe ──
        rsi_series = _rsi(today["close"], self.rsi_period)
        if rsi_series.isna().iloc[-1]:
            return []
        current_rsi = float(rsi_series.iloc[-1])

        # ── ATR for stop/target sizing ──
        atr_series = _atr(today, self.atr_period)
        if atr_series.isna().iloc[-1]:
            return []
        current_atr = float(atr_series.iloc[-1])
        if current_atr <= 0:
            return []

        today_date = today.index[-1].date()
        signals: List[Signal] = []

        # ── LONG: bullish trend + price at/below VWAP + RSI oversold ──
        if (trend_bullish
                and distance_pct <= 0.0                      # price is AT or below VWAP
                and current_rsi < self.rsi_oversold):
            setup_id = f"VWAP_LONG_{ticker}_{today_date}_{int(current_rsi)}"
            if setup_id not in self._seen_setups:
                signals.append(Signal(
                    signal_type="VWAP_REVERT_LONG",
                    direction="LONG",
                    entry_price=current_price,
                    sl=current_price - self.sl_atr_mult * current_atr,
                    tp=current_price + self.tp_atr_mult * current_atr,
                    setup_id=setup_id,
                    ticker=ticker,
                    strategy_name="vwap_revert",
                    confidence=0.65,
                    details={
                        "vwap": current_vwap,
                        "price_vs_vwap_pct": distance_pct,
                        "rsi": current_rsi,
                        "atr": current_atr,
                        "trend": "BULL",
                        "timeframe_trend_ema": self.trend_ema,
                    },
                ))

        # ── SHORT: bearish trend + price at/above VWAP + RSI overbought ──
        if (trend_bearish
                and distance_pct >= 0.0
                and current_rsi > self.rsi_overbought):
            setup_id = f"VWAP_SHORT_{ticker}_{today_date}_{int(current_rsi)}"
            if setup_id not in self._seen_setups:
                signals.append(Signal(
                    signal_type="VWAP_REVERT_SHORT",
                    direction="SHORT",
                    entry_price=current_price,
                    sl=current_price + self.sl_atr_mult * current_atr,
                    tp=current_price - self.tp_atr_mult * current_atr,
                    setup_id=setup_id,
                    ticker=ticker,
                    strategy_name="vwap_revert",
                    confidence=0.65,
                    details={
                        "vwap": current_vwap,
                        "price_vs_vwap_pct": distance_pct,
                        "rsi": current_rsi,
                        "atr": current_atr,
                        "trend": "BEAR",
                        "timeframe_trend_ema": self.trend_ema,
                    },
                ))

        return signals
