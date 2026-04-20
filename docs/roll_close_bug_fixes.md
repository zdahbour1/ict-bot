# Roll & Close Flow — Bug Fixes (April 20, 2026)

## Incident summary

On **2026-04-20 during market hours**, two live-trading incidents occurred
after the user lowered `roll_pct` for testing:

| Symbol | What happened | Consequence |
|--------|---------------|-------------|
| **IWM 275C** | Roll triggered; close SELL flattened position to 0; original bracket SL wasn't cancelled; ~20 min later the SL fired → **-2 short calls** (naked short) | Assignment risk if close > $275 (same-day expiry) |
| **TSLA 397.5P** | Roll triggered; close aborted with spurious "direction mismatch" error; DB marked closed anyway; reconcile later adopted as new db_id | Orphaned position, DB↔IB out of sync |

Both were manually resolved in TWS. This doc captures the three underlying
bugs and the fixes.

## The three bugs

### Bug A — Cross-client bracket blindness

**File:** `broker/ib_client.py::_ib_find_orders_for_contract`
**Symptom:** log line `"STEP 1: No orders found but brackets EXPECTED
(TP=X, SL=Y). Bracket may have JUST FIRED."` followed by proceeding to sell.
In reality the brackets are alive on IB — we just can't see them.

**Root cause:** `self.ib.openTrades()` returns only orders placed by the
**same IB clientId** as the querying connection. The bot uses a pool:
- Entry manager → clientId=3 (places the bracket)
- Exit manager → clientId=1 (calls the close)

The exit manager's `openTrades()` sees zero matches, so `cancel_all_orders`
has nothing to cancel, sells the position to 0, and leaves the TP/SL
bracket alive on IB. When the SL later triggers, it sells 2 more →
naked short.

**Fix:** call `ib.reqAllOpenOrders()` before each open-orders query.
This is a one-shot RPC that delivers `openOrder` callbacks for orders
placed by every client in the account, merging them into the current
connection's `wrapper.trades` dict. Subsequent `openTrades()` calls now
return the full set.

To avoid spamming IB during the tight 6-round cancel-verify poll loop,
the refresh is invoked **once** at the top of `cancel_all_orders_and_verify`
via a new `client.refresh_all_open_orders()` helper; the inner poll
keeps reading from the now-complete cache.

### Bug B — Direction-mismatch check inverted for `direction=SHORT`

**File:** `strategy/exit_executor.py::execute_exit` (line 229-240)
**Symptom:** log line `"EXECUTE EXIT ABORTED — direction mismatch!
Trade=SHORT but IB qty=2"` on any ICT bearish (long-puts) trade.

**Root cause:** ICT encodes bearish trades as `direction=SHORT`, meaning
"we buy puts for a bearish view". Our IB position is **+N puts** (we
hold them long). The old check flagged this as a mismatch:

```python
# OLD — wrong
if (trade.get("direction") == "LONG"  and ib_qty < 0) or \
   (trade.get("direction") == "SHORT" and ib_qty > 0):
    ABORT "direction mismatch"
```

`direction=SHORT` with `ib_qty > 0` is the **normal** state, not a
mismatch. The bot supports only long-options strategies today (never
sell-to-open); an active trade should always have `ib_qty > 0` on IB.

**Fix:** replace the direction-specific check with a simple positivity
check. A mismatch is `ib_qty <= 0` for either direction. If we ever add
true naked-short strategies (e.g. credit spreads), this check is the
right place to add a branch — but until then, any non-positive qty
during an active trade is suspicious and should abort.

```python
# NEW
if ib_qty <= 0:
    ABORT "position is non-positive — cannot sell to close"
```

### Bug C — `_atomic_close` finalizes DB even when IB close aborted

**File:** `strategy/exit_manager.py::_atomic_close` (line 347-376)
**Symptom:** DB trade marked `status=closed exit_result=WIN reason=ROLL`
while the position is still open on IB. Reconcile later adopts the
orphan as a new trade row.

