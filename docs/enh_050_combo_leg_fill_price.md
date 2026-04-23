# ENH-050 — Per-leg fill-price fallback for combo (BAG) orders

**Status:** Proposed
**Owner:** TBD
**Tracked in:** `docs/backlog.md` (created 2026-04-23)
**Related:** ENH-046 (BAG combo orders), ENH-047 (leg drill-down UI),
ENH-049 (delta-hedger)

---

## 1. The bug — what the user sees

After ENH-046 shipped combo orders, some multi-leg trades land in the
database with **`entry_price = 0` on every leg**, breaking:

- Per-leg P&L in the Trades-tab drill-down (`(exit - 0) * 100 * sign` gives garbage)
- The trade envelope's `pnl_usd` aggregate
- The delta-hedger's share-equivalent delta calculation (`bs_greeks(S, K=0, …)` is wrong)
- Any backtest replay that reads `entry_price` as the anchor

Live example — **MSFT trade 3414**, placed 2026-04-23 18:12:34 as a combo iron condor:

| leg_index | role        | symbol              | entry_price | current_price |
|-----------|-------------|---------------------|-------------|---------------|
| 0         | short_call  | MSFT260424C00415000 | **0.00**    | 3.38          |
| 1         | long_call   | MSFT260424C00425000 | **0.00**    | 0.00          |
| 2         | short_put   | MSFT260424P00415000 | **0.00**    | 0.00          |
| 3         | long_put    | MSFT260424P00405000 | **0.00**    | 0.00          |

Leg 0 shows `pnl_usd = -338` (which looks massive) because the UI
computes `(3.38 - 0.00) * 1 * 100 * -1 = -338`, but the actual fill
would have been around 3.50 so the real unrealized is approximately
-0.12 × 100 × -1 = **+$12**. Two orders of magnitude off.

A sibling trade from 3 minutes earlier — **COIN 3415** — populated
entry_price correctly on all 4 legs. Same code path, different result.

---

## 2. Why this happens — IB combo-fill mechanics

When we place a `BAG` (combo) order with `comboLegs`, IB reports
fills in one of **two ways** depending on the execution path:

### 2.1 Executed on an options exchange as a spread

Common case on SPX/QQQ and liquid underlyings. The exchange matches
the combo atomically at a **net price**. IB returns:

- `ib_trade.orderStatus.avgFillPrice` = the net (debit/credit)
- `ib_trade.fills[*].contract.conId` = the **per-leg conId**
- `ib_trade.fills[*].execution.avgPrice` = the **per-leg price**

This is the shape our code currently handles:

```python
leg_fill_prices: dict[int, float] = {}
for fill in getattr(ib_trade, "fills", []) or []:
    c_id = getattr(fill.contract, "conId", None)
    if c_id is not None:
        leg_fill_prices[int(c_id)] = float(fill.execution.avgPrice)
```

**COIN** hit this path — every leg got an `avgPrice`.

### 2.2 Executed as 4 separate option fills (SMART-routed legs)

When SMART routing splits the combo and fills each leg individually
on different exchanges — common for less liquid names like MSFT at
non-standard strikes — IB may deliver fills such that:

- `fill.contract.conId` is the **Bag's conId** (or `0`), not the leg's
- Per-leg prices live elsewhere or don't come through this object at all
- Only `orderStatus.avgFillPrice` reliably has the net

**MSFT 3414** hit this path — our `leg_fill_prices` dict ended up
empty and every leg defaulted to `0.0`.

The loop also has a 10-second max-wait (`for _ in range(20): sleep(0.5)`)
and some combos fill *after* that window on paper — we walk away with
a `Submitted` status and no fill data, leaving us to pretend each
leg's `fill_price` is 0.

---

## 3. Proposed fix

Three layered fallbacks, from most-accurate to crudest, all gated
**only when `fill_price = 0`** for a given leg:

### 3.1 Primary: Read `Execution.avgPrice` via ib_async's ``execDetails``

``ib_trade.fills`` is a simplified view. The richer source is
``ib.executions()`` / the ``execDetails`` event stream, which carries
one Execution per leg with its own contract conId **even when the
top-level fills list is sparse**. After placement, query:

```python
# Runs on IB event loop
executions = self.ib.executions()     # list[Fill]
for exec_row in executions:
    if exec_row.execution.orderId == ib_trade.order.orderId:
        c_id = exec_row.contract.conId
        price = exec_row.execution.avgPrice
        leg_fill_prices.setdefault(c_id, float(price))
```

This typically recovers 3 of 4 missing legs.

### 3.2 Secondary: Quote each leg with a fresh market-data snapshot

For any leg still at 0 after 3.1, fetch a mid quote:

```python
from broker.ib_market_data import IBMarketDataMixin
for leg_index, leg, contract in leg_contracts:
    if leg_fill_prices.get(contract.conId, 0) > 0:
        continue
    try:
        mid = self.get_option_price(leg["symbol"])  # existing method
        leg_fill_prices[contract.conId] = mid
    except Exception:
        pass
```

This is an **approximation** — the quote 200ms after the fill may
differ by a few cents — but wrong by pennies is infinitely better
than wrong by 100%.

### 3.3 Tertiary: Distribute net_fill_price across legs proportionally

Last resort when both 3.1 and 3.2 fail (e.g. market closed,
data-subscription issue). We know the **net fill** (reliable) and the
**direction** of each leg (from the input). Use the absolute-value
ratio of the strategy's quoted pre-fill estimates as weights, or
simply split by |short_call_est + long_call_est + …|.

