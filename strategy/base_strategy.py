"""
Strategy Plugin Framework — base interface (ENH-024).

Every strategy implements BaseStrategy and returns list[Signal].
Strategies are pure: no IB calls, no DB writes, no side effects.

The scanner runs every enabled strategy on each tick and hands the
resulting signals to the shared trade engine. This separation lets
us add/remove strategies without touching execution code.

See docs/strategy_plugin_framework.md for architecture details.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd


@dataclass
class LegSpec:
    """One leg of a multi-leg trade (iron condor, spread, hedged position).

    Strategies with multi-leg entries override ``BaseStrategy.place_legs()``
    to return a list of ``LegSpec`` objects. The TradeEntryManager hands
    them to ``IBClient.place_multi_leg_order`` which submits each leg as
    an independent order inside one OCA group.

    See docs/multi_strategy_architecture_v2.md Phase 6.
    """
    sec_type: str                        # 'OPT' | 'FOP' | 'STK'
    symbol: str                          # OCC for options, ticker for stock
    direction: str                       # 'LONG' | 'SHORT'
    contracts: int
    # Option-only — None for STK
    strike: Optional[float] = None
    right: Optional[str] = None          # 'C' | 'P'
    expiry: Optional[str] = None         # YYYYMMDD
    multiplier: int = 100
    exchange: str = 'SMART'
    currency: str = 'USD'
    leg_role: Optional[str] = None       # 'short_call', 'hedge_stock', etc
    underlying: Optional[str] = None


@dataclass
class Signal:
    """Standard signal output from any strategy.

    A superset of the legacy ICT signal dict so strategies emitting
    these Signal objects can be consumed by both the new plugin-aware
    scanner and (via .to_dict()) the legacy trade entry manager.
    """
    signal_type: str          # "LONG_iFVG", "ORB_BREAKOUT_LONG", "VWAP_REVERT_SHORT"
    direction: str            # "LONG" or "SHORT"
    entry_price: float
    sl: float                 # stop loss price (underlying)
    tp: float                 # take profit price (underlying)
    setup_id: str             # unique ID for dedup
    ticker: str = ""
    strategy_name: str = ""   # "ict", "orb", "vwap_revert"
    confidence: float = 0.0   # 0.0 to 1.0 — strategy's confidence
    details: dict = field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        return f"{self.signal_type}_{round(self.entry_price, 2)}"

    def to_dict(self) -> dict:
        """Flatten to the legacy signal dict shape used by older callers."""
        d = {
            "signal_type": self.signal_type,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "sl": self.sl,
            "tp": self.tp,
            "setup_id": self.setup_id,
            "ticker": self.ticker,
            "strategy_name": self.strategy_name,
            "confidence": self.confidence,
        }
        # Lift raid/confirmation/fvg/ob up so legacy code keeps working
        for k in ("raid", "confirmation", "fvg", "ob"):
            if k in self.details:
                d[k] = self.details[k]
        d["details"] = self.details
        return d


class BaseStrategy(ABC):
    """Abstract base class for all trading strategies."""

    # ── Identity ──────────────────────────────────────────────
    @property
    @abstractmethod
    def name(self) -> str:
        """Unique strategy name: 'ict', 'orb', 'vwap_revert'."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description for dashboard."""

    # ── Core detection ────────────────────────────────────────
    @abstractmethod
    def detect(
        self,
        bars_1m: pd.DataFrame,
        bars_1h: pd.DataFrame,
        bars_4h: pd.DataFrame,
        levels: list,
        ticker: str,
    ) -> List[Signal]:
        """Detect trading signals from price data.

        MUST be pure — no side effects, no IB calls, no DB writes.
        Returns list of Signal objects (may be empty).
        """

    # ── Optional hooks ────────────────────────────────────────
    def configure(self, settings: dict) -> None:
        """Optional: configure strategy parameters from settings table."""
        return None

    def reset_daily(self) -> None:
        """Optional: reset daily state (seen setups, counters)."""
        return None

    def mark_used(self, setup_id: str) -> None:
        """Optional: mark a setup as consumed so it won't re-fire."""
        return None

    # ── Multi-leg entry (Phase 6) ─────────────────────────────
    def place_legs(self, signal: Signal) -> Optional[List["LegSpec"]]:
        """Return N LegSpec objects for a multi-leg entry, or None for
        single-leg (default). TradeEntryManager calls this before falling
        back to the single-leg option_selector path.

        Default implementation returns None — preserves backward compat
        for every existing single-leg strategy (ICT, ORB, VWAP). Only
        delta-neutral / spread / hedged plugins override.
        """
        return None


class StrategyRegistry:
    """Simple in-process registry for strategy plugins.

    Register strategies at import time:
        StrategyRegistry.register(ORBStrategy)

    Then the scanner resolves them by name from the settings table.
    """
    _classes: dict[str, type[BaseStrategy]] = {}

    @classmethod
    def register(cls, strategy_cls: type[BaseStrategy]) -> type[BaseStrategy]:
        # Instantiate once briefly to read .name (no I/O allowed in __init__)
        instance = strategy_cls()
        cls._classes[instance.name] = strategy_cls
        return strategy_cls

    @classmethod
    def get(cls, name: str) -> Optional[type[BaseStrategy]]:
        return cls._classes.get(name)

    @classmethod
    def all_names(cls) -> list[str]:
        return sorted(cls._classes.keys())

    @classmethod
    def instantiate(cls, name: str, **kwargs) -> Optional[BaseStrategy]:
        strategy_cls = cls.get(name)
        if strategy_cls is None:
            return None
        return strategy_cls(**kwargs)
