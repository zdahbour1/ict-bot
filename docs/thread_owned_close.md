# Thread-Owned Close: Who Opens, Closes

**Status:** design, pending approval
**Motivation:** eliminate cross-client bracket-cancel issues at the
root by routing every close operation to the same IB client that
placed the bracket.
**Alternative considered:** continue using the cross-client fan-out
(`1a15d50`) — it works, but it's a workaround that papers over the
underlying asymmetry between entry and exit threads.

---

## The core problem

Today's architecture:

```
┌─────────────────────┐           ┌─────────────────────┐
│ Scanner thread      │           │ Exit manager thread │
│ (IB clientId = N+1) │           │ (IB clientId = N)   │
└──────────┬──────────┘           └──────────┬──────────┘
           │ detects signal                  │ monitors open trades
           │ places bracket                  │ detects TP/SL/roll/time
           │ (parent BUY + TP/SL children)   │ trigger
           │                                 │
           └─────────────┐       ┌───────────┘
                         ▼       ▼
                    ┌──────────────────┐
                    │   IB / TWS       │
                    │   (same account) │
                    └──────────────────┘
                                ▲
     Exit manager sends cancelOrder for a child placed by scanner.
     IB returns Error 10147 — the exit manager's clientId didn't
     place this order, so from its perspective the order "doesn't
     exist". Cancel silently reverts.
```

Cross-client fan-out (shipped) makes cancels work by broadcasting
to every pool connection — the owning client eventually processes
it. But it still has costs:

- Each fan-out wastes 2 of 3 cancel calls (10147 on non-owners)
- Race windows where the cancel lands on a non-owner first, owner
  later → observable as "PendingCancel thrash" in logs
- New code paths in the exit flow have to be aware of cross-client
  semantics; easy to reintroduce subtle bugs

The cleaner model: **a trade is owned by the client that placed it.
All subsequent order operations on that trade happen on that same
client.** Exit manager's job becomes orchestration, not execution.

## Model

```
┌─────────────────────┐    close message    ┌─────────────────────┐
│ Exit manager        │ ───────────────────▶│ Scanner thread      │
│ (observer)          │                      │ (executor)          │
│                     │◀───── result ─────── │                     │
│ - monitors trades   │                      │ - runs close on own │
│ - atomic DB lock    │                      │   IB client         │
│ - decides close     │                      │ - cancels brackets  │
│ - waits for result  │                      │ - sends close SELL  │
│ - finalizes DB      │                      │ - verifies + reports│
└─────────────────────┘                      └─────────────────────┘
```

### Responsibilities

| Today                                    | Proposed                                 |
|------------------------------------------|------------------------------------------|
| Exit manager runs `execute_exit` directly | Exit manager sends a `CloseRequest` to the owning scanner thread |
| Scanner focuses on scanning only          | Scanner processes CloseRequest messages in-between scan cycles |
| Bracket cancels cross-client              | Bracket cancels stay on the placing client — no 10147, no fan-out |
| `IBClient.cancel_order_by_id` fans out    | Still exists as a fallback; rarely used |

## Message protocol

Define a small, typed message:

```python
# strategy/close_request.py
from dataclasses import dataclass
from typing import Optional
import threading

@dataclass
class CloseRequest:
    trade_id:     int                # db_id
    reason:       str                # 'TP', 'SL', 'ROLL', 'MANUAL', 'TIME_EXIT'
    should_roll:  bool
    pnl_pct:      float
    current_price: float
    reason_detail: Optional[str] = None

    # Set by the scanner thread after execution
    completed:    threading.Event = None
    result:       dict = None        # {ok: bool, exit_price, brackets_cleared, error?}

    def __post_init__(self):
        if self.completed is None:
            self.completed = threading.Event()
```

Each `IBConnection` gets an additional inbox:

```python
class IBConnection:
    ...
    self._close_inbox: queue.Queue[CloseRequest] = queue.Queue()
```

Between scans, each scanner thread drains its inbox and executes
pending close requests on its own IB client.

## Database support

Add one column to `trades`:

```sql
ALTER TABLE trades
  ADD COLUMN owning_client_id INTEGER;
```

- Written at entry time (`add_trade`): the IB clientId that actually
  placed the bracket.
- Read at close time by the exit manager to look up the owning
  connection in the pool.
- NULL for legacy rows / adopted orphans; fallback to cross-client
  fan-out in that case (graceful degradation).

## Exit-manager flow under the new model

```python
def _atomic_close(self, trade, current_price, result, reason, pnl_pct,
                  should_roll, reason_detail=""):
    # Step 1-2: unchanged (DB lock + read current state)
    session, locked_data = lock_trade_for_close(trade_id)
    ...

    # Step 3 (CHANGED): delegate IB work to the owning thread
    owning_client_id = locked_data.get('owning_client_id')
    owning_conn = pool.find_connection(owning_client_id)

    if owning_conn is None:
        # Orphan / unknown owner — use the fan-out path (today's code).
        # This keeps ARCH-005 guarantees for adopted trades and old rows.
        execute_exit(self.client, live_trade, reason)
        rolled = None
    else:
        # Normal path: scanner thread executes the close on its client.
        req = CloseRequest(trade_id=trade_id, reason=reason, ...)
        owning_conn.post_close_request(req)
        req.completed.wait(timeout=30)   # close is fast; cap the wait
        if not req.result or not req.result.get('ok'):
            # Owning thread failed or timed out — don't finalize.
            # Log, release lock, retry next cycle.
            release_trade_lock(session)
            return
        rolled = req.result.get('rolled_trade')  # if it was a roll

    # Step 4+: verify_close_on_ib, enrichment, finalize_close
    # (unchanged — still runs on the exit manager's thread)
    ...
```