For iron condor at the body this degenerates gracefully — short legs
are typically 3-5× the long legs in premium, so a net credit of
$1.50 allocates roughly $2.25 to each short and $0.75 to each long.
Not exact, but better than zeros.

Implementation note: the pre-fill estimates aren't currently captured
by the strategy's `place_legs`. We can plumb them through — just a
new optional `est_price` on `LegSpec` that the strategy fills in if
it has quotes. If absent, equal split across legs.

### 3.4 Data back-fill for existing bad rows

For the handful of trades already persisted with `entry_price=0`
(observed: one trade as of 2026-04-23), run a one-shot SQL helper
that:

1. Selects legs with `entry_price = 0` from recent-enough trades
2. Pulls the trade's combo `order_id` → queries `ib.executions()` for
   matching executions (if still in IB's 24-hour window)
3. Writes the recovered `execution.avgPrice` back to `trade_legs`
4. Falls through to 3.2 (current quote) for anything not recoverable

Ship this as `scripts/backfill_combo_fill_prices.py`, same pattern as
`scripts/cleanup_orphan_brackets.py`.

---

## 4. Alternatives considered

### 4.1 "Don't use combo orders" — rejected

Solves the symptom by forcing the 4-independent-orders path which
has its own partial-fill problem (solved by ENH-046 but at the cost
of 4× bracket orders which caused the earlier bracket-explosion
incident). We want combo.

### 4.2 "Require LimitOrder with net-price limit" — partial solution

Using `LimitOrder` with a computed net price limit would:
- Prevent market slippage (different problem — agent report)
- Still not guarantee per-leg fill prices come back cleanly

Worth doing (tracked separately as ENH-050-v2 / agent follow-up) but
doesn't replace the fallback chain. An LMT fill still produces
per-leg executions that may or may not come through.

### 4.3 "Reject trade if any leg fills at 0" — rejected

Rolling back an already-filled condor adds operational risk (each
unwind leg incurs slippage + commission). Cleaner to accept the
position + back-fill the price data.

### 4.4 "Wait longer than 10 seconds for fills" — incomplete

Extending the wait loop to 30s or 60s helps some cases but doesn't
fix the root issue: IB's `fills` list sometimes just doesn't break
out per-leg prices even for long-completed executions. The
`execDetails` stream is a separate code path.

---

## 5. Tests

Must-have (ship with the feature):

1. **Primary path still works** — when `ib_trade.fills` contains
   per-leg conIds + prices, those land in `legs_result` unchanged.
   (Existing test covers this; extend with MSFT-style empty-fills
   input to assert fallback kicks in.)

2. **execDetails fallback** — mock `ib.executions()` to return
   matching executions; assert `leg_fill_prices` picks them up
   for legs the fills list missed.

3. **Quote fallback** — mock both `fills` and `executions` empty;
   stub `client.get_option_price` to return fake mids; assert the
   leg dict gets `fill_price = mid`.

4. **Proportional distribution** — all quote paths fail; net
   fill = 1.50; 4 legs with est_price hints [2.20, 0.80, 2.00, 0.60];
   assert allocation weights sum correctly.

5. **No-op when all fills populated** — primary path doesn't invoke
   the fallback chain (saves IB calls when fills are clean).

6. **Back-fill script** — dry-run mode reports plan without writing;
   actual mode writes correct prices and logs per-leg source
   (execDetails vs quote vs proportional).

---

## 6. Rollout plan

| Stage | Scope | Gating |
|-------|-------|--------|
| **A** | Add 3.1 (execDetails) to `_ib_place_combo` | Ship immediately — pure addition, no behavior regression possible |
| **B** | Add 3.2 (quote fallback) | Gate on `COMBO_PRICE_QUOTE_FALLBACK=true` default true — trivial to turn off if quotes add latency |
| **C** | Add 3.3 (proportional split) | Gate on `COMBO_PRICE_PROPORTIONAL_FALLBACK=true` default true — strategy-specific `est_price` plumbing is optional |
| **D** | `scripts/backfill_combo_fill_prices.py` | One-shot on-demand |
| **E** | Plumb `est_price` into `DeltaNeutralStrategy.place_legs` | Optional quality improvement |

Stages A-C all ship in the same PR. Stage D is a separate PR. Stage E
is follow-up.

---

## 7. Open questions

1. **Does IB paper data have enough executions-table history for 3.1?**
   Live account does; paper may be shorter. Need to verify with a test
   trade.

2. **How fresh is "fresh enough" for 3.2?** If 500ms after fill is
   acceptable, standard 1.5s snapshot sleep is fine. For sub-second
   freshness we'd need to use streaming.

3. **Should `entry_price` back-fill overwrite existing nonzero values?**
   Current plan: NO — only fill zeros so we don't corrupt good data.

4. **Audit trail**: should back-filled prices carry a `price_source`
   tag in `trade_legs` (e.g. `exec`, `quote`, `proportional`) for
   future debugging? Adding a column is cheap; worth doing.

---

## 8. Success criteria

After Stage A+B deploy, of the next 20 combo trades placed:

- ≥ 95% have `entry_price > 0` on **every** leg without human intervention
- ≥ 80% use source `fills` or `exec` (3.1) — these are within 1 cent
  of the true fill
- ≤ 20% fall through to `quote` (3.2) — typically within 10 cents
- ≤ 5% reach `proportional` (3.3) — always on the Trades-tab UI
  reviewed by user before trusting P&L

If any leg repeatedly hits `proportional`, the strategy's IB
qualification or market-data entitlement is the real issue and should
be escalated separately.
