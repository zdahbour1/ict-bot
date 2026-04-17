# Strategy Plugin Framework — Multi-Scanner Architecture

## Purpose

Extend the bot to support multiple trading strategies beyond ICT. Each strategy is a 
pluggable scanner that detects signals independently. All strategies share the same 
trade execution, management, and closure infrastructure (ARCH-005/006).

The strategy layer is FULLY DECOUPLED from trade management. Strategies produce signals. 
The trade engine executes them. This separation enables:
- Adding new strategies without touching execution code
- Running multiple strategies simultaneously
- Backtesting any strategy with the same framework
- A/B testing strategies against each other in live or paper trading

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    STRATEGY PLUGIN FRAMEWORK                     │
│                                                                   │
│  ┌─ Strategy Plugins (interchangeable) ────────────────────────┐ │
│  │                                                              │ │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │ │
│  │  │  ICT     │  │  ORB     │  │  VWAP    │  │  Custom  │   │ │
│  │  │ Strategy │  │ Strategy │  │ Strategy │  │ Strategy │   │ │
│  │  │          │  │          │  │          │  │          │   │ │
│  │  │ Raids    │  │ Opening  │  │ VWAP     │  │ Your     │   │ │
│  │  │ iFVG/OB  │  │ Range    │  │ Breakout │  │ Logic    │   │ │
│  │  │ Displace │  │ Breakout │  │ + Revert │  │ Here     │   │ │
│  │  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘   │ │
│  │       │              │              │              │         │ │
│  │       ▼              ▼              ▼              ▼         │ │
│  │  ┌──────────────────────────────────────────────────────┐   │ │
│  │  │              Signal (standard dataclass)              │   │ │
│  │  │  signal_type, direction, entry_price, sl, tp,         │   │ │
│  │  │  setup_id, ticker, details, strategy_name             │   │ │
│  │  └──────────────────────────┬───────────────────────────┘   │ │
│  └─────────────────────────────┼───────────────────────────────┘ │
│                                │                                  │
│  ┌─ Trade Engine (shared) ─────┼──────────────────────────────┐  │
│  │                             ▼                               │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │  │
│  │  │ Trade Entry  │  │ Exit Manager │  │ Reconciliation│     │  │
│  │  │ Manager      │  │ (DB-backed)  │  │ (2-pass sync) │     │  │
│  │  │              │  │              │  │               │     │  │
│  │  │ Pre-flight   │  │ Monitor P&L  │  │ DB ↔ IB      │     │  │
│  │  │ IB check     │  │ Exit conds   │  │               │     │  │
│  │  │ Place order  │  │ Atomic close │  │               │     │  │
│  │  └──────────────┘  └──────────────┘  └──────────────┘     │  │
│  │                                                             │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │  │
│  │  │ Option       │  │ Exit         │  │ Trade        │     │  │
│  │  │ Selector     │  │ Executor     │  │ Logger       │     │  │
│  │  │              │  │              │  │              │     │  │
│  │  │ ATM lookup   │  │ Cancel → Sell│  │ DB + CSV     │     │  │
│  │  │ Contract val │  │ Bracket mgmt │  │ Enrichment   │     │  │
│  │  └──────────────┘  └──────────────┘  └──────────────┘     │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                   │
│  ┌─ Data Layer (shared) ──────────────────────────────────────┐  │
│  │  PostgreSQL │ IB Connection Pool │ Price Feed               │  │
│  └─────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Strategy Plugin Interface

Every strategy MUST implement this interface:

```python
# strategy/base_strategy.py

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List
import pandas as pd


@dataclass
class Signal:
    """Standard signal output from any strategy."""
    signal_type: str          # "LONG_iFVG", "ORB_BREAKOUT_LONG", "VWAP_REVERT_SHORT"
    direction: str            # "LONG" or "SHORT"
    entry_price: float
    sl: float                 # stop loss price
    tp: float                 # take profit price
    setup_id: str             # unique ID for dedup
    ticker: str
    strategy_name: str        # "ict", "orb", "vwap_breakout"
    confidence: float = 0.0   # 0.0 to 1.0 — strategy's confidence in the signal
    details: dict = field(default_factory=dict)


class BaseStrategy(ABC):
    """Abstract base class for all trading strategies."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique strategy name: 'ict', 'orb', 'vwap_breakout'"""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description for dashboard."""
        pass

    @abstractmethod
    def detect(self, bars_1m: pd.DataFrame, bars_1h: pd.DataFrame,
               bars_4h: pd.DataFrame, levels: list,
               ticker: str) -> List[Signal]:
        """
        Detect trading signals from price data.
        
        MUST be pure — no side effects, no IB calls, no DB writes.
        Returns list of Signal objects.
        """
        pass

    def configure(self, config: dict):
        """Optional: configure strategy parameters from settings."""
        pass

    def reset_daily(self):
        """Optional: reset daily state (seen setups, counters)."""
        pass
```

---

## Recommended Strategy #1: Opening Range Breakout (ORB)

### Overview

The ORB strategy identifies the high and low of the first N minutes of trading (typically 
15 or 30 minutes), then trades breakouts above the high or below the low. This is one 
of the most backtested and documented day trading strategies.

### Backtested Performance

