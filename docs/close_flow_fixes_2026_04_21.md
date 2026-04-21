# Close-Flow Fixes — 2026-04-21 Live Session

Two distinct bugs surfaced on the first live day after the IB↔DB
correlation + sell-first close mode shipped. Both triggered from the
same SPY 710P trade; both are fixed and covered by tests. This doc is
the post-mortem and design reference.

> **TL;DR** — The bot (a) rolled the same trade four times at the same
> strike because the close-verify was fooled by the new rolled position
> sitting on the old `conId`, and (b) spuriously logged "brackets alive
> after close" on every close because `ib_async`'s per-connection order
> cache goes stale when another pool connection cancels the order.
>
> Fixes:
> 1. `execute_roll` rejects same-symbol rolls as degenerate churn.
> 2. `exit_manager` skips `_verify_close_on_ib` when a legitimate
>    (different-symbol) roll returned.
> 3. `find_open_orders_for_contract` fans out across every pool
>    connection and takes the most-terminal status per `permId`.

**Commits:** `a1f23df`, `949c7da`
**Tests:** `tests/unit/test_roll_same_strike_guard.py`,
`tests/unit/test_find_open_orders_pool_merge.py`

---

## Part 1 — Roll-loop churn when selector picks the same strike

### Symptom
Trade 1223 (SPY 710P) opened cleanly at 14:32:48. When `peak_pnl_pct`
crossed the roll threshold at 14:37, the bot initiated a ROLL. It then
ran **four more rolls** back to back, each opening and closing a new
position at the **same strike** (SPY 710P). Net commission cost: 8
extra round-turns. User flagged it: "opened and closed three other
trades with the same strike for no apparent reason."

### Timeline from bot.log

```
07:37:27  ROLL START (db_id=1223)
07:37:40  SELL-FIRST EXIT COMPLETE            ← old position closed
07:37:45  ROLL COMPLETE → SPY260421P00710000  ← NEW @ SAME STRIKE
07:39:50  ROLL START (db_id=1223)             ← loop iter 2
07:40:08  ROLL COMPLETE → SPY260421P00710000  ← SAME STRIKE again
07:40:26  ROLL START (db_id=1223)             ← loop iter 3
07:40:43  ROLL COMPLETE → SPY260421P00710000  ← SAME STRIKE again
07:40:53  ROLL START (db_id=1223)             ← loop iter 4
07:41:15  ROLL: old position closed, new entry failed (IB rejected)
07:41:27  ATOMIC CLOSE COMPLETE db_id=1223
```

### Root cause

The flow in `strategy.exit_manager.handle_exit` for a roll decision is:

1. Acquire row-level lock on the trade.
2. Call `execute_roll(client, live_trade, pnl_pct)`:
   - `execute_exit` sends MKT SELL → old position flattens.
   - `select_and_enter_put` picks the best 0.10-delta put and enters.
     **Critically: the "best" strike is chosen from current market
     conditions.** When SPY barely moved, the selector picked the
     *same* 710P.
   - Returns the new trade dict.
3. Call `_verify_close_on_ib(live_trade)` on the **old** trade's
   `ib_con_id`. The helper polls `get_position_quantity(con_id)`
   expecting `0`.
4. Because the new rolled position lives on the **same `conId`** as the
   old one, the position query returns `2` — the freshly opened
   contracts. Verify returns `False`.
5. `handle_exit` releases the lock without calling `finalize_close`.
   The DB row stays `status='open'`.
6. Next exit cycle sees the same row, recomputes pnl (still elevated),
   triggers ROLL again. Go to step 2.

The loop only broke when IB eventually rejected a duplicate entry and
`execute_roll` returned `None`, causing `_verify_close_on_ib` to hit
the position (now truly 0 because the last roll failed to re-enter)
and succeed.

### Why this wasn't caught earlier

Rolls on different strikes work perfectly. All prior rolls on the
feature branch landed on different strikes because the underlying had
moved enough between entry and roll that the selector picked a new
0.10-delta candidate. The degenerate same-strike case only shows up
when the underlying chops near the original entry price — exactly
what SPY did around 14:37.

### Fix

Two-part, both in one commit (`a1f23df`):

#### 1. `strategy/exit_executor.py::execute_roll`

```python
if rolled_trade.get("symbol") == trade.get("symbol"):
    log.warning(
        f"[{ticker}] ROLL ABORTED — new entry at SAME symbol "
        f"{rolled_trade['symbol']} as the trade being rolled. "
        f"Treating as plain exit; closing duplicate position."
    )
    try:
        execute_exit(client, rolled_trade,
                      reason="SAME_STRIKE_ROLL_REVERT")
    except Exception as e:
        log.error(f"[{ticker}] failed to revert same-strike roll: {e}")
    return None
```

