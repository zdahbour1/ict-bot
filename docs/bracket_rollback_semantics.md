# Bracket Operations — Transaction / Rollback Semantics

**Principle** (explicit from user requirement 2026-04-20 afternoon):

> Any transaction is either fully complete successfully OR rolled back
> entirely. If a request did not complete during the time allocated,
> it does not mean it will not complete later. You can't just abandon
> it. You need to rollback the request and make sure nothing happens.
> This is a mission critical system and every action taken has to be
> fully controlled.

IB's order-cancellation API is **asynchronous**. When we send
`cancelOrder` and poll for terminal status, three outcomes are
possible:

| Outcome | What happened | Risk |
|---------|---------------|------|
| a. Cancelled within our poll window | Clean — cancel + close proceed | None |
| b. Still PendingCancel when we give up | Cancel is still pending on IB's side | **Will likely complete minutes later; we've lost control** |
| c. Reverted to Submitted | IB rejected the cancel | Retry, or abort and accept bracket is alive |

The bug fixed today manifested from case (b): we sent cancels, they
went `PendingCancel`, our strict-verification loop gave up after 9
seconds, the close SELL was aborted — but ~30s later IB processed
the cancels anyway. Result: 10 positions left naked (no brackets,
no close).

## Compensating transactions

Since `cancelOrder` is not reversible — once IB processes the cancel,
the order is gone — we cannot undo the cancel request itself. But we
can take a **compensating action** that restores the state we need:
placing a fresh bracket.

The transaction model for a bracket close becomes:

```
begin close()
  step 1: send cancelOrder on bracket children
  step 2: poll for terminal state (with timeout)
  step 3a (happy): cancel reached terminal → send close SELL
  step 3b (abort): cancel did not verify AND position still held
           → compensating action: place_protection_brackets()
             (fresh TP + SL attached to the existing position)
end close()
```

In case (b), the compensating action MAY race with the late-arriving
cancel. IB will reject one of two attempts (code 201 — cannot have
orders on both sides). We accept that occasional retry cost because
it's bounded and safe.

## Where this is implemented

- **PASS 4 of reconcile** (`strategy/reconciliation.py`) — every cycle
  checks each open trade's `ib_tp_perm_id` / `ib_sl_perm_id` against
  IB's live orders. If both legs are in a terminal non-fill state
  (Cancelled / Inactive / MISSING) and the position is still held,
  it's an **unprotected_position**. Audit emitted, then
  `_restore_brackets_for` runs the compensating action:
  `place_protection_brackets(symbol, contracts, tp_price, sl_price)`.

- **`BrokerClient.place_protection_brackets`** (`broker/ib_client.py`)
  — attaches a SELL LMT + SELL STP pair in a fresh OCA group to an
  existing long position. Unlike `place_bracket_order`, there's no
  new parent BUY. This is purely protection on inventory already
  held.

- **Audit trail** (`strategy/audit.py`):
  - `unprotected_position` — emitted by reconcile when naked state
    detected.
  - `bracket_restored` — emitted when compensating action succeeds.
  - `bracket_restore_failed` — emitted when the restore itself
    fails (e.g., IB rejects because the zombie cancel is still
    pending). Next reconcile cycle will retry.

## What the UI shows

The **Brackets** column on the Trades tab renders per-trade TP/SL
status in one cell, colored:

| Display | Meaning |
|---------|---------|
| `TP OK $X · SL OK $Y`  (green) | Protected — both legs active |
| `TP CXL · SL CXL ⚠`  (red bg) | **Unprotected** — compensation pending |
| `TP FILL · SL CXL` (blue TP) | TP filled; SL auto-cancelled (trade likely already closing) |
| `TP GONE · SL GONE ⚠` (red bg) | permIds recorded but IB has no trace — reconcile will re-attach |

Hover tooltip reveals `permId`, `orderId`, status, price, and the
`ib_brackets_checked_at` timestamp so operator can tell how fresh
the data is.

## Invariants we guarantee

1. **No open trade remains unprotected for more than one reconcile
   cycle**, unless the restoration itself fails repeatedly (in
   which case an `error`-level audit row fires each cycle).
2. **Every cancel attempt has a known outcome**: either the
   corresponding close SELL fires (happy path), or the
   `bracket_restored` audit confirms compensation.
3. **The `trades` row is the authoritative view of bracket state**
   between reconcile cycles. UI renders from that; no need to
   round-trip to IB per render.

## Open questions / future hardening

1. **Race during restoration**: if the late-arriving cancel lands
   after we've re-placed the bracket, IB might apply it to the new
   order (unlikely — different permId — but theoretically). Would
   require one more guard: check the cancel queue and invalidate
   stale cancels. Not implemented; has not been observed.
2. **Restore with drifted SL**: today we restore at the original
   `stop_loss_level`. A trade that has trailed up to +30% peak gets
   its SL restored to the entry-based level, losing the trailing
   progress. Future: use `dynamic_sl_pct` to restore at the current
   trailing level.
3. **Multiple restores in one cycle**: 10 unprotected positions mean
   20 order placements in one reconcile pass. IB pacing (50 orders
   / second) means this is fine today but would need batching at
   100+ positions.

## Testing strategy

- Unit: mock `place_protection_brackets` and assert the reconcile
  PASS 4 calls it with the right `contracts / tp_price / sl_price`
  when the trade row has Cancelled statuses.
- Integration (mocked IB): simulate the full flow — trade open,
  brackets get marked Cancelled, reconcile runs, DB row gets new
  perm IDs written, audit row emitted.
- Live smoke: after restart, the 10 currently-unprotected positions
  should trigger `bracket_restored` audits within the first reconcile
  cycle (60s). Verify by SQL filter.

## Related docs

- `docs/bracket_cancel_strict_verification.md` — the MSFT strict-cancel
  fix (the bug that triggered this work)
- `docs/orphan_bracket_detector.md` — PASS 3 orphan cleanup
- `docs/logging_and_audit.md` — audit trail pattern used here
- `docs/roll_close_bug_fixes.md` — the Fix A/B/C lineage
