# Live FOP (Futures-Options) Trading — Design Doc

**Status:** planning
**Target branch:** new, spun off after `feature/profitability-research` merges
**Effort estimate:** 1–2 weeks
**Blocker for merge:** none — this is net-new work

---

## 1. Today's state

### What works
- **Backtest side** — `backtest_engine/data_provider_ib.py` pulls IB
  historical bars for FOP contracts (unit-tested, QQQ stock passed a
  real-IB smoke).
- `FOP_SPECS` (in `broker/ib_contracts.py`) — per-underlying lookup of
  exchange, multiplier, currency, strike interval for the major contracts:
  MNQ, NQ, MES, ES, GC, MGC, CL.
- `spec_from_ticker_row()` helper turns a `tickers` row into a build
  spec for `FuturesOption(...)`.
- `tickers` DB table has the 6 FOP rows seeded (CL, ES, GC, MES, MNQ,
  NQ), all with `is_active=FALSE`.

### What does NOT work (and what this doc is about)
- **Live scanner** has zero dispatch on `sec_type` — it treats every
  `tickers` row as equity and runs ICT against 1-min bars fetched via
  yfinance/IB stock contracts.
- **`option_selector`** (`select_and_enter` / `select_and_enter_put`)
  builds an `Option(ticker, expiry, strike, right, "SMART")` only.
  No branch that builds a `FuturesOption` from `FOP_SPECS`.
- **`broker/ib_client.place_bracket_order`** takes an OCC string, which
  is equity-option format. Futures options don't have an OCC symbol
  identical to equity; they have their own `localSymbol` format (e.g.
  `MNQM6 P20000` — root + month code + right + strike).
- **`broker/ib_contracts.ib_occ_to_contract`** regex
  `^([A-Z]+)(\d{6})([CP])(\d{8})$` is equity-only; won't parse FOP.
- **Exit path** (cancel, close, verify) queries IB positions and
  matches by `conId`, which is fine in principle, but every localSymbol
  read / contract rebuild currently assumes equity format.
- **Reconciliation** will see FOP positions as "orphans" and attempt
  to adopt them with `secType="OPT"` — that fails to qualify, and the
  trade lands in the DB with a broken contract handle.

Bottom line: **the scanner, the entry path, the order placement, and
the reconcile path all have OPT assumptions baked in.**  Live FOP is
a feature, not a flag.

## 2. Goals & non-goals

### Goals
- One end-to-end live trade on a MICRO futures option (MNQ or MES) via
  the same signal → entry → monitor → exit pipeline used for equities
  today.
- Same audit trail guarantees: every state transition writes a
  `log_trade_action` row.
- Same roll/close safety: Fix A/B/C must apply to FOP trades without
  modification.
- Same dashboard visibility: Trades tab, Audit modal, Threads page —
  no FOP-specific UI.

### Non-goals (this iteration)
- **Full futures-contract management** (rolling the underlying, margin
  simulation, overnight hold through settlement).
- **24-hour trading** — we trade FOP only during the same hours as
  equity options at first (13:30–20:00 UTC), skipping overnight.
- **Multi-leg spreads** — single-leg long calls / puts only, same as
  ICT today. Spreads are a separate design.
- **Automatic FOP signal discovery** (new strategies tuned for futures
  behavior). First version reuses ICT's signal logic on the underlying
  future's 1-min bars.
- **Production-volume sizing** — paper-account micros (MNQ, MES) only
  for the first live runs.

## 3. Architecture

### 3.1 The two key abstractions

```
                  ┌──────────────────────────────────┐
                  │      TickerConfig (new)          │
                  │  ──────────────────────           │
                  │  sec_type   : "OPT" | "FOP"      │
                  │  symbol     : "QQQ" | "MNQ"      │
                  │  exchange   : "SMART" | "GLOBEX" │
                  │  multiplier : 100 | 2 | 5 | 20   │
                  │  currency   : "USD"              │
                  └──────────────┬───────────────────┘
                                 │
                    produced by build_ticker_configs()
                    (reads tickers + strategies tables,
                     merges with FOP_SPECS defaults)
                                 │
                  ┌──────────────▼──────────────────┐
                  │   ContractBuilder (new)         │
                  │  ────────────────────           │
                  │  for_entry(cfg, signal, dte)    │
                  │    → Option(...)  if OPT        │
                  │    → FuturesOption(...) if FOP  │
                  │  from_position(pos)             │
                  │    → rebuild contract from IB   │
                  │       Position.contract         │
                  └─────────────────────────────────┘
```