If the selector returned the same symbol, the roll is pointless. Close
the duplicate we just opened, return `None`. The outer loop then
treats this as a plain exit — finalize the old DB row and stop.

#### 2. `strategy/exit_manager.py::handle_exit`

```python
if should_roll and rolled is not None:
    log.info(f"[{ticker}] VERIFY CLOSE: skipped — legitimate roll "
             f"to {rolled.get('symbol')}; trusting execute_roll's "
             f"own post-SELL verification")
    close_ok = True
else:
    close_ok = self._verify_close_on_ib(live_trade)
```

When a roll *succeeds* (returns a new trade with a different symbol),
skip the misleading conId-based verify. `execute_exit` inside
`execute_roll` already ran the post-SELL `qty==0` check. If `rolled`
is `None` (new entry failed or same-strike abort), run the normal
verify — in that case the old `conId` really is flat.

### Tests

`tests/unit/test_roll_same_strike_guard.py` — 5 cases:
- Same-symbol roll → close duplicate, return None
- Different-symbol roll → proceed normally with "ROLL from" annotation
- Legit-roll branch → skip verify
- No-roll branch → still run verify
- Failed-new-entry (rolled=None, should_roll=True) → still run verify

### Why not fix the selector directly?

Tempting: "make the selector skip the current strike." Rejected for
two reasons:

1. **Semantics.** The selector's job is "pick the best 0.10-delta put
   given current market." Forcing it away from the current strike is
   a strategy decision that doesn't belong in the picker.
2. **Safety.** If the selector *must* pick a different strike, it
   might pick a genuinely worse one (farther OTM, less liquid). The
   guard in `execute_roll` is a cheaper, more local fix: if no
   meaningfully-different strike is available, **don't roll** — just
   take profit with a plain exit. That's the right business decision.

A future enhancement could extend the should_roll predicate upstream
so we never even enter `execute_roll` when the selector won't produce
a different strike — but that requires separating "pick strike" from
"place order" in `option_selector`. Deferred.

---

## Part 2 — Stale per-connection cache made POST-SELL verify lie

### Symptom

Every successful close produced log noise:

```
POST-SELL verify: 2 bracket(s) still alive after 5.0s — issuing
                   explicit cancel: [3577, 3576]
POST-SELL verify: 2 bracket(s) STILL ALIVE after explicit cancel —
                   reconcile PASS 3/4 will clean up
brackets_alive_after_close: 2 brackets alive after SELL
```

Dashboard lit up with yellow/red alerts. Reconcile PASS 3/4 then ran
on the next tick, found nothing alive (because the brackets actually
*were* gone), and cleared the alert. Harmless in outcome but:
- False-positive critical-path alert fires on every single close.
- Adds ~5s of dead time to every exit.
- Masks real alerts when they come.

### What IB actually saw

Hand-tracing orderIds 3576 (TP LMT) and 3577 (SL STP) through the log:

```
14:37:28.345  3577 → PendingCancel (cancel sent from exit-mgr)
14:37:28.935  3577 → Cancelled  ✅
14:37:29.003  3576 → PendingCancel
14:37:29.110  3576 → Cancelled  ✅
14:37:36      our code: "still alive after 5s"       ← LIE
14:37:39      our code: "STILL ALIVE after explicit cancel" ← LIE
```

Both orders reached terminal state within **0.8 seconds**. Our verify
then polled for 5 more seconds and concluded they were still live.
The disconnect was entirely on our side.

### Root cause

`ib_async`'s `IB.openTrades()` returns a cached list populated by
`wrapper.trades` — a dict maintained by the per-connection event
loop. When IB emits an `orderStatus` callback, ib_async updates the
trade object on the connection whose socket received the callback.

The pool has ~4 active connections (`exit-mgr`, `scanner-A`,
`scanner-B`, `entry-mgr`). Order 3576 was *placed* via scanner-A
(`clientId=3`) so scanner-A's wrapper has the canonical status. When
exit-mgr's `cancel_order_by_id` fans out, the cancel actually
succeeds on scanner-A's connection — status transitions to Cancelled
**on scanner-A's wrapper**.

exit-mgr's wrapper is untouched. Its cached entry for 3576 still says
`Submitted` from the last time it was populated via
`reqAllOpenOrders`.

`reqAllOpenOrders` is supposed to refresh the view, but IB's contract
is subtle: **it returns currently-OPEN orders only.** Cancelled
orders are not in the response. There's no "removal" callback — the
stale entry in exit-mgr's wrapper just sits there forever, with its
old `Submitted` status, until the connection closes.

Our `_ib_find_orders_for_contract` queried exit-mgr's `openTrades()`,
saw the stale `Submitted`, filtered out only `Cancelled/Inactive/
ApiCancelled`, and returned the ghost as a live order.

