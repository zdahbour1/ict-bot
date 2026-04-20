# Orphan Bracket Detector — Design

## The problem

A **bracket order** is a parent BUY (entry) plus two SELL children
(take-profit LMT + stop-loss STP) linked by `parentId` and
`ocaGroup`. When the entry fills, the children become working orders
that will eventually fire when price hits TP or SL.

The normal close flow (`_atomic_close` → `execute_exit` →
`cancel_all_orders_and_verify` → sell → `_verify_close_on_ib`)
cancels the bracket children before sending its own close SELL.

**Multiple live incidents have shown this can fail:**
- IWM 04-19 — Fix A (cross-client visibility) landed after this.
- TSLA 04-20 — Fix B (SHORT direction) landed after this.
- MSFT 04-20 — strict cancel verification (commit `363380f`).
- AAPL 04-20 — still happened because the bot was running the
  pre-`363380f` code.

In every case the pattern is identical: the bracket children end up
**orphaned** — still working on IB, no matching open DB trade, no
long position to sell from. When the TP or SL trigger fires, it
sells from zero → **naked short**.

Fix A/B/C and the strict cancel verification prevent the race at
close time, but they're first-line defenses. A **second-line
detector** is valuable for:
1. Orders left orphaned by a bot crash mid-close.
2. Orders surviving IB connectivity glitches where our cancel was
   dropped silently.
3. Manual interference (user opens/closes in TWS out-of-band).
4. Future bugs we haven't found yet.

## Design principles

1. **Multi-phase** — a single snapshot isn't enough. A transient race
   (entry just placed, DB write still committing) can look like an
   orphan for a fraction of a second. The detector must see the same
   orphan across **two separate scans** separated by a **grace
   period** before acting.

2. **Conservative classification** — only flag orders that would
   widen short exposure if they fire. Specifically, a SELL order
   with no matching open DB trade AND no positive IB position to
   sell against. User-placed manual orders (`parentId=0`) are
   never touched.

3. **Stateful across cycles** — the detector keeps a small
   in-memory dict `{orderId → first_seen_ts}`. An order present in
   the current scan gets its timestamp set on first sighting;
   subsequent scans check if `now - first_seen_ts >= GRACE_PERIOD`.
   Orders that disappear (got cancelled, or DB caught up) are
   pruned.

4. **Audit trail** — every orphan cancellation writes a
   `cancel_orphan_bracket` audit row with full context (orderId,
   permId, conId, symbol, parent order, TP/SL level). Visible in
   the UI so the user can see "reconcile cancelled this bracket
   because its parent trade was closed at X."

## Scan lifecycle

```
       ┌─ iteration N:   see working SELL order 4383, conId=999.
       │                 no matching DB open trade, no long position.
       │                 → mark SUSPECT: suspect[4383] = now
       │
       │                 (grace period: 60 seconds)
       │
       ├─ iteration N+1: see 4383 again, still no DB, no position.
       │                 now - suspect[4383] >= 60s  →  CONFIRMED ORPHAN
       │                 → cancel 4383 (strict terminal verification)
       │                 → AUDIT cancel_orphan_bracket
       └─────────────────────────────────────────────────────────────

Alternatively, 4383 can EXIT suspicion at iteration N+1:
   • It's gone from openTrades   →  it cancelled / filled naturally
   • DB now has a matching trade →  adopt path picked it up
   • IB qty for that conId > 0   →  someone opened a long position
   In any of these cases, drop 4383 from suspect and don't act.
```

## What counts as an "orphan"

An order passes ALL of these tests → flagged:

| Test | Value | Why |
|------|-------|-----|
| `status` | in `{Submitted, PreSubmitted, PendingSubmit}` | Terminal states are fine |
| `action` | `SELL` | Only sell-side orders can flip us short |
| `parentId` | `!= 0` | Standalone orders = user-placed, leave alone |
| DB `open` trades | no match on `ib_con_id` | The parent trade is gone |
| IB position qty for `conId` | `<= 0` | No long position to sell legitimately |
| Time-in-suspect | `>= GRACE_PERIOD_SEC` (default 60s) | Avoid races |

## What we deliberately DO NOT flag

- BUY orders (we never go net-short, so a hanging BUY doesn't create
  naked exposure even if wrong).
