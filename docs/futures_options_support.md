# Futures Options Support — Design Document

## Purpose

Extend the ICT trading bot to support options on futures contracts (MNQ, NQ, ES, MES, 
Gold/GC, Oil/CL). Currently the bot only trades equity options (QQQ, SPY, AAPL, etc.). 
Futures options have different IB contract specifications, exchange routing, margin 
requirements, and expiration cycles that need to be handled.

---

## Supported Instruments

### Micro/Mini Futures Options

| Symbol | Underlying | Exchange | Multiplier | Description |
|--------|-----------|----------|------------|-------------|
| **MNQ** | Micro E-mini Nasdaq-100 | CME/GLOBEX | $2 | Micro NQ options |
| **NQ** | E-mini Nasdaq-100 | CME/GLOBEX | $20 | Full NQ options |
| **MES** | Micro E-mini S&P 500 | CME/GLOBEX | $5 | Micro ES options |
| **ES** | E-mini S&P 500 | CME/GLOBEX | $50 | Full ES options |
| **GC** | Gold Futures | COMEX/NYMEX | $100 | Gold options |
| **MGC** | Micro Gold | COMEX/NYMEX | $10 | Micro gold options |
| **CL** | Crude Oil | NYMEX | $1,000 | Oil options |
| **MCL** | Micro Crude Oil | NYMEX | $100 | Micro oil options |

### Key Differences from Equity Options

| Aspect | Equity Options | Futures Options |
|--------|---------------|-----------------|
| **secType** | `OPT` | `FOP` (Futures Option) |
| **Exchange** | SMART, AMEX, CBOE | GLOBEX, CME, NYMEX, COMEX |
| **Underlying** | Stock/ETF | Future contract (e.g., NQM6) |
| **Multiplier** | 100 (always) | Varies by contract (2, 5, 20, 50, 100, 1000) |
| **Expiration** | Daily (0DTE), weekly, monthly | Weekly (Weds), monthly, quarterly |
| **Strike format** | Dollars (e.g., $634.00) | Points/dollars (e.g., 21500) |
| **Trading hours** | 9:30 AM - 4:00 PM ET | Near 24 hours (Sun-Fri) |
| **Settlement** | Cash or physical delivery | Cash settled |
| **Margin** | Reg-T margin | SPAN margin (different calculation) |
| **Symbol format** | OCC (QQQ260415C00634000) | Different (NQ FOP GLOBEX ...) |

---

## Architecture Changes

### What Changes

```
┌─────────────────────────────────────────────────────────────────┐
│                    COMPONENT IMPACT ANALYSIS                     │
│                                                                   │
│  MUST CHANGE:                                                    │
│  ├── broker/ib_contracts.py  — FOP contract creation + routing   │
│  ├── broker/ib_client.py     — Order placement for FOP           │
│  ├── config.py               — Futures-specific configuration    │
│  ├── db/models.py            — Add instrument_type, multiplier   │
│  ├── strategy/option_selector.py — Futures option selection      │
│  └── utils/occ_parser.py     — Support FOP symbol format         │
│                                                                   │
│  MINOR CHANGES:                                                  │
│  ├── strategy/scanner.py     — Different trading hours           │
│  ├── strategy/exit_manager.py — Multiplier-aware P&L             │
│  ├── strategy/exit_conditions.py — Same logic, different params  │
│  ├── dashboard (various)     — Display futures options            │
│  └── reconciliation.py       — Handle FOP positions              │
│                                                                   │
│  NO CHANGE (reuse as-is):                                        │
│  ├── strategy/signal_engine.py    — Works on any price bars      │
│  ├── strategy/ict_long.py         — Pure price action logic      │
│  ├── strategy/ict_short.py        — Pure price action logic      │
│  ├── strategy/levels.py           — Support/resistance            │
│  ├── strategy/exit_executor.py    — Close flow is generic        │
│  ├── db/writer.py                 — Generic trade operations     │
│  └── strategy/error_handler.py    — Generic                      │
└─────────────────────────────────────────────────────────────────┘
```

### Key Insight: Most Code Is Instrument-Agnostic

The ICT strategy (signal_engine, ict_long, ict_short, levels) operates on price bars — 
it doesn't care whether those bars come from QQQ or NQ futures. The signal detection 
is 100% reusable.

The changes are concentrated in:
1. **Contract creation** — how we build and qualify IB contracts (FOP vs OPT)
2. **P&L calculation** — different multipliers per instrument
3. **Trading hours** — futures trade nearly 24 hours
4. **Data provider** — fetching futures price data

---

## Database Changes

### Tickers Table — Add Instrument Type