Based on research from [QuantifiedStrategies](https://www.quantifiedstrategies.com/opening-range-breakout-strategy/) 
and [Options Cafe](https://options.cafe/blog/0dte-opening-range-breakout-strategy-spy-backtested-results/):

- **60-minute ORB on SPY 0DTE**: 89.4% win rate, 1.44 profit factor
- **5-minute ORB on SPY 0DTE**: 40-42% win rate, but profitable overall (+$14,860 over 303 trades)
- Works best in first 2 hours of trading — [TradeSwing reports 400% annual returns](https://tradethatswing.com/opening-range-breakout-strategy-up-400-this-year/)

### Implementation

```python
# strategy/orb_strategy.py

class ORBStrategy(BaseStrategy):
    """Opening Range Breakout — trades the first N-minute range."""

    name = "orb"
    description = "Opening Range Breakout — trades breakout of first N minutes"

    def __init__(self):
        self.range_minutes = 15       # configurable: 5, 15, 30, 60
        self.breakout_buffer = 0.001  # 0.1% buffer above/below range
        self._seen_setups = set()

    def detect(self, bars_1m, bars_1h, bars_4h, levels, ticker) -> List[Signal]:
        signals = []
        
        # 1. Get today's bars only
        today_bars = _get_today_bars(bars_1m)
        if len(today_bars) < self.range_minutes:
            return []
        
        # 2. Calculate opening range (first N minutes)
        range_bars = today_bars.iloc[:self.range_minutes]
        range_high = range_bars['high'].max()
        range_low = range_bars['low'].min()
        range_mid = (range_high + range_low) / 2
        
        # 3. Check for breakout in subsequent bars
        post_range = today_bars.iloc[self.range_minutes:]
        for i, bar in post_range.iterrows():
            # Long breakout: close above range high
            if bar['close'] > range_high * (1 + self.breakout_buffer):
                setup_id = f"ORB_LONG_{ticker}_{bar.name.date()}"
                if setup_id not in self._seen_setups:
                    self._seen_setups.add(setup_id)
                    signals.append(Signal(
                        signal_type="ORB_BREAKOUT_LONG",
                        direction="LONG",
                        entry_price=bar['close'],
                        sl=range_mid,          # SL at range midpoint
                        tp=bar['close'] + (range_high - range_low),  # 1:1 R:R
                        setup_id=setup_id,
                        ticker=ticker,
                        strategy_name="orb",
                        confidence=0.7,
                        details={"range_high": range_high, "range_low": range_low,
                                 "range_minutes": self.range_minutes}
                    ))
                    break  # One signal per direction per day
            
            # Short breakout: close below range low
            if bar['close'] < range_low * (1 - self.breakout_buffer):
                setup_id = f"ORB_SHORT_{ticker}_{bar.name.date()}"
                if setup_id not in self._seen_setups:
                    self._seen_setups.add(setup_id)
                    signals.append(Signal(
                        signal_type="ORB_BREAKOUT_SHORT",
                        direction="SHORT",
                        entry_price=bar['close'],
                        sl=range_mid,
                        tp=bar['close'] - (range_high - range_low),
                        setup_id=setup_id,
                        ticker=ticker,
                        strategy_name="orb",
                        confidence=0.7,
                        details={"range_high": range_high, "range_low": range_low}
                    ))
                    break
        
        return signals
```

### Why ORB Complements ICT

| Aspect | ICT | ORB |
|--------|-----|-----|
| Signal timing | Throughout the day | First 15-60 minutes only |
| Signal type | Liquidity raids + displacement | Range breakout |
| Complexity | High (multi-step confirmation) | Simple (just range + breakout) |
| Best market | Trending + ranging | Strong open with momentum |
| Overlap | Low — different setups | ORB can confirm ICT direction |

---

## Recommended Strategy #2: VWAP Mean Reversion

### Overview

The VWAP (Volume Weighted Average Price) strategy trades pullbacks to VWAP in trending 
markets. When price is above VWAP, buy pullbacks to VWAP. When below, sell rallies to 
VWAP. This is a mean reversion approach that works well in liquid markets.

### Backtested Performance

Based on research from [QuantifiedStrategies](https://www.quantifiedstrategies.com/vwap-trading-strategy/)
and [QuantVPS](https://www.quantvps.com/blog/backtest-vwap-trading-strategy-python):

- **VWAP bounce strategy**: 713% total return over 3 years in one backtest (~200% annualized)
- Works best in trending markets with high volume
- [GitHub VwapProject](https://github.com/hedge0/VwapProject): Production VWAP bot for ES/NQ futures

### Implementation

```python
# strategy/vwap_strategy.py

class VWAPStrategy(BaseStrategy):
    """VWAP Mean Reversion — trade pullbacks to VWAP."""

    name = "vwap_revert"
    description = "VWAP Mean Reversion — buy at VWAP support, sell at VWAP resistance"

    def __init__(self):
        self.touch_threshold = 0.001  # within 0.1% of VWAP
        self.trend_ema = 20           # EMA period for trend
        self.rsi_period = 14
        self.rsi_oversold = 35
        self.rsi_overbought = 65
        self._seen_setups = set()

    def detect(self, bars_1m, bars_1h, bars_4h, levels, ticker) -> List[Signal]:
        signals = []
        
        if len(bars_1m) < 60:
            return []
        
        # 1. Calculate VWAP
        today_bars = _get_today_bars(bars_1m)
        if len(today_bars) < 30:
            return []
        
        cum_vol = today_bars['volume'].cumsum()
        cum_vp = (today_bars['close'] * today_bars['volume']).cumsum()
        vwap = cum_vp / cum_vol
        
        current_price = today_bars['close'].iloc[-1]
        current_vwap = vwap.iloc[-1]
        
        # 2. Determine trend (EMA on 1h)
        ema = bars_1h['close'].ewm(span=self.trend_ema).mean()
        trend = "BULL" if bars_1h['close'].iloc[-1] > ema.iloc[-1] else "BEAR"
        
        # 3. Calculate RSI
        delta = today_bars['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(self.rsi_period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(self.rsi_period).mean()
        rs = gain / loss.replace(0, 1e-10)
        rsi = 100 - (100 / (1 + rs))
        current_rsi = rsi.iloc[-1]
        
        # 4. Signal: price touches VWAP in trending market
        distance_to_vwap = abs(current_price - current_vwap) / current_vwap
        
        # Long: bullish trend + price pulls back to VWAP + RSI oversold
        if (trend == "BULL" and distance_to_vwap < self.touch_threshold 
                and current_price >= current_vwap * 0.999
                and current_rsi < self.rsi_oversold):
            setup_id = f"VWAP_LONG_{ticker}_{today_bars.index[-1]}"
            if setup_id not in self._seen_setups:
                self._seen_setups.add(setup_id)
                atr = _calc_atr(today_bars, 14)
                signals.append(Signal(
                    signal_type="VWAP_REVERT_LONG",
                    direction="LONG",
                    entry_price=current_price,
                    sl=current_vwap - atr,       # SL below VWAP by 1 ATR
                    tp=current_price + 2 * atr,  # TP at 2 ATR (2:1 R:R)
                    setup_id=setup_id,
                    ticker=ticker,
                    strategy_name="vwap_revert",
                    confidence=0.65,
                    details={"vwap": current_vwap, "rsi": current_rsi,
                             "trend": trend, "atr": atr}
                ))
        
        # Short: bearish trend + price rallies to VWAP + RSI overbought
        if (trend == "BEAR" and distance_to_vwap < self.touch_threshold
                and current_price <= current_vwap * 1.001
                and current_rsi > self.rsi_overbought):
            setup_id = f"VWAP_SHORT_{ticker}_{today_bars.index[-1]}"
            if setup_id not in self._seen_setups:
                self._seen_setups.add(setup_id)
                atr = _calc_atr(today_bars, 14)
                signals.append(Signal(
                    signal_type="VWAP_REVERT_SHORT",
                    direction="SHORT",
                    entry_price=current_price,
                    sl=current_vwap + atr,
                    tp=current_price - 2 * atr,
                    setup_id=setup_id,
                    ticker=ticker,
                    strategy_name="vwap_revert",
                    confidence=0.65,
                    details={"vwap": current_vwap, "rsi": current_rsi,
                             "trend": trend, "atr": atr}
                ))
        
        return signals
```

### Why VWAP Reversion Complements ICT and ORB

| Aspect | ICT | ORB | VWAP Reversion |
|--------|-----|-----|----------------|
| Market type | Any | Strong open | Trending with pullbacks |
| Entry style | Confirmation chain | Range breakout | Mean reversion |
| Risk:Reward | Variable | 1:1 | 2:1 |
| Signal frequency | Multiple per day | 1 per day | Multiple per day |
| Best time | 7-9 AM PT | First 15-60 min | Mid-morning |

---

## Scanner Integration

### Current Scanner (single strategy)

```python
# scanner.py (current)
class Scanner:
    def __init__(self, ...):
        self.signal_engine = SignalEngine(ticker)  # ICT only
```

### New Scanner (multi-strategy)

```python
# scanner.py (new)
class Scanner:
    def __init__(self, client, exit_manager, ticker, strategies=None):
        # Load enabled strategies from DB settings
        self.strategies = strategies or self._load_strategies()
        self.trade_manager = TradeEntryManager(client, exit_manager, ticker)
    
    def _load_strategies(self) -> list:
        """Load enabled strategies from settings table."""
        strategies = []
        # Check which strategies are enabled
        if config_get("STRATEGY_ICT_ENABLED", True):
            from strategy.ict_strategy import ICTStrategy
            strategies.append(ICTStrategy(self.ticker))
        if config_get("STRATEGY_ORB_ENABLED", False):
            from strategy.orb_strategy import ORBStrategy
            strategies.append(ORBStrategy())
        if config_get("STRATEGY_VWAP_ENABLED", False):
            from strategy.vwap_strategy import VWAPStrategy
            strategies.append(VWAPStrategy())
        return strategies
    
    def _scan(self):
        bars_1m, bars_1h, bars_4h = self._fetch_bars()
        levels = compute_levels(bars_1h)
        
        # Run ALL enabled strategies
        all_signals = []
        for strategy in self.strategies:
            try:
                signals = strategy.detect(bars_1m, bars_1h, bars_4h, levels, self.ticker)
                all_signals.extend(signals)
            except Exception as e:
                log.error(f"[{self.ticker}] Strategy {strategy.name} failed: {e}")
        
        # Process signals (same trade entry logic for all strategies)
        for signal in all_signals:
            trade = self.trade_manager.enter(signal)
            if trade:
                log.info(f"[{self.ticker}] {signal.strategy_name}: {signal.signal_type}")
```

---

## Database Changes

### Settings — Strategy Enable/Disable

```sql
INSERT INTO settings (category, key, value, data_type, description) VALUES
  ('strategy', 'STRATEGY_ICT_ENABLED', 'true', 'bool', 'Enable ICT strategy scanner'),
  ('strategy', 'STRATEGY_ORB_ENABLED', 'false', 'bool', 'Enable Opening Range Breakout scanner'),
  ('strategy', 'STRATEGY_VWAP_ENABLED', 'false', 'bool', 'Enable VWAP Mean Reversion scanner'),
  ('strategy', 'ORB_RANGE_MINUTES', '15', 'int', 'ORB opening range period (5/15/30/60)'),
  ('strategy', 'VWAP_RSI_PERIOD', '14', 'int', 'VWAP strategy RSI period'),
  ('strategy', 'VWAP_RSI_OVERSOLD', '35', 'int', 'VWAP RSI oversold threshold'),
  ('strategy', 'VWAP_RSI_OVERBOUGHT', '65', 'int', 'VWAP RSI overbought threshold');
```

### Trades Table — Strategy Tracking

The existing `signal_type` column already captures the strategy (e.g., `LONG_iFVG`, 
`ORB_BREAKOUT_LONG`). Add a `strategy_name` column for easier filtering:

```sql
ALTER TABLE trades ADD COLUMN strategy_name VARCHAR(30) DEFAULT 'ict';
```

---

## Dashboard — Strategy Configuration

```
┌─── Settings Tab → Strategy Section ─────────────────────────┐
│                                                              │
│  Enabled Strategies:                                         │
│  [✓] ICT (Inner Circle Trader)      [Configure]             │
│  [ ] ORB (Opening Range Breakout)   [Configure]             │
│  [ ] VWAP (Mean Reversion)          [Configure]             │
│  [ ] Custom Strategy                [Upload]                 │
│                                                              │
│  ORB Configuration:                                          │
│  ┌──────────────────────────────────────────────────┐       │
│  │ Range Period:    [15 ▾] minutes                   │       │
│  │ Breakout Buffer: [0.1%]                           │       │
│  │ Max Trades/Day:  [2  ]                            │       │
│  └──────────────────────────────────────────────────┘       │
│                                                              │
│  VWAP Configuration:                                         │
│  ┌──────────────────────────────────────────────────┐       │
│  │ RSI Period:      [14 ]                            │       │
│  │ RSI Oversold:    [35 ]                            │       │
│  │ RSI Overbought:  [65 ]                            │       │
│  │ EMA Trend:       [20 ] period                     │       │
│  └──────────────────────────────────────────────────┘       │
└──────────────────────────────────────────────────────────────┘
```

---

## Implementation Order

1. **BaseStrategy interface** — `strategy/base_strategy.py`
2. **Wrap existing ICT** — `strategy/ict_strategy.py` implements BaseStrategy
3. **Update scanner** — accept multiple strategies
4. **ORB strategy** — `strategy/orb_strategy.py`
5. **VWAP strategy** — `strategy/vwap_strategy.py`
6. **Settings** — strategy enable/disable in dashboard
7. **Analytics** — filter by strategy_name in charts
8. **Backtest** — strategy selector in backtest config

---

## Benefits

1. **Diversification** — Multiple uncorrelated strategies reduce drawdowns
2. **Market adaptability** — ICT works in ranging, ORB in trending, VWAP in pullbacks
3. **Easy experimentation** — Enable/disable strategies from dashboard, no code changes
4. **Backtesting** — Compare strategies head-to-head on same data
5. **Community extensibility** — Share strategies as plugins
6. **Risk management** — Different strategies can have different position sizes

---

## Sources

- [QuantifiedStrategies: Opening Range Breakout Backtest](https://www.quantifiedstrategies.com/opening-range-breakout-strategy/)
- [Options Cafe: 0DTE ORB Strategy Backtested](https://options.cafe/blog/0dte-opening-range-breakout-strategy-spy-backtested-results/)
- [TradeSwing: ORB Strategy Up 400%](https://tradethatswing.com/opening-range-breakout-strategy-up-400-this-year/)
- [QuantifiedStrategies: VWAP Trading Strategy](https://www.quantifiedstrategies.com/vwap-trading-strategy/)
- [QuantVPS: VWAP Strategy Python Backtest](https://www.quantvps.com/blog/backtest-vwap-trading-strategy-python)
- [GitHub: VwapProject (ES/NQ Futures)](https://github.com/hedge0/VwapProject)
- [Reddit: 0DTE ORB on SPY - 303 Trades Backtested](https://www.reddit.com/r/options/comments/1rkx5vr/0dte_opening_range_breakout_strategy_on_spy_full/)
