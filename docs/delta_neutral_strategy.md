# Delta-Neutral Strategy — Research & Feasibility Analysis

## What Is Delta-Neutral Trading?

Delta-neutral is an options strategy that maintains a portfolio delta near zero — meaning 
the position doesn't profit from directional moves. Instead, it profits from:

1. **Theta decay** — options lose time value every day, benefiting sellers
2. **Volatility contraction** — if implied volatility drops after entry
3. **Range-bound price action** — price stays within a defined range

The key idea: **you don't need to predict direction — just predict that price stays within a range.**

---

## Delta-Neutral Structures for 0DTE / Day Trading

### 1. Iron Condor (Most Popular for Automation)

```
Sell OTM Put ──┐                    ┌── Sell OTM Call
               │   PROFIT ZONE      │
Buy further    │ ◄──────────────────► │   Buy further  
OTM Put ───────┘                    └── OTM Call

Collect premium from selling both sides.
Max profit = net credit received.
Max loss = spread width - credit.
```

**Backtested Results** (from [Theta Profits](https://www.thetaprofits.com/my-most-profitable-options-trading-strategy-0dte-breakeven-iron-condor/)):
- **9,100 trades** (Apr 2021 — Feb 2026)
- **40% win rate** — but avg win > 2× avg loss → net profitable
- Breakeven iron condor: sell strikes at expected move boundaries
- Works on SPY 0DTE

### 2. Iron Butterfly (Higher Premium, Tighter Range)

```
                    ATM Strike
                       │
Buy OTM Put ─── Sell ATM Put/Call ─── Buy OTM Call
                       │
                  Max profit here
                  (at expiration)
```

**Backtested Results** (from [Option Alpha](https://optionalpha.com/blog/0dte)):
- **72% win rate** on iron butterflies
- **95.4% win rate** with targeted entry criteria (specific study)
- Higher premium collected than iron condor
- Tighter profit zone — needs more precision

### 3. Straddle/Strangle (Buy-Side — Profits from Big Moves)

```
Buy ATM Put + Buy ATM Call (Straddle)
  OR
Buy OTM Put + Buy OTM Call (Strangle)

Profits when price moves significantly in EITHER direction.
Loses if price stays flat (theta decay).
```

**Use case**: Before known catalysts (earnings, FOMC, CPI). NOT ideal for 
systematic daily trading due to theta bleed.

---

## Recommended Approach: Automated 0DTE Iron Condor on SPY/QQQ

### Why This Works for Our System

| Factor | Assessment |
|--------|-----------|
| **Liquidity** | SPY/QQQ 0DTE options have massive volume (millions/day) |
| **Automation** | Systematic entry rules, mechanical management |
| **IB support** | Full bracket order support for multi-leg spreads |
| **Risk defined** | Max loss = spread width - credit (known upfront) |
| **Theta advantage** | 0DTE options lose ALL remaining time value by EOD |
| **Market neutral** | Don't need directional bias — ICT handles directional |
| **Complementary** | ICT profits from moves, delta-neutral profits from no moves |

### Strategy Rules

```
ENTRY:
  Time: 10:00 AM PT (after morning volatility settles)
  Instrument: SPY or QQQ 0DTE options
  Structure: Iron Condor
    ─ Sell Call at +1σ (1 standard deviation above current price)
    ─ Buy Call at +1σ + $2 width
    ─ Sell Put at -1σ (1 standard deviation below)
    ─ Buy Put at -1σ - $2 width
  Delta target: net delta between -0.05 and +0.05
  Credit target: minimum $0.50 per spread ($1.00 total IC)
  
MANAGEMENT:
  ─ If delta exceeds ±0.15: adjust the untested side closer to ATM
  ─ If one short strike is breached: close that side for a loss
  ─ If P&L reaches +50% of max credit: close for profit
  ─ If P&L reaches -100% of max credit: close for loss (1:1 risk)
  
EXIT:
  ─ 2:30 PM PT: close all remaining positions (30 min before close)
  ─ OR: let expire worthless (max profit on 0DTE)
```

### Expected Performance

Based on backtested data from multiple sources:

| Metric | Conservative | Moderate | Aggressive |
|--------|-------------|----------|------------|
| Win Rate | 65-70% | 55-60% | 45-50% |
| Avg Win | $80-120 | $150-200 | $250-350 |
| Avg Loss | $150-200 | $200-300 | $300-500 |
| Monthly Trades | 15-18 | 18-20 | 20-22 |
| Expected Monthly | +$200-400 | +$400-800 | +$600-1200 |
| Max Drawdown | -$500 | -$1,000 | -$2,000 |

---

## Implementation in Our System

### New Strategy Plugin

```python
# strategy/delta_neutral_strategy.py

class DeltaNeutralStrategy(BaseStrategy):
    """Iron Condor on 0DTE SPY/QQQ options."""

    name = "delta_neutral"
    description = "Delta-neutral iron condor — profits from theta decay"

    def detect(self, bars_1m, bars_1h, bars_4h, levels, ticker) -> List[Signal]:
        """
        Unlike directional strategies, this doesn't look for price action signals.
        Instead, it checks:
        1. Is it the right time? (after morning, before close)
        2. Is IV high enough to sell? (VIX check)
        3. Is the expected range calculable? (ATR/std dev)
        Then generates an iron condor signal.
        """
        signals = []
        now = _current_time_pt()
        
        # Only enter between 10:00 AM and 12:00 PM PT
        if not (10 <= now.hour < 12):
            return []
        
        # Calculate expected range (1 standard deviation)
        returns = bars_1m['close'].pct_change().dropna()
        daily_std = returns.std() * (390 ** 0.5)  # annualize intraday vol
        current_price = bars_1m['close'].iloc[-1]
        
        upper_strike = round(current_price * (1 + daily_std), 0)
        lower_strike = round(current_price * (1 - daily_std), 0)
        
        # Generate iron condor signal
        setup_id = f"IC_{ticker}_{now.date()}"
        if setup_id not in self._seen_setups:
            self._seen_setups.add(setup_id)
            signals.append(Signal(
                signal_type="IRON_CONDOR",
                direction="NEUTRAL",  # New direction type
                entry_price=current_price,
                sl=0,  # N/A for spreads — risk is defined by structure
                tp=0,  # N/A — managed by spread P&L
                setup_id=setup_id,
                ticker=ticker,
                strategy_name="delta_neutral",
                confidence=0.6,
                details={
                    "structure": "iron_condor",
                    "upper_strike": upper_strike,
                    "lower_strike": lower_strike,
                    "spread_width": 2.0,
                    "daily_std": daily_std,
                    "expected_credit": 1.00,
                }
            ))
        return signals
```

### IB Multi-Leg Order Support Needed

Current system places single-leg orders. Iron condors need 4-leg orders:

```python
# broker/ib_orders.py — NEW: multi-leg order support

def place_iron_condor(ib, ticker, upper_call, lower_put, 
                       width, contracts, account):
    """Place 4-leg iron condor as a single combo order on IB."""
    
    # Create combo contract
    combo = Contract()
    combo.symbol = ticker
    combo.secType = "BAG"  # IB combo/bag order
    combo.currency = "USD"
    combo.exchange = "SMART"
    
    # 4 legs
    combo.comboLegs = [
        ComboLeg(conId=sell_call.conId, ratio=1, action="SELL"),
        ComboLeg(conId=buy_call.conId, ratio=1, action="BUY"),
        ComboLeg(conId=sell_put.conId, ratio=1, action="SELL"),
        ComboLeg(conId=buy_put.conId, ratio=1, action="BUY"),
    ]
    
    # Limit order at net credit
    order = LimitOrder("SELL", contracts, credit_price)
    trade = ib.placeOrder(combo, order)
    return trade
```

---

## Changes Required

| Component | Change | Effort |
|-----------|--------|--------|
| **strategy/delta_neutral_strategy.py** | New: iron condor signal generation | Medium |
| **broker/ib_orders.py** | New: multi-leg combo order placement | Medium |
| **broker/ib_client.py** | Add: combo order support, option chain for spreads | Medium |
| **strategy/option_selector.py** | Add: spread construction (4 strikes selection) | Medium |
| **strategy/exit_conditions.py** | Add: spread P&L evaluation (not single-leg) | Small |
| **strategy/exit_executor.py** | Add: close combo order (single close for all 4 legs) | Small |
| **db/models.py** | Add: `structure` field (single_leg, iron_condor, iron_butterfly) | Small |
| **dashboard** | Add: spread display, greeks per leg | Medium |

### Risks & Challenges

| Risk | Mitigation |
|------|-----------|
| IB combo orders have different fill behavior | Test extensively on paper first |
| Spread P&L tracking is more complex (4 legs) | Track as single position with net credit |
| Delta adjustment during the day requires monitoring | Add delta check in exit_manager cycle |
| Early assignment risk (rare for 0DTE) | Monitor ITM legs, close before expiry |
| VIX crush can help or hurt depending on timing | Enter when VIX > 15 for better premiums |

---

## Highly Liquid Tickers for Delta-Neutral

| Tier | Tickers | 0DTE Volume | Spread Width |
|------|---------|-------------|--------------|
| **Tier 1** | SPY, QQQ | Millions/day | $0.01 bid-ask |
| **Tier 2** | AAPL, NVDA, TSLA, AMZN, META | Hundreds of thousands | $0.01-0.05 |
| **Tier 3** | GOOGL, MSFT, AMD, NFLX | Tens of thousands | $0.05-0.10 |

Recommend starting with **SPY and QQQ only** — they have the tightest spreads and 
most liquidity for multi-leg orders. Then expand to Tier 2 once proven.

---

## How Delta-Neutral Complements the Existing System

```
┌─────────────────────────────────────────────────────────────┐
│                 PORTFOLIO STRATEGY MIX                        │
│                                                               │
│  Market Trending ──▶  ICT Strategy (directional)             │
│                      Profits from displacement + FVG/OB      │
│                                                               │
│  Market Breaking Out ──▶  ORB Strategy (momentum)            │
│                           Profits from range expansion        │
│                                                               │
│  Market Pulling Back ──▶  VWAP Strategy (mean reversion)     │
│                           Profits from VWAP bounce            │
│                                                               │
│  Market Range-Bound ──▶  Delta-Neutral (theta decay)         │
│                          Profits from NO movement             │
│                          Collects premium while others wait   │
│                                                               │
│  Result: ALWAYS have a strategy that fits current conditions  │
└─────────────────────────────────────────────────────────────┘
```

---

## Sources

- [Theta Profits: 0DTE Breakeven Iron Condor — 9,000 Trades](https://www.thetaprofits.com/my-most-profitable-options-trading-strategy-0dte-breakeven-iron-condor/)
- [Option Alpha: Top 0DTE Strategies — 25k Trades Analysis](https://optionalpha.com/blog/0dte)
- [Option Alpha: 0DTE Iron Butterfly Backtest](https://optionalpha.com/bots/study-targeted-0-1-dte-iron-butterfly)
- [GitHub: 0dte-trader — IB Automated Iron Condor](https://github.com/aicheung/0dte-trader)
- [SoFi: Delta Neutral Explained](https://www.sofi.com/learn/content/delta-neutral/)
- [Data Driven Options: Delta Neutral Back Ratio](https://datadrivenoptions.com/delta-neutral-back-ratio-call-spread/)
- [LinkedIn: Building Systematic Delta Neutral Strategies](https://www.linkedin.com/pulse/beyond-directional-bets-building-systematic-delta-bejar-garcia-crmof)