## Scanner-thread inbox processing

Scanners run a scan cycle every `SCAN_INTERVAL` seconds (default 60).
Between cycles they poll the inbox:

```python
def _scan_loop(self):
    while not self._stop_event.is_set():
        # Process any pending close requests FIRST — they're
        # time-sensitive (TP/SL may be about to fire on the bracket).
        self._drain_close_inbox()

        # Regular scan cadence
        if time_for_scan():
            self._run_scan()

        time.sleep(0.5)

def _drain_close_inbox(self):
    while True:
        try:
            req = self._conn._close_inbox.get_nowait()
        except queue.Empty:
            return
        try:
            result = self._execute_close_locally(req)
            req.result = result
        except Exception as e:
            req.result = {"ok": False, "error": str(e)}
        finally:
            req.completed.set()
```

## Ordering guarantees

- A scanner thread cannot place a NEW entry while processing a
  CloseRequest on the same ticker (naturally serialized by the
  single thread per ticker).
- Multiple close requests for different tickers on the same
  scanner connection serialize FIFO — fine, they're independent.
- Exit manager waits (with timeout) on `req.completed` before
  moving on to verification/finalize — same atomic semantics as
  today.

## Failure modes

| Failure | Behavior today | Behavior with new model |
|---------|----------------|--------------------------|
| Owning thread dead | N/A | Detect via `conn.connected` check; fall back to fan-out path |
| Owning thread slow / blocked on a scan | Close runs on exit mgr, may fail cross-client | Close waits up to 30s in inbox; if timeout, release lock, retry next exit cycle |
| Owning thread disconnected from IB mid-close | Cancels fail silently (current bug) | Connection monitor reconnects; next retry lands cleanly |
| Adopted trade (no owning_client_id) | Cross-client fan-out | Same — graceful fallback |
| Close triggers during scanner's own entry flow | Race, partially handled | Naturally serialized: close runs AFTER the scanner finishes its current op |

## Phased rollout

### Phase 0 — current
Cross-client fan-out (`1a15d50`) makes cancels work today. This is
the safety net. **No change required to ship.**

### Phase 1 — record owning_client_id
- DDL: add `trades.owning_client_id`
- `add_trade` writes it from the placing client
- No consumers yet; data gathered for Phase 2
- **Outcome:** in a week, 100% of new trades have the field
  populated; we can start switching

### Phase 2 — `CloseRequest` plumbing
- `CloseRequest` dataclass, `IBConnection.post_close_request`,
  scanner `_drain_close_inbox`
- Gated behind `USE_THREAD_OWNED_CLOSE = False` config flag
- Unit tests for the message protocol (mocked IB)

### Phase 3 — rollout
- Flip `USE_THREAD_OWNED_CLOSE = True`
- `_atomic_close` looks up owner; if found, uses CloseRequest path;
  else falls back to fan-out (safety net stays)
- Monitor audit trails for one full session before declaring done

### Phase 4 — decommission the fan-out for owned trades
- Once every trade has `owning_client_id` and the CloseRequest
  path has a week of clean operation, the fan-out becomes
  orphan-only code. Keep it for reconcile's orphan cancellation
  (where no trade ownership exists); remove from the regular
  close path.

## Testing strategy

- **Unit:** `CloseRequest` round-trip, inbox draining, timeout behavior,
  missing-owner fallback.
- **Integration (mocked IB):** full `_atomic_close` with ownership
  lookup + scanner-side execution; simulate owning-thread failure
  and verify fallback.
- **Live smoke (Phase 3 gate):** one normal close + one roll + one
  manual close + one TIME_EXIT. Audit trail must show
  `action=close:*` rows from the SCANNER actor, not the exit
  manager. Watch for zero `Error 10147` in bot.log across the
  full session.

## What this does NOT change

- **Monitoring** stays on the exit manager thread. It's the cheap
  part (a price poll + some math); scanners shouldn't be in that
  hot loop.
- **ARCH-005** (atomic close) is preserved end-to-end — the DB
  lock is held by the exit manager throughout the wait; the
  finalize happens only after the owning thread confirms.
- **Reconciliation** stays as-is — it already operates on its own
  orchestration model.
- **The orphan detector** (PASS 3) still exists and handles cases
  where the above protocol doesn't reach (adopted positions, bot
  restarts mid-close, etc.).

## Open questions

1. **Latency.** Does waiting up to 30s for the owning thread
   introduce unacceptable delay on a time-critical ROLL? My
   intuition: no — rolls are triggered by monitor findings that
   aren't sub-second sensitive, and the scan interval between
   inbox polls is 500ms. But worth measuring once.
2. **Multiple tickers per connection.** `get_scanner_connection`
   maps ticker → connection deterministically. If a scanner
   connection hosts 10 tickers, one slow close could delay
   unrelated entries. Mitigation: spawn a dedicated close-worker
   thread per connection if this becomes a problem.
3. **Reconcile close (RECONCILE reason).** Does reconcile also
   send CloseRequests, or does it use the fan-out? Leaning
   toward fan-out since reconcile-closes are already "anomaly"
   paths and the orphan detector handles the order side.

## Related docs

- `docs/roll_close_bug_fixes.md` — initial A/B/C fixes
- `docs/bracket_cancel_strict_verification.md` — the MSFT fix
- `docs/orphan_bracket_detector.md` — second-line cleanup
- `1a15d50` (cross-client fan-out) — the workaround this replaces