```sql
ALTER TABLE tickers ADD COLUMN instrument_type VARCHAR(10) DEFAULT 'equity';
  -- 'equity' = stock/ETF options (current)
  -- 'futures' = futures options (new)

ALTER TABLE tickers ADD COLUMN multiplier INT DEFAULT 100;
  -- 100 for equity options
  -- varies for futures: 2 (MNQ), 20 (NQ), 5 (MES), 50 (ES), etc.

ALTER TABLE tickers ADD COLUMN exchange VARCHAR(20) DEFAULT 'SMART';
  -- 'SMART' for equities
  -- 'GLOBEX' for NQ/ES/MNQ/MES
  -- 'NYMEX' for CL/MCL
  -- 'COMEX' for GC/MGC

ALTER TABLE tickers ADD COLUMN futures_expiry VARCHAR(10);
  -- Current front month: '202406' (Jun 2026)
  -- NULL for equities
```

### Trades Table — Add Multiplier

```sql
ALTER TABLE trades ADD COLUMN multiplier INT DEFAULT 100;
  -- Stored per trade so P&L calculation is always correct
  -- even if ticker config changes later
```

---

## Contract Creation Flow

### Current (Equity Options)

```python
# broker/ib_contracts.py
contract = Option(ticker, expiry, strike, right, exchange)
# e.g., Option("QQQ", "20260415", 634.0, "C", "SMART")
```

### New (Futures Options)

```python
# broker/ib_contracts.py
from ib_async import FuturesOption, Future

# Step 1: Get the underlying futures contract
future = Future(symbol="NQ", exchange="CME", currency="USD")
ib.qualifyContracts(future)
# This gives us the front month: NQM6 (June 2026)

# Step 2: Get the futures option chain
chains = ib.reqSecDefOptParams(
    underlyingSymbol="NQ",
    futFopExchange="GLOBEX",
    underlyingSecType="FUT",
    underlyingConId=future.conId
)

# Step 3: Create the futures option contract
fop = FuturesOption(
    symbol="NQ",
    lastTradeDateOrContractMonth="20260417",
    strike=21500,
    right="C",
    exchange="GLOBEX",
    multiplier="20"
)
ib.qualifyContracts(fop)
```

### Unified Contract Factory

```python
# broker/ib_contracts.py — new function

def create_option_contract(ib, ticker_config: dict) -> tuple:
    """
    Create the appropriate option contract based on instrument type.
    Returns (contract, chain, price) or raises RuntimeError.
    
    ticker_config from DB tickers table:
      instrument_type: 'equity' or 'futures'
      symbol: 'QQQ' or 'NQ'
      exchange: 'SMART' or 'GLOBEX'
      multiplier: 100 or 20
      futures_expiry: None or '202406'
    """
    if ticker_config['instrument_type'] == 'futures':
        return _create_futures_option(ib, ticker_config)
    else:
        return _create_equity_option(ib, ticker_config)
```

---

## P&L Calculation Changes

### Current (Equity — Always ×100)

```python
pnl_usd = (exit_price - entry_price) * 100 * contracts
```

### New (Multiplier-Aware)

```python
multiplier = trade.get("multiplier", 100)
pnl_usd = (exit_price - entry_price) * multiplier * contracts
```

**Files to update:**
- `db/writer.py` — `close_trade()`, `finalize_close()`, `update_trade_price()`
- `strategy/exit_manager.py` — `_check_exits()` P&L calculation
- `strategy/trade_logger.py` — CSV P&L calculation
- `dashboard/routes/analytics.py` — Analytics views (or handle in SQL views)
- `db/analytics_views.sql` — Use `COALESCE(t.multiplier, 100)` in risk_capital calc

---

## Trading Hours

### Current (Equity — Fixed Window)

```python
# config.py
TRADE_WINDOW_START_PT = 7   # 7:00 AM PT
TRADE_WINDOW_END_PT = 9     # 9:00 AM PT
MARKET_OPEN_PT = 6           # 6:30 AM PT
MARKET_CLOSE_PT = 13         # 1:00 PM PT
```

### New (Per-Instrument)

```python
# Move to tickers table or settings
TRADING_HOURS = {
    "equity": {"open": (6, 30), "close": (13, 0), "window": (7, 0, 9, 0)},
    "futures": {"open": (0, 0), "close": (23, 0), "window": (6, 0, 10, 0)},
    # Futures trade Sun 6pm – Fri 5pm ET (nearly 24h)
    # ICT window for futures: 6am-10am PT (includes London + NY sessions)
}
```

### Scanner Impact