**Root cause:** after calling `execute_roll` / `execute_exit`, the code
proceeds unconditionally to `finalize_close`. If the IB side aborted —
either because `cancel_all_orders_and_verify` failed (Bug A), or the
direction check tripped (Bug B), or the SELL never filled —
`finalize_close` still marks the DB trade closed. This violates
**ARCH-005** ("If any step fails → rollback, retry next cycle").

**Fix:** insert a post-close IB verification step. Poll IB for up to 3
seconds, expecting:
1. Position quantity = 0 (either our SELL filled or bracket fired)
2. No working orders remaining (defensively cancel any stragglers)

If position is still non-zero after the poll window, release the DB
lock without calling `finalize_close` — the trade stays `open` and
the next exit-manager cycle retries. The close will eventually succeed
or a human gets paged via reconciliation.

## API surface changes

### `BrokerClient.refresh_all_open_orders()` (new)

```python
def refresh_all_open_orders(self) -> None:
    """Refresh the local openTrades() cache with orders from ALL clients
    in the account (not just this connection's clientId). Must be
    called before any find_open_orders_for_contract() query that
    needs to see brackets placed by a different pool connection."""
```

Implementation: submit `self.ib.reqAllOpenOrders()` to the IB thread.

### `ExitManager._verify_close_on_ib(trade) -> bool` (new)

```python
def _verify_close_on_ib(self, trade: dict) -> bool:
    """Poll IB up to 3s expecting position=0 + no working orders.

    Returns True if the close completed cleanly on IB. Returns False
    if position is still non-zero after the poll window — caller must
    release the DB lock and skip finalize_close."""
```

## Test plan

All three fixes get unit tests with a mocked `BrokerClient` —
no live IB required.

| Test | File | Covers |
|------|------|--------|
| `test_find_open_orders_calls_req_all` | `tests/unit/test_exit_close_flow.py` | Fix A — ensures the new refresh hook fires before query |
| `test_cancel_verify_sees_cross_client_bracket` | same | Fix A — simulates two-clientId scenario, asserts the "straggler" bracket gets cancelled |
| `test_direction_short_with_positive_qty_proceeds` | same | Fix B — long-put trade no longer aborts |
| `test_direction_short_with_zero_qty_aborts` | same | Fix B — genuine mismatch still caught |
| `test_direction_long_with_negative_qty_aborts` | same | Fix B — the other genuine mismatch still caught |
| `test_atomic_close_finalizes_when_verify_passes` | `tests/integration/test_atomic_close_verify.py` | Fix C — happy path with position=0 on IB |
| `test_atomic_close_releases_lock_when_verify_fails` | same | Fix C — position non-zero after poll → lock released, no finalize |
| `test_atomic_close_cancels_stragglers` | same | Fix C — any remaining working order gets defensively cancelled after verify |

Every new behavior also gets a trace log line so bot.log clearly shows:
- `[IWM] REFRESH ALL ORDERS: N trades visible (cross-client merge)`
- `[IWM] VERIFY CLOSE: position=0, N stragglers cancelled, PASS`
- `[IWM] VERIFY CLOSE FAILED: position still 2 after 3s — releasing lock, will retry`

## Deployment

1. Ship on branch `feature/profitability-research` (same branch as
   the analytics work — the live-trading code and the profitability
   research share this worktree).
2. Bot must be restarted to pick up the Python changes. Frontend is
   not affected.
3. No schema changes, no migrations.

## What this does NOT fix

- The bot still silently accepts `direction=SHORT` semantics meaning
  "long puts". Future naked-short strategies will need a third
  direction value (`NAKED_SHORT` or similar) and branch logic in
  the position-sign check.
- The TP/SL prices shipped by `place_bracket_order` are not yet
  persisted to `trades.ib_tp_price` / `ib_sl_price` — the log now
  shows them (per the Apr-20 observability commit `20287ac`) but
  they aren't queryable from SQL. Separate follow-up.
- Reconcile's PASS 2 still adopts orphaned IB positions. With
  these fixes, orphans should be rare — but reconcile as a safety
  net stays in place.
