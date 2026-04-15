"""
Signal Engine — Pure signal detection with no side effects.

Wraps ICT long + short strategies, handles deduplication,
and returns clean Signal objects. No broker calls, no DB writes.
"""
import logging
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from strategy.ict_long import run_strategy
from strategy.ict_short import run_strategy_short
import config

log = logging.getLogger(__name__)


@dataclass
class Signal:
    """Immutable signal detected by the ICT strategy."""
    signal_type: str          # "LONG_iFVG", "SHORT_OB", etc.
    direction: str            # "LONG" or "SHORT"
    entry_price: float
    sl: float
    tp: float
    setup_id: str
    ticker: str = ""
    details: dict = field(default_factory=dict)  # raid, confirmation, FVG/OB specifics

    @property
    def dedup_key(self) -> str:
        return f"{self.signal_type}_{round(self.entry_price, 2)}"


class SignalEngine:
    """
    Pure signal detection engine. Detects ICT setups and returns Signal objects.

    Responsibilities:
    - Run ICT long + short strategies
    - Deduplicate signals by (signal_type, entry_price)
    - Track seen setups to avoid re-signaling
    - NO broker calls, NO DB writes, NO trade management
    """

    def __init__(self, ticker: str):
        self.ticker = ticker
        self._seen_setups: set = set()
        self._alerts_today: int = 0

    def reset_daily(self):
        """Reset daily state. Called at midnight by scanner."""
        self._seen_setups.clear()
        self._alerts_today = 0

    def detect(self, bars_1m: pd.DataFrame, bars_1h: pd.DataFrame,
               bars_4h: pd.DataFrame, levels: list,
               max_alerts: int = None) -> List[Signal]:
        """
        Run ICT signal detection. Returns list of new, deduplicated Signals.

        Pure function — no side effects. Does NOT mark setups as used.
        Call mark_used() after a trade is successfully entered.
        """
        if max_alerts is None:
            max_alerts = config.MAX_ALERTS_PER_DAY

        # Scan last 120 bars (2 hours) for setups
        bars_scan = bars_1m.iloc[-120:] if len(bars_1m) > 120 else bars_1m

        # Run long strategy
        signals_long = run_strategy(
            bars_scan, bars_1h, bars_4h, levels,
            alerts_today=self._alerts_today,
        )

        # Run short strategy
        signals_short = run_strategy_short(
            bars_scan, bars_1h, bars_4h, levels,
            alerts_today=self._alerts_today,
            max_alerts=max_alerts,
        )

        raw_signals = signals_long + signals_short

        # Deduplicate by (signal_type, entry_price) — prevents duplicate emails
        seen_combos: dict = {}
        deduped: list = []
        for sig in raw_signals:
            key = (sig["signal_type"], round(sig["entry_price"], 2))
            if key not in seen_combos:
                seen_combos[key] = True
                deduped.append(sig)
            else:
                log.info(f"[{self.ticker}] DUPLICATE SIGNAL filtered: "
                         f"{sig['signal_type']} @ ${sig['entry_price']:.2f}")

        # Filter out already-seen setups and convert to Signal objects
        signals: List[Signal] = []
        for sig in deduped:
            setup_id = sig.get("setup_id", "")
            if setup_id in self._seen_setups:
                continue  # Already traded or seen this setup

            signals.append(Signal(
                signal_type=sig.get("signal_type", "UNKNOWN"),
                direction=sig.get("direction", "LONG"),
                entry_price=sig["entry_price"],
                sl=sig["sl"],
                tp=sig["tp"],
                setup_id=setup_id,
                ticker=self.ticker,
                details={
                    "raid": sig.get("raid", {}),
                    "confirmation": sig.get("confirmation", {}),
                    "fvg": sig.get("fvg"),
                    "ob": sig.get("ob"),
                    # Preserve full signal dict for email/logging
                    "_raw": sig,
                },
            ))

        return signals

    def mark_used(self, setup_id: str):
        """Mark a setup as consumed (trade entered). Prevents re-signaling."""
        self._seen_setups.add(setup_id)
        self._alerts_today += 1

    def clear_seen_setups(self):
        """Clear seen setups (e.g., after a trade closes to allow new entries)."""
        self._seen_setups.clear()

    @property
    def alerts_today(self) -> int:
        return self._alerts_today