`scanner.py` `_check_windows()` needs to read trading hours from the ticker config 
instead of using hardcoded values:

```python
def _check_windows(self):
    ticker_config = get_ticker_config(self.ticker)  # from DB
    if ticker_config['instrument_type'] == 'futures':
        # Futures: nearly 24-hour trading
        in_market = True  # Always in market (Mon-Fri)
        in_trade_window = check_futures_window(now_pt)
    else:
        # Equity: current logic
        in_market = check_equity_market(now_pt)
        in_trade_window = check_equity_window(now_pt)
```

---

## Data Provider

### Current (Equity)

```python
# data/ib_provider.py — uses IB reqHistoricalData for equity bars
bars = get_bars_1m_ib(client, "QQQ", days_back=5)
```

### New (Futures)

Same IB API, different contract type:

```python
# For futures: use the continuous futures contract
from ib_async import ContFuture

contract = ContFuture("NQ", "CME")
ib.qualifyContracts(contract)
bars = ib.reqHistoricalData(
    contract, endDateTime='', durationStr='5 D',
    barSizeSetting='1 min', whatToShow='TRADES',
    useRTH=False  # Include extended hours for futures
)
```

---

## Option Selector Changes

### Current (`strategy/option_selector.py`)

```python
def select_and_enter(client, ticker):
    option_symbol = client.get_atm_call_symbol(ticker)
    # ... validates, gets price, places bracket order
```

### New (Instrument-Aware)

```python
def select_and_enter(client, ticker):
    ticker_config = get_ticker_config(ticker)
    
    if ticker_config['instrument_type'] == 'futures':
        option_symbol = client.get_atm_futures_call(ticker, ticker_config)
    else:
        option_symbol = client.get_atm_call_symbol(ticker)
    
    # Rest of flow is the same — validate, price, bracket order
    # P&L targets use multiplier from ticker_config
```

---

## Implementation Order

1. **DB schema changes** — Add columns to tickers and trades tables
2. **Config/tickers** — Seed futures tickers with correct exchange/multiplier
3. **Contract creation** — FOP contract factory in ib_contracts.py  
4. **Data provider** — Futures bar fetching via IB ContFuture
5. **P&L calculation** — Multiplier-aware across all files
6. **Trading hours** — Per-instrument window in scanner
7. **Option selector** — Futures option chain + strike selection
8. **Dashboard** — Display futures options, multiplier in trade table
9. **Testing** — Paper trade futures options end-to-end

---

## Risk Considerations

| Risk | Mitigation |
|------|-----------|
| Futures options have less liquidity → wider spreads | Add spread check before entry |
| Different margin requirements (SPAN) | Monitor account margin, don't exceed |
| Nearly 24-hour trading → bot must handle overnight | Add session awareness, don't trade during maintenance (5pm-6pm ET) |
| Futures expiration cycles are different | Use front month, auto-roll at expiry |
| Higher notional value per contract | Use micro contracts (MNQ, MES) for testing |
| CME data requires separate IB subscription | Verify user has CME market data enabled |

---

## Example Ticker Configuration

```sql
-- Seed futures tickers
INSERT INTO tickers (symbol, name, is_active, contracts, instrument_type, multiplier, exchange, futures_expiry)
VALUES
  ('MNQ', 'Micro E-mini Nasdaq 100', true, 2, 'futures', 2, 'GLOBEX', '202406'),
  ('NQ', 'E-mini Nasdaq 100', false, 1, 'futures', 20, 'GLOBEX', '202406'),
  ('MES', 'Micro E-mini S&P 500', true, 2, 'futures', 5, 'GLOBEX', '202406'),
  ('ES', 'E-mini S&P 500', false, 1, 'futures', 50, 'GLOBEX', '202406'),
  ('GC', 'Gold Futures', false, 1, 'futures', 100, 'COMEX', '202406'),
  ('MGC', 'Micro Gold', true, 2, 'futures', 10, 'COMEX', '202406'),
  ('CL', 'Crude Oil', false, 1, 'futures', 1000, 'NYMEX', '202407'),
  ('MCL', 'Micro Crude Oil', true, 2, 'futures', 100, 'NYMEX', '202407');
```

---

## Benefits

1. **Diversification** — Trade across asset classes (equities + futures)
2. **Extended hours** — Futures trade nearly 24/5, capture more ICT setups
3. **Tax advantages** — Futures options have 60/40 tax treatment (Section 1256)
4. **Leverage** — Micro contracts provide efficient capital usage
5. **Correlation** — NQ options vs QQQ options: same underlying, different mechanics
6. **ICT compatibility** — ICT strategy works on futures charts natively
