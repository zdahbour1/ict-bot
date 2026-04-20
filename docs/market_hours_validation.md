# Market-Hours Validation Plan
**Branch:** `feature/profitability-research`
**Purpose:** confirm the weekend's live-code changes are safe to merge.

Everything below is live-path work that unit tests cover in principle
but need real market exercise to declare "done".  Run through this
checklist on any market session before the merge.

Each item has:
- **What we shipped**
- **How to exercise it**
- **What to look for (pass)**
- **What would indicate a problem (fail)**

---

## A. Fix A — Cross-client bracket visibility *(commit `3eda3b8`)*

**What we shipped.** `BrokerClient.refresh_all_open_orders()` calls
`ib.reqAllOpenOrders()` so the exit-manager's IB client (clientId=1)
can see brackets placed by the entry-manager's IB client (clientId=3).
Called once at STEP 0 of `cancel_all_orders_and_verify`.

**Exercise:** any normal trade that closes (TP / SL / roll / manual).
Doesn't matter which — every close hits this path.

**Pass:**
```
bot.log →  [TICKER] STEP 0: reqAllOpenOrders → N trades visible (cross-client merge)
           [TICKER] STEP 1: Finding ALL open orders for conId=...
           [TICKER] STEP 2: Found 2 order(s), cancelling all: [TP, SL]
           [TICKER] STEP 3: All orders CANCELLED (verified after 0.5s)
```
Where `N > 0` and STEP 2 finds the brackets instead of "No orders
found but brackets EXPECTED."

**Fail:**
- STEP 1 still logs "No orders found but brackets EXPECTED" — the refresh
  isn't working or reqAllOpenOrders is ignored by this IB build.
- After the close, `verify_close_ok` reports stragglers cancelled — means
  brackets *did* survive the cancel step. Still recoverable (the straggler
  sweep catches them) but investigate why the pre-sell cancel missed.

---

## B. Fix B — Direction-check for SHORT positions *(commit `3eda3b8`)*

**What we shipped.** A bearish ICT signal (direction=SHORT → long puts)
with `ib_qty > 0` no longer trips "EXECUTE EXIT ABORTED — direction
mismatch." The check now aborts only on `ib_qty < 0` (net-short, which
we don't trade today).

**Exercise:** any put trade that closes (any exit reason).

**Pass:**
```
bot.log →  [TICKER] EXECUTE EXIT START — reason=... direction=SHORT contracts=2
           [TICKER] CLOSE STEP 4 RESULT: IB position qty=2
           [TICKER] CLOSE STEP 5 RESULT: Sell order sent — 2x TICKER... (SHORT)
```

**Fail:**
- "EXECUTE EXIT ABORTED — NEGATIVE position on IB (qty=-N)" — that means
  we somehow ended up net-short before the close. A pre-existing bug, not
  a regression from our fix.  Capture the audit trail of that trade ID
  immediately (`/api/trades/{id}/audit`) so we can diagnose.

---

## C. Fix C — Verify close + straggler sweep *(commit `3eda3b8`)*

**What we shipped.** `_verify_close_on_ib()` polls IB up to 3s for
position=0, sweeps any leftover working orders, then (only then) lets
`_atomic_close` call `finalize_close`. Failure releases the DB lock
without marking the trade closed — the next exit cycle retries.

**Exercise:** same — any close. Also any roll.

**Pass (happy path):**
```
bot.log →  [TICKER] VERIFY CLOSE: position=0 after 0.5s — sweeping stragglers
           [TICKER] ATOMIC CLOSE COMPLETE: db_id=X → WIN (REASON)
system_log → audit row: verify_close_ok
             audit row: close:<REASON>
```

**Pass (recovery path — stragglers found):**
```
bot.log →  [TICKER] VERIFY CLOSE: 1 straggler orders found after close —
           cancelling defensively: [3095]
```
This is the mechanism that would have prevented the IWM short on 04-20.
If you see this line fire in the log even once, the fix paid off.