Every place that today calls `Option(...)` or parses an OCC string
goes through `ContractBuilder` instead.

### 3.2 Dispatch points

| Component | Today (OPT-only) | New (OPT + FOP) |
|-----------|------------------|-----------------|
| `scanner.py` | Builds Stock contract for 1-min bars | If `cfg.sec_type=='FOP'`, uses `Future` for underlying bars via `data_provider_ib.fetch_bars_ib` |
| `option_selector.select_and_enter` | `Option(ticker, exp, strike, right, "SMART")` | `ContractBuilder.for_entry(cfg, signal, dte)` |
| `broker/ib_client.place_bracket_order` | Uses `_occ_to_contract(symbol)` (regex) | Pass `contract` directly OR accept `TickerConfig` + strike+right+expiry |
| `broker/ib_client._ib_get_positions_raw` | Records OCC `localSymbol` only | Records structured fields (`secType, symbol, expiry, strike, right, multiplier`) so the caller can rebuild |
| `strategy/reconciliation` adopt path | Builds `Option(...)` inline from position | Uses `ContractBuilder.from_position(pos)` |
| DB schema | `trades.ib_con_id` stored, symbol is OCC | Add `trades.sec_type` + `trades.expiry` + `trades.strike` + `trades.right` so we can always rebuild the contract without relying on localSymbol parsing |

### 3.3 Database changes

Add to `trades` table:

```sql
ALTER TABLE trades
  ADD COLUMN sec_type    VARCHAR(5)  NOT NULL DEFAULT 'OPT',
  ADD COLUMN expiry      VARCHAR(8),            -- YYYYMMDD or YYYYMM
  ADD COLUMN strike      NUMERIC(12,3),
  ADD COLUMN right        CHAR(1),              -- 'C' or 'P'
  ADD COLUMN multiplier  INTEGER NOT NULL DEFAULT 100,
  ADD COLUMN exchange    VARCHAR(20) NOT NULL DEFAULT 'SMART',
  ADD COLUMN currency    VARCHAR(3)  NOT NULL DEFAULT 'USD';
```

Purpose: stop relying on regex-parsing `symbol` to rebuild contracts.
Every entry writes these fields explicitly; every reconcile/close
reads them. OCC symbol stays for display / backward compat but becomes
optional for contract construction.

### 3.4 Scanner behavior

- Today: scanner assumes ticker is a stock. Pulls 1-min bars via
  `data/ib_provider.py` (`Stock(ticker, "SMART", "USD")`) and runs
  signal_engine on those bars.
- New: if the `tickers` row has `sec_type='FOP'`, the scanner builds
  a **Future contract** for the underlying (e.g. `MNQ Jun 2026`)
  and pulls its 1-min bars. The signal-engine logic is identical — it
  doesn't know or care whether the bars came from an equity or a
  future.
- The ONLY change to signal_engine is a brief sanity check: FOP
  underlying bars have different tick sizes and overnight gaps. The
  1h and 4h bars used for EMA bias may have fewer sessions of history.
  First-version: skip FOP scanners outside RTH (regular trading hours).

### 3.5 Entry path

Pseudo-code in `option_selector`:

```python
def select_and_enter(client, ticker_cfg: TickerConfig):
    signal = ...  # from scanner
    if ticker_cfg.sec_type == 'OPT':
        # existing equity path
        ...
    elif ticker_cfg.sec_type == 'FOP':
        spec = FOP_SPECS[ticker_cfg.symbol]
        # 1. Fetch current underlying price
        underlying_price = client.get_future_price(ticker_cfg.symbol, spec)
        # 2. Resolve expiry (nearest weekly or monthly FOP)
        expiry = resolve_fop_expiry(client, ticker_cfg.symbol,
                                     dte_target=ticker_cfg.dte_days)
        # 3. Pick ATM strike on the nearest-interval grid
        strike = round_to_grid(underlying_price, spec['strike_interval'])
        right = 'C' if signal.direction == 'LONG' else 'P'
        # 4. Build the contract via ContractBuilder, qualify via IB
        contract = ContractBuilder.for_entry(
            ticker_cfg, expiry=expiry, strike=strike, right=right)
        # 5. Place bracket — same API, no OCC symbol needed
        result = client.place_bracket_order_on_contract(
            contract, contracts=ticker_cfg.contracts,
            action='BUY', tp_price=..., sl_price=...)
        # 6. Write to DB with all structured fields (sec_type, expiry, etc.)
```

