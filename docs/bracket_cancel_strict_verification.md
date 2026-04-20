# Bracket-Cancel Strict Verification (MSFT incident 2026-04-20)

## The incident

MSFT 417.5C rolled to 420C at 09:41. The close succeeded on paper,
the new leg opened, but ~64 seconds later the original TP limit
order fired at $2.42, selling 2 more contracts at zero position and
leaving the user **short 2 naked calls** — same outcome as the IWM
incident yesterday, different mechanism.

## The mechanism (different from the IWM/TSLA bugs)

Yesterday's Fix A (`reqAllOpenOrders`) made cross-client brackets
visible. Today they were visible. The bot found them, issued cancels,
and STEP 3 logged `All orders CANCELLED (verified after 0.5s)`.

**But the cancels weren't actually terminal.** They were in
`PendingCancel` — cancel-requested, not yet acknowledged. STEP 3's
filter only rejected `("Submitted", "PreSubmitted", "PendingSubmit")`
and treated everything else — including `PendingCancel` — as
"cancelled, safe to proceed."

Timeline from bot.log:

```
09:41:42.508  orderId=4383 TP → PendingCancel  (cancel sent)
09:41:42.616  orderId=4384 SL → PendingCancel  (cancel sent)
09:41:43      STEP 3: "All orders CANCELLED"  ← declared victory prematurely
09:41:43      STEP 5 close SELL 2x fills @ $2.20  → position 2 → 0
09:41:43.901  orderId=4383 flips back:
              PendingCancel → PreSubmitted → Submitted
              IB REVERTED the cancel.
09:41:43.901  orderId=4384 flips back: PendingCancel → PreSubmitted
09:41:51      Fix C straggler sweep runs, finds both alive,
              issues cancels again → PendingCancel (again)
09:42:47      orderId=4383 (LMT SELL @ $2.42) hits the market price
              and FILLS before the second cancel completes.
              Position 0 → **-2 short**.
```

So there are actually TWO bugs, layered:

1. **Primary — premature cancel-verification:** `PendingCancel`
   treated as terminal. The original close SELL fires while brackets
   are still alive on IB.

2. **Secondary — IB reverts cancels unpredictably:** our cancel
   requests can be rejected by IB and the orders silently reactivate.
   This happened twice for 4383 in the MSFT timeline. Today's Fix C
   straggler sweep hit the same issue — the re-issued cancel was
   also incomplete when the LMT filled.

## Why Fix A + Fix C alone didn't save us

- **Fix A** (`reqAllOpenOrders`) worked — the brackets were found.
- **Fix C** (`verify_close_on_ib` straggler sweep) worked — the
  resubmitted brackets were detected at 09:41:51 and re-cancelled.
- Both fixes ASSUME that issuing a cancel = the order is gone.
  That's the bad assumption. Cancels are asynchronous and can be
  reverted by IB.

## The proper fix

### Change 1 — Strict terminal-state verification

Only consider an order safe if its status is in
`{'Cancelled', 'ApiCancelled', 'Inactive', 'Filled'}` OR the order
is no longer present in `openTrades()` at all. `PendingCancel`
explicitly does NOT count — we keep polling.

```python
SAFE_STATES = {"Cancelled", "ApiCancelled", "Inactive", "Filled"}
# NOT safe: "Submitted", "PreSubmitted", "PendingSubmit", "PendingCancel"
```

### Change 2 — Detect cancel reverts and retry

If during polling we see an order transition from `PendingCancel`
back to `Submitted` / `PreSubmitted`, that's IB rejecting the cancel.
Re-issue it. Cap at 3 retry rounds with fresh `reqAllOpenOrders`
each round. After 3 failed rounds, ABORT the close — don't send
the SELL. The trade stays open; next exit cycle will retry.

### Change 3 — Safety net: detect negative position after close

Even with Changes 1+2, there's a small remaining race: a working
LMT SELL at a specific price can fill in the milliseconds between
our position check and IB processing our cancel. The _verify_close
path (Fix C) detects `position == 0` and returns success. But
what if `position == -N`? That means a bracket fired AFTER our
flatten, flipping us short.

New behavior in `_verify_close_on_ib`:

- Existing: position == 0 → sweep stragglers → return True
- **NEW**: position < 0 → **we're naked short, audit an error,
  attempt defensive BUY to restore flat, return False**

Defensive BUY:
- Only fires when position is negative AND `abs(position) == original
  contract count` (i.e., we know exactly one bracket fired)
- Uses a MARKET order to eat the short immediately
- Writes `AUDIT short_recovery_buy` with the full context
- If the BUY also fails → emergency-stop the bot (can't trust state)

This is defensive code that SHOULD never fire if Changes 1+2 work.
But if they ever don't, this is the last line before naked exposure.

## What this does NOT do

- **Doesn't prevent IB from reverting cancels**. That's a TWS /
  account setting issue. We see `Error 10349 — Order TIF was set
  to DAY based on order preset` repeatedly in the log; the account's
  order preset is overriding our cancel TIF and causing reverts.
  Workaround: avoid the revert by retrying, accept the occasional
  failure, use defensive BUY on the rare negative-position race.
- **Doesn't change the bracket approach**. A more robust design
  would attach the close SELL to the same `ocaGroup` as the
  bracket so it naturally participates in the "one cancels all"
  protocol. That's a bigger refactor; the strict-verification +
  defensive-BUY combination closes the hole without requiring it.

## Tests

Every scenario above gets a unit test with a mocked BrokerClient:

- PendingCancel is not treated as terminal
- Cancel revert (PendingCancel → Submitted) triggers a retry
- Max retries exhausted → abort returns False
- Negative position detected in verify_close → defensive BUY issued
- Defensive BUY failure → audit logged at error level

## Deployment

Same branch (`feature/profitability-research`). Bot needs restart to
pick up. Unit tests cover both primary and secondary changes.

## Related docs

- `docs/roll_close_bug_fixes.md` — the A/B/C fixes from yesterday
- `docs/logging_and_audit.md` — the audit-trail pattern this uses
- `docs/market_hours_validation.md` — adds a new gate: **zero
  short-recovery-buy events during a clean roll**