- Standalone orders (`parentId == 0`) — could be user's manual fills.
- Orders on contracts where we DO hold a long position — that's a
  legitimate bracket or a manual TP/SL the user placed; leaving
  someone's bracket alive is safer than blindly cancelling it.
- Orders matching ANY open DB trade, even if the con_id lookup
  looks mismatched. Defensive default: tie goes to "leave alone".

## Integration point

The detector runs as part of the **periodic reconciliation cycle**
(every `RECONCILIATION_INTERVAL_MIN` minutes, default 1).  After
PASS 1 (close DB-only trades) and PASS 2 (adopt IB-only positions)
finish, the detector gets the final view of both sides and runs
orphan detection.

So the full reconcile flow becomes:

```
PASS 1: DB-open trades not on IB → close with RECONCILE reason
PASS 2: IB positions not in DB   → adopt into DB
PASS 3 (NEW): working SELL orders without matching open trade/position
             → suspect now (if first sighting) or cancel (if aged-out)
```

## Cancellation protocol

When an orphan is confirmed, we cancel it using the same strict
verification helper from `exit_executor.cancel_all_orders_and_verify` —
cancel + poll for terminal state + retry on revert.  If after the
retry budget the cancel didn't stick, log at **error** level and
leave it for the next reconcile cycle; don't spin forever.

## Audit semantics

New canonical action in `strategy.audit`:

```
cancel_orphan_bracket    reconciliation    orderId, permId, conId,
                                            symbol, action (SELL),
                                            orderType (LMT / STP),
                                            age_suspected_sec,
                                            price_level (lmt/aux)
```

`trade_id` is `None` for orphan actions — that's the whole point,
there's no matching trade. The audit row still shows up in
`/api/system-log` and can be surfaced in the Threads page System
Log under the `reconciliation` component filter.

## Edge cases

### The detector itself is stateful — restarts lose the suspect table

When the bot restarts, the suspect dict is empty.  First reconcile
cycle after restart will see any orphan afresh → mark suspect → one
full grace period (60s) before acting.  So worst case after a crash
or restart: orphan gets up to ~ `RECONCILE_INTERVAL + GRACE_PERIOD`
seconds of extra life.  Acceptable given rarity.

### Cancel-verification can itself fail

If `cancel_order_by_id` is called but the cancel reverts (the MSFT
mechanism), the order stays in suspect until next cycle. The
detector's retry loop handles this — strict verification inside
the cancel call refuses to return True until terminal.

### False positive: an order in suspect that SHOULDN'T be cancelled

The grace period (60s) + multi-phase check (must be observed twice)
makes a false positive extremely unlikely in practice. But as a
safeguard: the cancel action goes through the normal cancel API,
which respects bracket protection. A human using TWS to manually
override a trade would see our cancel attempt in the TWS order
history — nothing destructive happens beyond cancelling a SELL.

## Tunable constants (settings table, strategy_id NULL)

| key | default | meaning |
|-----|---------|---------|
| `ORPHAN_GRACE_PERIOD_SEC` | 60 | time an order must remain orphaned |
| `ORPHAN_DETECTOR_ENABLED` | true | master kill-switch |
| `ORPHAN_AUTO_CANCEL` | true | if false: log only, don't cancel |

Starting the detector in a **log-only mode** (`ORPHAN_AUTO_CANCEL=false`)
for the first session after deployment is a reasonable caution.  If
no false positives show up over a market session, flip to auto-cancel.

## Tests

Unit tests (mocked IB):
- Orphan seen once → marked suspect, NOT cancelled
- Orphan seen twice across grace period → cancelled + audit written
- Order with matching DB trade → never flagged
- Order with positive IB position → never flagged
- Standalone order (`parentId=0`) → never flagged
- Order that disappears before second scan → suspect cleared
- `ORPHAN_AUTO_CANCEL=false` → detected but not cancelled, audit row
  still written with action `orphan_detected_not_cancelled`
- BUY orders → never flagged

## Related docs

- `docs/roll_close_bug_fixes.md` — Fix A/B/C first-line defenses
- `docs/bracket_cancel_strict_verification.md` — MSFT fix
- `docs/logging_and_audit.md` — audit trail pattern extended with
  `cancel_orphan_bracket`