### 3.6 Exit / roll path

Once the trade row has `sec_type`, `expiry`, `strike`, `right`,
`multiplier`, `exchange` — the exit path uses `ContractBuilder.from_row()`
instead of the OCC regex. Nothing else needs to change:

- `cancel_all_orders_and_verify` → reqAllOpenOrders + match by conId
  (already cross-client-aware via Fix A)
- `_verify_close_on_ib` → polls `get_position_quantity(conId)` (conId
  works identically for FOP)
- Audit writes → unchanged

## 4. Reconcile behavior for FOP

Reconcile is already con_id-centric and doesn't really care about
OCC symbols for matching. The two changes needed:

1. **Adopt path** reads `position.contract.secType` and routes to
   `ContractBuilder.from_position()` — which for secType='FOP' writes
   the structured fields into the adopted trade row.
2. **Close path** uses `ContractBuilder.from_row()` to rebuild the
   contract for the RECONCILE-close sell order.

## 5. Risks and edge cases

### 5.1 Margin — not simulated today

ICT equity options: you pay debit × 100 × contracts; max loss = debit.
FOP: you pay premium × multiplier × contracts (for longs this is
still the max loss, same as equity options). **But** — IB applies
SPAN margin that moves with underlying price. A long call on MNQ at
$22,500 with the future at $22,000 ties up roughly $400 × 2 =
$800 of margin regardless of the premium paid. As long as we stay
long-only, max loss is still just the premium, but the margin
footprint is larger than an equity option of equivalent notional.

**Mitigation:** start with MICROS only (MNQ, MES) — margin is ~1/10
of the full-size contract. With the default `contracts=2` and
`ROLL_THRESHOLD=0.80`, a single trade ties up well under $500 of
margin on a MNQ position.

### 5.2 Trading hours

CME futures trade 23 hours. FOP liquidity is thin outside RTH. To
match equity-options behavior for the first version, add a trade
window guard that short-circuits FOP scanners outside
06:30–13:00 PT (RTH). Configurable per-ticker.

### 5.3 Strike grid

Equity options: strikes every $0.50 or $1 near ATM. FOP: grid varies
by product — MNQ is every $25; ES is every $5. `FOP_SPECS` already
records this; `round_to_grid()` uses it. The risk is picking a strike
on a grid that doesn't have a listed contract that day (rare but
possible) — need to retry on adjacent strikes the same way we do for
equity "ATM ± 1 tick" today.

### 5.4 Multi-expiry chains

A single underlying has weekly, monthly, quarterly FOPs available
simultaneously. We pick the nearest by `dte_target` (default 7 days).
`reqSecDefOptParams` returns all expirations at once, so we just
sort and pick.

## 6. Phased rollout

### Phase 1 — Scaffolding (no live orders)

1. DDL for the new `trades` columns (+ default values for backfill).
2. `ContractBuilder` with unit tests covering OPT + FOP construction
   and rebuild-from-position.
3. Update all `localSymbol`/OCC read sites to use
   `ContractBuilder.from_position()` (the 5 spots my `_normalize_occ`
   fix just touched).
4. Scanner dispatch: `sec_type='FOP'` → fetch Future bars via existing
   `data_provider_ib`. Confirm 1-min bars arrive + signal_engine runs
   without errors on MNQ.
5. `option_selector` FOP branch — resolves expiry + strike, builds
   contract, but RETURNS EARLY before placing any order
   (logs "FOP entry: would place 2x MNQ ...").

**Gate.** Regression green, bot can scan MNQ in `trade_mode='ALERT'`
(signal-only, no orders) across one market session, logs look right.
Zero live FOP orders.

### Phase 2 — First live micro