**Fail:**
```
[TICKER] VERIFY CLOSE FAILED: position still 2 after 3s — releasing
DB lock, will retry next cycle
```
Not necessarily a bug — if IB is slow to fill, a single retry can happen.
But if the same trade emits this line 3+ times in a row, the close is
genuinely stuck. Stop the bot, capture `/api/trades/{id}/audit`, and
close manually in TWS.

---

## D. Roll flow end-to-end *(bugs A+B+C combined)*

**What we shipped.** The IWM and TSLA incidents from 04-20 happened
during rolls.  The combined fix means a roll should now either complete
cleanly (close old → open new) or abort cleanly (close old fails →
retry, don't orphan).

**Exercise:** a roll has to trigger. Options:
1. Wait for a real roll at the default `roll_pct`.
2. **For testing only**, lower `settings.roll_pct` on ONE strategy to
   force a roll. Revert before leaving the session running.

**Pass:**
The audit trail for the rolled trade shows in order:
```
AUDIT open            (entry)
AUDIT verify_close_ok (close side of the roll verified)
AUDIT close:ROLL      (DB marks old leg closed)
AUDIT roll_open       (new leg opened, from_trade_id=<old>)
```
And the new leg has its own `AUDIT open` row. On the Trades tab,
clicking **Audit** on either leg shows the chain from both sides.

**Fail:**
- `AUDIT roll_open` missing → the close worked but the new entry failed.
  Not a regression — the old behavior. Acceptable; just means the roll
  half-completed. Bot continues monitoring the closed side.
- `AUDIT verify_close_fail` followed by a `reconcile_adopt` a minute
  later for a position on the same contract → close actually failed
  and reconcile picked up the orphan. This is the incident pattern
  we're trying to eliminate. If this happens, stop and diagnose.
- Any `reconcile_adopt` with a SHORT qty (like IWM -2) → the TP/SL
  from the old leg fired after we "closed" and flipped us short. That
  means Fix A/C didn't prevent it. Stop immediately.

---

## E. Audit trail completeness *(commit `b2ee7c5`)*

**What we shipped.** Every trade-state transition writes a
`system_log` row via `strategy.audit.log_trade_action` with
`details.trade_id`, `action`, `actor`, `py_thread`, plus extras.

**Exercise:** any trade lifecycle. Also observe reconcile startup.

**Pass.** Each of the following transitions MUST produce an audit row
(verify with the Audit button on the Trades tab, or the
`/api/trades/{id}/audit` endpoint):

| Transition | Expected action | Written by |
|---|---|---|
| Trade opens | `open` | `scanner-TICKER` |
| Verify close pre-finalize | `verify_close_ok` | `exit_manager` |
| Verify close failed | `verify_close_fail` | `exit_manager` |
| DB-side close | `close:TP` / `close:SL` / `close:ROLL` / `close:MANUAL` | `exit_manager` |
| Roll opens new leg | `roll_open` (with `from_trade_id`) | `exit_manager` |
| Startup reconcile closes orphan | `reconcile_close` | `reconciliation` |
| Startup reconcile adopts IB orphan | `reconcile_adopt` | `reconciliation` |

**Fail.** Any closed trade whose Audit modal shows ONLY an `open` row
but no `close:*` row.  That means the closing component bypassed
`log_trade_action`.  Report the trade ID and the closing reason so
we can instrument the gap.

---

## F. Entry-manager thread visibility *(commit `0ddd345`)*

**What we shipped.** Shared `entry-manager` thread_status row, updated
at each stage: `preflight` → `placing` → `filled` / `blocked` /
`failed`. Corresponding system_log rows tagged `entry-manager`.

**Exercise:** any signal firing.

**Pass:** Threads page shows an `entry-manager` row that updates within
~1s of each signal, with a message like
`PLACING: INTC — LONG_OB CALL @ $59.87`.  After fill:
`FILLED: INTC — INTC260425C00060000 @ $1.45`.

Filter System Log by `component=entry-manager` — you see a complete
timeline of entry activity across all tickers.

**Fail:** Entry manager stuck showing `idle` while `scanner-X` rows
cycle from signal to blocked — means the hook didn't fire.

---

## G. Observability polish *(commit `20287ac`)*

Three cosmetic improvements that only need eyeballing once.

### Timestamps on Threads page
- Open Threads → System Log panel.
- Every timestamp renders as `MM-DD HH:MM:SS PT` with the header note
  "(times shown in Pacific Time)".
- Day boundary visible if you scroll back to yesterday's entries.

### Signal → order log trail in bot.log
On the next signal, bot.log should show consecutively:
```
[TICKER] ICT SIGNAL: LONG_OB ... (the big banner)
[TICKER] SIGNAL→ORDER: LONG_OB entry=$... sl=$... tp=$... — running pre-flight
[TICKER] PLACING ORDER: signal=LONG_OB leg=CALL (...)
[IB] BRACKET BUY: 2x TICKER... — parent=... TP=<id> @ $<price> SL=<id> @ $<price>
[TICKER] Trade #1/8 opened: TICKER...
```
The bracket log line must include `@ $<price>` for both TP and SL. That
data was invisible before — without it you couldn't have caught the
stale-$0.38-SL that ate IWM on 04-20.

### Reconcile summary detail
The periodic reconcile (every 60s) now logs:
```
[RECONCILE] Done: closed=0, adopted=0, IB=N, DB was=N, DB now=N
```
When activity occurs, it appends named items:
```
...closed=1, adopted=1, IB=17, DB was=17, DB now=17
 | closed: [TSLA TSLA260420P00397500 db_id=939 WIN(+23.2%)]
 | adopted: [TSLA TSLA...P00397500 2x@$4.49 SHORT db_id=951]
```
Trigger by stopping/restarting the bot (startup reconcile clears all
DB-only stragglers and will populate the `closed:` section with
specifics instead of just a count).

---

## H. Sweep launch UI *(commit `74bbfe4`)*

**What we shipped.** Purple "+ Parameter Sweep" button + bot_manager
`/run-sweep` endpoint.  Form lets you grid-sweep PT/SL/interval/DTE.

**Exercise:**
1. Make sure the host `bot_manager.py` restarted recently (it did,
   yesterday).
2. Open Backtest tab → click `+ Parameter Sweep`.
3. Use a tiny grid (2 cells) and 5-day window to smoke-test.
4. Click Launch.

**Pass:** Status 202 in the browser dev tools; within ~30s the runs
table shows 2 new rows appearing.  `sweep.log` on the host shows the
subprocess finished.

**Fail:** `/api/backtests/sweep/launch` returns 404 → sidecar still
running old code. Restart `bot_manager.py`.

---

## I. Backtest Analytics end-to-end *(commits `7fbbd00`, `d7087b1`, `dcd5ef7`, `01b69ba`, `3819f46`, `c20ce7b`, `d042044`)*

These don't need market hours, but are worth a final walk-through:

1. Backtest tab → **Analytics panel → Charts view** — click any bar →
   chips appear above runs table, whole page reslices (stats, charts,
   tables).
2. Click the same bar again → filter clears (toggle behavior).
3. Column header on RunsTable — **sort** re-queries backend across all
   runs (network tab confirms).
4. Column filter input on RunsTable — 300ms debounce, then server
   re-query.
5. **Analytics → Feature Importance** view — toggle entry/exit source,
   adjust min_trades, quartile tiles color-code vs baseline.
6. Click any run row → **TradesModal** opens with same sortable/
   filterable columns, server-side sort works inside the modal.
7. Top 15 Runs chart → clicking a bar opens the TradesModal for that
   specific run.

---

## Merge gate

To merge this branch into the integration target, the following must
hold after a live session:

- [ ] Sections **A, B, C, D, E** each observed at least one pass cycle
      on a live trade.
- [ ] Zero `AUDIT verify_close_fail` rows — or every failure followed
      by a successful retry within 60s.
- [ ] Zero `AUDIT reconcile_adopt` rows during active trading (adoption
      at startup is fine — it's the cleanup mechanism).
- [ ] Zero trades closed with `status=closed` but no matching
      `AUDIT close:*` audit row.
- [ ] Sections **F–I** eyeballed once and match the "Pass" descriptions.

If anything in the list fails, capture the offending trade's audit
trail and the relevant bot.log window, then stop and debug.