### Fix

Make `find_open_orders_for_contract` **pool-aware**: query every
connection's `openTrades()`, dedupe by `permId` (globally unique
across clients once assigned), and keep the most-terminal status per
permId.

```python
TERMINAL_RANK = {
    "Cancelled":     4,
    "ApiCancelled":  4,
    "Inactive":      4,
    "Filled":        4,
    "PendingCancel": 3,
    "Submitted":     2,
    "PreSubmitted":  1,
    "PendingSubmit": 0,
}
```

When two connections report the same permId, the higher-ranked status
wins. So scanner-A's `Cancelled` (rank 4) overrides exit-mgr's stale
`Submitted` (rank 2). The merged result is then filtered to drop
anything terminal — the caller only sees genuinely-live orders.

### Key implementation details

1. **Extracted `_ib_find_orders_for_contract_on_conn` as a staticmethod.**
   Same core logic, callable against any `ib_async.IB` instance —
   needed for the fan-out which runs on each pool connection's thread.

2. **New `include_terminal: bool` kwarg.** The merge phase needs to
   see terminal statuses to resolve the conflict, but the pre-existing
   single-connection callers want only live orders. Default is
   `True` on the static method, so the public `find_open_orders_for_contract`
   passes `True` during merging and filters terminal at the edge.

3. **Fallback when pool is None.** Keeps the pre-existing
   single-connection code path alive for tests and any standalone
   usage. Still filters terminal before returning.

4. **`permId` as dedup key, with fallback to `(clientId, orderId)`**
   for pre-submit orders where permId hasn't been assigned yet.

5. **Non-owning fan-out connections can fail the query** (timeout,
   transient IB error) — the code catches at `log.debug` and moves
   on. One connection's view is enough to detect terminal state on
   the next iteration.

### Why this is the right layer to fix it

The stale-cache problem is an `ib_async` implementation detail — not
something our application logic should reason about. Fixing it inside
`find_open_orders_for_contract` (the boundary where the application
reads IB state) means:
- All callers benefit automatically: reconciliation, orphan detection,
  POST-SELL verify, cancel fan-out.
- The rest of the codebase keeps pretending `find_open_orders_for_contract`
  returns the truth. It now does.

Alternative considered — **track every permId returned by
reqAllOpenOrders and treat absence as terminal** — would also work
but requires plumbing a "snapshot watermark" through ib_async's event
model. The pool-fan-out is cheaper and doesn't depend on reading
ib_async internals.

### Tests

`tests/unit/test_find_open_orders_pool_merge.py` — 4 cases:
- Stale `Submitted` on own + fresh `Cancelled` from fan-out → `[]`
- Live `Submitted` with no terminal elsewhere → returned as alive
- No pool → falls back to single-connection filter
- Same permId from multiple connections → deduped

---

## Related & deferred

- **`docs/ib_db_correlation.md` §11** — full context on the sell-first
  close mode, the cross-client cancel asymmetry that motivated it, and
  the stable-clientId routing (ARCH-007) that's the cleaner long-term
  fix.
- **`docs/bracket_cancel_strict_verification.md`** — the legacy
  cancel-first path, still reachable via `CLOSE_MODE_SELL_FIRST=false`.
- **Separate backlog: split `option_selector.select_and_enter_*`**
  into a pure `pick_strike()` and a side-effecting `enter()`. That
  would let `should_roll` know if a meaningfully-different strike is
  even available, and abort upstream before any IB work happens.

---

## What to watch in logs going forward

A **clean close** now looks like:

```
SELL-FIRST: ...
PRE-SELL cancel: firing best-effort cancel for 2 order(s) (non-blocking)
...MKT SELL sent...
POST-SELL verify: all brackets terminal (IB auto-cancelled on position flat)
SELL-FIRST EXIT COMPLETE
```

A **legitimate roll** looks like:

```
ROLL START
SELL-FIRST EXIT COMPLETE
ROLL COMPLETE → <DIFFERENT symbol>
VERIFY CLOSE: skipped — legitimate roll to <symbol>; trusting
              execute_roll's own post-SELL verification
ATOMIC CLOSE COMPLETE
```

A **degenerate same-strike roll** now short-circuits:

```
ROLL START
SELL-FIRST EXIT COMPLETE
ROLL ABORTED — new entry at SAME symbol ... Treating as plain exit;
              closing duplicate position.
(plain exit verify + finalize)
```

If you see the old noise (`brackets_alive_after_close`,
`POST-SELL ... STILL ALIVE`) again, that's a **real** issue — a
bracket genuinely survived both cancel passes — not the cache bug.
Investigate by correlating permIds in `bot.log` against IB logs.
