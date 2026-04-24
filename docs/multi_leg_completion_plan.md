# Multi-Leg Strategy Completion Plan

**Status:** Draft (2026-04-24)
**Context:** DN/ZDN strategies were implemented with entry-only support; exit, brackets, adoption, and UI treat every position as single-leg. That created a cascade of production issues (stuck orders, ghost DB rows, leg-by-leg closes, missing brackets). All multi-leg strategies are disabled until this plan lands.

---

## 1. Two classes of multi-leg strategy

The architecture fork (per user direction 2026-04-24):

### Class A — Defined-Risk, IB-Bracketed
No hedge. Net credit/debit at entry is fixed. IB can own the exit via bracket on the combo.

**Members:** `v1_baseline`, `v2_hold_day`, `v3_phaseB`, `v4_filtered`.

- **Entry:** BAG limit order, signed net credit (ENH-063).
- **Bracket:** placed immediately after fill; TP = 50% of credit captured, SL = 2× credit debited.
  - Uses IB combo brackets — one TP LMT + one SL STP, both on the reverse BAG.
- **Exit:** IB fires bracket → bot receives fill notification → updates DB.
- **Bot monitoring:** envelope-level only (net P&L); no hedging.

### Class B — Bot-Managed, Delta-Hedged
Stock hedge adjusts continuously; TP/SL must factor in hedge P&L, which IB brackets can't express.

**Members:** `v5_hedged`, `v5b_sweep_winner`, all `zdn_*` variants.

- **Entry:** BAG limit order (same as Class A).
- **No bracket:** bot owns exit logic entirely. Per-envelope monitor loop.
- **Hedge:** existing `delta_hedger.py` runs every 30s, buys/sells stock to keep |net_delta| ≤ band (variant-tuned — ZDN uses 10 shares, V5 uses 20).
- **Exit trigger:** compute `net_pnl = sum(leg_market_value × direction) + stock_hedge_market_value - entry_credit`. Fire close when `net_pnl >= TP_usd` or `net_pnl <= -SL_usd` or `time >= exit_before_close_min`.
- **Exit execution:** one BAG reverse + one stock flatten, in that order. Both must complete before status→closed.

---

## 2. Work breakdown (phases)

### Phase 1 — Combo close path **[do first, lands this session]**
Foundation for both classes. Without this, even Class A can't close cleanly.

- Wire `place_combo_close_order` into `exit_executor.execute_exit` when `trade["n_legs"] > 1` and `trade["has_combo_order_id"]`.
- Reverse-sign the legs; reuse the original combo contract metadata.
- Handle partial fills: if combo close partial-fills, retry up to 3× before escalating to per-leg flatten.
- Update `trade_legs` in one transaction: `contracts_open = 0`, `leg_status = 'closed'` for every leg.
- Tests: happy path, partial-fill retry, leg sync on success.

### Phase 2 — Envelope-level brackets for Class A
Only fires when variant has `delta_hedge = False`.

- After `insert_multi_leg_trade` returns, place **one** bracket: TP @ 50% credit (sell combo at 50% of entry net), SL @ 2× credit (sell combo at 200% of entry net).
- Store bracket IDs on the `trades` row (new columns: `ib_tp_order_id`, `ib_sl_order_id`) — _trades_, not _trade_legs_.
- Reconciliation PASS 4 (bracket audit) recognizes envelope brackets.
- Tests: bracket placed, bracket survives restart, reconciliation doesn't spuriously restore.

### Phase 3 — Bot-managed exit loop for Class B
New thread: `EnvelopeExitMonitor`.

- Polls open trades where `trade.strategy.delta_hedge = True` every `DN_EXIT_POLL_SEC` (default 15s).
- Computes `net_pnl` = Σ(leg_mid × multiplier × signed_qty) + hedge_shares × stock_mid − entry_credit_usd.
- Checks triggers in order: hard time-exit → SL → TP → profit target pct.
- Fires `close_multi_leg` which invokes Phase 1 combo close + flattens `hedge_shares`.
- Audit row on every decision (`envelope_exit_decisions` table) for post-mortem.
- Tests: each trigger fires once and exactly once, hedge flat before status change.

### Phase 4 — Multi-leg-aware adoption (reconciliation PASS 2)
Eliminates the ICT-ticker-collision bug.

- When ≥3 option positions on same (underlying, expiry) land within ±10s of each other, group into one multi-leg adoption.
- Detect shape: iron condor (4 legs, 2 short + 2 long, different strikes), iron butterfly (4 legs, 2 shorts at same ATM), vertical (2 legs, same right), strangle (2 legs, both short OTM).
- Adopt under a new strategy_id `dn_adopted` with `n_legs=N`, tagged `signal_type='ORPHAN_ADOPTED_MULTI_LEG'`.
- Add a new unique-index exclusion for `dn_adopted` so it can hold multiple trades per ticker (each with distinct `client_trade_id`).
- Tests: 4-leg grouping, 2-leg grouping, mixed expiries NOT grouped.

### Phase 5 — Trades UI
Show multi-leg as single row, drill-down for legs.

- `TradeTable.tsx`: add `n_legs` column. When `>1`, row is expandable. Expansion shows each leg + hedge shares + net P&L.
- Hide per-leg duplicate rows when they roll up under a multi-leg trade.
- Net-credit / net-debit badge on the row.
- Filter: strategy filter already exists; add `trade_type` filter (single / combo / hedged).
- Tests: render expanded + collapsed states, live P&L refresh.

### Phase 6 — Integration harness
End-to-end tests on paper before re-enabling any DN/ZDN strategy.

- Scenario A: open Class A → TP hits → close confirmed.
- Scenario B: open Class B → delta drifts → hedger fires → TP hits → close both combo + stock.
- Scenario C: restart bot mid-trade → state recovers correctly.

---

## 3. Rollout

1. Land Phase 1 (combo close) — this session.
2. Land Phase 2 + Phase 3 — next two sessions, tested on paper manually before re-enabling.
3. Phase 4 — parallel track, low urgency now that strategies are off.
4. Phase 5 — after paper validation of 2+3.
5. Re-enable Class A strategies only, observe for 2 days.
6. Re-enable Class B strategies after 2 clean days on Class A.

---

## 4. Non-goals (explicitly not in scope)

- Combo-orderbook price improvement (paying less than mid on BAG submission).
- Rolling (closing near-expiry and opening next-month at same deltas).
- Multi-underlying strategies (pairs, ratios across different tickers).
- Live Greeks beyond delta (gamma scalping is already ad-hoc in hedger).