1. Enable ONE FOP ticker (MNQ) in paper account.
2. `is_active=TRUE`, `contracts=1` (not 2), `ROLL_THRESHOLD` locked at
   default (don't force rolls while also testing the code path).
3. Scanner runs, one trade places, monitor runs, natural close or
   manual close.
4. Audit trail shows `open` / `close:*` rows with FOP-shaped
   `details.sec_type='FOP'`.
5. Fix A/B/C still apply — straggler sweep must work on FuturesOption
   conIds (should — conId is contract-agnostic).

**Gate.** One clean live round-trip, then close the gate and validate
the audit trail before enabling more contracts.

### Phase 3 — Widen

1. MES added alongside MNQ.
2. `contracts=2` restored if micros prove stable at 1.
3. RTH window guard tightened based on observed fills.
4. Roll mechanics tested on FOP specifically (lower `ROLL_THRESHOLD`
   for one session).

### Phase 4 — Full-size contracts (optional)

Only after Phase 3 is stable across multiple weeks. ES + NQ full-size
have 10× the notional of micros; a single trade mistake is meaningful
money. Not on the near-term roadmap.

## 7. Testing strategy

### Unit tests (no IB)

- `ContractBuilder.for_entry(sec_type='OPT')` — identical to today
- `ContractBuilder.for_entry(sec_type='FOP')` — asserts
  FuturesOption(exchange='GLOBEX', multiplier=2) for MNQ, etc.
- `ContractBuilder.from_position(pos_fop)` — rebuild from
  IB Position mock
- `round_to_grid(22487, interval=25)` → 22500
- `resolve_fop_expiry(chain, dte=7)` → picks nearest

### Integration tests (mocked IB)

- Adopt path handles FOP position → writes trade row with
  `sec_type='FOP'`, `multiplier=2`, `strike=...`
- Exit path rebuilds FuturesOption from trade row → cancels bracket
  → sells → verifies close

### Live smoke (Phase 2 gate)

Single manual trade on MNQ in paper account, round-trip end-to-end.
Observe audit trail. Kill switch ready.

## 8. File map (planned)

| New | Purpose |
|-----|---------|
| `broker/contract_builder.py` | `ContractBuilder` class |
| `broker/fop_client.py` *(maybe)* | FOP-specific IB methods (`get_future_price`, `resolve_fop_expiry`) |
| `strategy/option_selector_fop.py` *(maybe)* | FOP branch of entry logic |
| `tests/unit/test_contract_builder.py` | Unit tests |
| `tests/integration/test_fop_live_flow.py` | Mocked-IB integration |
| `db/migrations/add_trade_contract_fields.sql` | DDL for new trades columns |
| `docs/fop_live_trading_design.md` | This doc |

| Modified | Change |
|----------|--------|
| `strategy/scanner.py` | Dispatch on `sec_type` |
| `strategy/option_selector.py` | Add FOP branch + use ContractBuilder |
| `broker/ib_client.py` | `place_bracket_order_on_contract()` variant; `_ib_get_positions_raw` returns structured fields; localSymbol reads via ContractBuilder |
| `strategy/reconciliation.py` | Adopt + close via ContractBuilder |
| `db/writer.py` | insert_trade + get_open_trades_from_db read new columns |
| `tickers` DB rows | `is_active=TRUE` for MNQ in Phase 2 |

## 9. Open questions

1. **Signal engine on futures.** ICT's VWAP / session-open / liquidity-raid
   concepts are equity-session-centric. Does running them on 24h future
   bars produce noisy signals? Phase 1 will surface this empirically.
2. **Commission model.** Futures options commissions are per-contract
   per-side (~$0.85-2.50 depending on product). The engine's
   `commission_per_contract` already handles this but may need
   per-product values.
3. **Settlement.** Some FOP settle to cash, some to the underlying
   future on expiry. For the 7-DTE default, rolling/closing before
   expiry is standard — but we should flag any same-day-expiry trades
   as "close before EOD" to avoid unexpected futures delivery.

## 10. What NOT to ship before this is done

- No code that assumes `tickers` rows are always OPT — already tripped
  on this during the symbol-normalization fix (`088e494`), expect more.
- No changes to the Trades tab UI that hardcode 100× multiplier in
  P&L display.
- No expansion of `FOP_SPECS` to exotics (FX futures, overseas
  indices) until the core path is proven on MNQ.
