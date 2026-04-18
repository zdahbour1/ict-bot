"""
ICT Strategy plugin — wraps the existing SignalEngine (ICT long + short)
in the BaseStrategy interface so it can be run alongside other strategies.

Behavior is identical to the legacy SignalEngine — this is purely adapter
code (ENH-024). No signal logic lives here.
"""
from __future__ import annotations

from typing import List
import pandas as pd

from strategy.base_strategy import BaseStrategy, Signal, StrategyRegistry
from strategy.signal_engine import SignalEngine


@StrategyRegistry.register
class ICTStrategy(BaseStrategy):
    """Inner Circle Trader plugin — raids + iFVG/OB displacement."""

    @property
    def name(self) -> str:
        return "ict"

    @property
    def description(self) -> str:
        return ("ICT (Inner Circle Trader) — liquidity raids + iFVG/OB "
                "displacement with multi-timeframe confirmation")

    def __init__(self, ticker: str = ""):
        # SignalEngine is per-ticker; in multi-ticker mode the scanner
        # must instantiate one ICTStrategy per ticker. We also allow
        # late-binding via detect(ticker=...) to keep the plugin stateless
        # for dashboards/backtests that haven't chosen a ticker yet.
        self._ticker = ticker
        self._engines: dict[str, SignalEngine] = {}
        if ticker:
            self._engines[ticker] = SignalEngine(ticker)

    def _engine_for(self, ticker: str) -> SignalEngine:
        eng = self._engines.get(ticker)
        if eng is None:
            eng = SignalEngine(ticker)
            self._engines[ticker] = eng
        return eng

    def detect(self, bars_1m: pd.DataFrame, bars_1h: pd.DataFrame,
               bars_4h: pd.DataFrame, levels: list, ticker: str) -> List[Signal]:
        eng = self._engine_for(ticker)
        raw_signals = eng.detect(bars_1m, bars_1h, bars_4h, levels)
        # SignalEngine returns its own Signal dataclass — re-map to the
        # canonical plugin Signal (adds strategy_name + confidence).
        out: List[Signal] = []
        for s in raw_signals:
            out.append(Signal(
                signal_type=s.signal_type,
                direction=s.direction,
                entry_price=s.entry_price,
                sl=s.sl,
                tp=s.tp,
                setup_id=s.setup_id,
                ticker=s.ticker or ticker,
                strategy_name="ict",
                confidence=0.75,
                details=s.details,
            ))
        return out

    def mark_used(self, setup_id: str) -> None:
        for eng in self._engines.values():
            eng.mark_used(setup_id)

    def reset_daily(self) -> None:
        for eng in self._engines.values():
            eng.reset_daily()
