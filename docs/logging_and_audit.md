# Logging & Audit Trail — Design & Usage Guide

**Audience:** operators investigating a live trade, and developers
adding new components that touch trade state.

**Core promise:** every action that mutates a trade is recorded with
enough context to reconstruct the full "who did what, when, why"
timeline — across every thread that touched it, from open to close.
No mystery actions.

---

## 1. The three layers of logging

The bot writes logs at three layers. Each has a different purpose and
lifetime.

| Layer | Where | What | Lifetime | Grep-able? |
|-------|-------|------|----------|------------|
| **Python logger** | `bot.log` file + console | Every `log.info/warn/error` call. High volume, low structure. | Rotates when the bot restarts (appended across sessions). | Yes, plain text |
| **`system_log` table** | Postgres | State-change events + errors + periodic summaries. Medium volume, structured `details` JSONB. | Persistent; no rotation today. | Yes, SQL |
| **Audit trail** *(subset of `system_log`)* | Postgres | State transitions on a specific trade. Every row carries `details.trade_id` so the UI can reconstruct a per-trade timeline. | Persistent. | Yes, SQL or UI |

**The audit trail is a pattern, not a separate table.** Rows written
via `strategy.audit.log_trade_action()` live in `system_log` but
follow a fixed schema that makes them trivially filterable.

---

## 2. Audit row schema

Every audit row has:

```python
{
    "component": "<actor>",          # scanner-INTC, exit_manager, reconciliation, ...
    "level":     "info|warn|error",
    "message":   "[AUDIT <action>] <human-readable sentence>",
    "details":   {
        "trade_id":  <int | None>,   # primary filter key
        "action":    "<canonical keyword>",
        "actor":     "<same as component>",
        "py_thread": "<Python thread.name>",
        # ... plus action-specific extras (ticker, symbol, prices, permIds)
    },
    "created_at": <timestamp with time zone>   # auto, UTC in DB
}
```

The `"[AUDIT <action>]"` prefix on `message` is deliberate: you can grep
`bot.log` for `AUDIT ` and see every state transition with zero
ambiguity.

### Canonical `action` vocabulary

| Action | Written by | Meaning |
|--------|------------|---------|
| `open` | `scanner-TICKER` (runs on scanner thread, calls `TradeEntryManager.enter()`) | Trade opened on IB + registered in DB. Carries bracket IDs, entry price, signal details. |
| `close:TP` | `exit_manager` | Closed via take-profit bracket fill |
| `close:SL` | `exit_manager` | Closed via stop-loss bracket fill |
| `close:ROLL` | `exit_manager` | Closed as part of a roll (new leg opens separately with `roll_open`) |
| `close:MANUAL` | `exit_manager` | Closed via dashboard command |
| `close:RECONCILE` | `exit_manager` | Closed because reconciliation detected it was gone from IB *(alternative to `reconcile_close` when the close flows through the atomic-close path)* |
| `roll_start` | `exit_manager` | Roll triggered, about to close old leg |
| `roll_open` | `exit_manager` | New leg opened as part of a roll. `details.from_trade_id` points at the closed leg. |
| `roll_abort` | `exit_manager` | Roll couldn't complete (close failed). Trade stays open. |
| `cancel_bracket` | `exit_manager` / `exit_executor` | TP or SL order cancelled during close prep |
| `reconcile_close` | `reconciliation` | DB-open trade not found on IB → closed with a RECONCILE exit reason |
| `reconcile_adopt` | `reconciliation` | IB position found without a DB row → new trade row created. Logged at `warn` because adoption means DB↔IB mismatch. |
| `verify_close_ok` | `exit_manager` | Post-close IB verification passed (position=0, stragglers swept) |
| `verify_close_fail` | `exit_manager` | Post-close verification failed → DB lock released, will retry (level=error) |

Any NEW state-changing action should pick an existing keyword or extend
this list (and update this doc).

---

## 3. How threads touch a trade — the actors

The bot is multi-threaded. The audit trail is the only place where you
can see all the threads that touched one trade, in order.

```
┌─────────────────────────────────────────────────────────────────┐
│  Scanner thread (scanner-INTC)                                  │
│    pulls bars → signal_engine detects signal                    │
│    → calls TradeEntryManager.enter()                            │
│       ├─ audit: AUDIT open ...                                  │
│       └─ broker/ib_client places bracket (clientId=3)           │
└─────────────────────────────────────────────────────────────────┘
                    │
                    ▼ (trade is now live)
┌─────────────────────────────────────────────────────────────────┐
│  Exit manager thread (exit_manager)                             │
│    periodic MONITOR on every open trade                         │
│    when TP/SL/ROLL triggers:                                    │
│       → _atomic_close() locks DB row                            │
│       → execute_exit() / execute_roll() on broker (clientId=1)  │
│       → _verify_close_on_ib() polls IB                          │
│          ├─ audit: AUDIT verify_close_ok | verify_close_fail    │
│          └─ cancel stragglers (cross-client, see Fix A)         │
│       → finalize_close() in DB                                  │
│          └─ audit: AUDIT close:<REASON>                         │
│       → for a roll: add_trade(new_leg)                          │
│          └─ audit: AUDIT roll_open  (with from_trade_id)        │
└─────────────────────────────────────────────────────────────────┘
                    │
                    ▼ (defense in depth)
┌─────────────────────────────────────────────────────────────────┐
│  Reconciliation thread (reconciliation)                         │
│    Every 60s:                                                   │
│      PASS 1 - DB rows not on IB → close with RECONCILE reason   │
│         └─ audit: AUDIT reconcile_close                         │
│      PASS 2 - IB positions not in DB → adopt as new trade       │
│         └─ audit: AUDIT reconcile_adopt                         │
└─────────────────────────────────────────────────────────────────┘
```

Each of those threads writes to the same `system_log` table. The audit
query stitches them together by `trade_id`.

### About the entry manager

`TradeEntryManager` is *not* its own OS thread — it runs on the scanner
thread that called it. But entries take the account-wide
`entry-manager` thread row to publish their current stage (see the
separate **Threads page** feature) so you can see entry activity across
all tickers in one place.

---

## 4. Pulling a trade's timeline

### From the UI

1. **Trades tab** → any row (open or closed) → click **Audit** button
2. Modal opens with the full chronological timeline:
   - **Time** — Pacific Time, `MM-DD HH:MM:SS PT`
   - **Thread / actor** — `scanner-INTC`, `exit_manager`, `reconciliation`
   - **Action** — colored badge per canonical keyword
   - **Message** + expandable JSON **details** for structured fields

Roll chains work from both sides: viewing trade 944 (closed-via-roll)
shows the old leg's close AND the roll_open row for trade 956 (new
leg, linked via `from_trade_id=944`).

### From SQL

```sql
-- Every audit row for one trade, oldest first
SELECT created_at,
       component                            AS actor,
       details->>'action'                   AS action,
       message,
       details
FROM   system_log
WHERE  (details->>'trade_id')::int      = 944
    OR (details->>'from_trade_id')::int = 944
    OR (details->>'to_trade_id')::int   = 944
ORDER BY created_at ASC;
```

### From the API

```
GET /api/trades/{id}/audit
```

Returns the same chronologically-ordered list. Used by the UI modal;
also handy from a shell with `curl | jq` when you're debugging over ssh.

```bash
curl -s localhost/api/trades/944/audit \
  | jq '.entries[] | "\(.created_at)  \(.component)  \(.details.action)  \(.message)"'
```

### From bot.log

Every audit row is ALSO a Python log line. Grep works:

```bash
grep "AUDIT " bot.log | grep "IWM"          # everything audit-tagged for IWM
grep "AUDIT close:" bot.log                 # all closes
grep "AUDIT verify_close_fail" bot.log      # close retries — investigate these
```

---

## 5. What you'll see on a typical trade

Chronological audit trail for a healthy ICT long that hit take-profit:

```
04-20 07:15:02 PT  scanner-INTC      open              opened INTC260425C00060000 @ $1.45 signal=LONG_OB
                                                        details: {ticker: INTC, direction: LONG, contracts: 2,
                                                                  ib_perm_id: 77..., ib_tp_order_id: 3094,
                                                                  ib_sl_order_id: 3095, signal_entry: 59.87}
04-20 07:28:31 PT  exit_manager      verify_close_ok   IB position flattened + stragglers swept
04-20 07:28:31 PT  exit_manager      close:TP          closed INTC260425C00060000 @ $2.90 → WIN (TP)
                                                        details: {exit_price: 2.90, result: WIN, reason: TP,
                                                                  pnl_pct: 100.0, rolled: false}
```

Chronological trail for a trade that rolled:

```
04-20 06:42:40 PT  scanner-IWM       open              opened IWM260420C00275000 @ $0.98 signal=LONG_OB
04-20 06:43:41 PT  exit_manager      verify_close_ok   IB position flattened + stragglers swept
04-20 06:43:41 PT  exit_manager      close:ROLL        closed IWM260420C00275000 @ $1.18 → WIN (ROLL)
                                                        details: {rolled: true, pnl_pct: 20.4}
04-20 06:43:51 PT  exit_manager      roll_open         rolled from IWM260420C00275000 → IWM260420C00276000 @ $0.82
                                                        details: {from_trade_id: 944, to_symbol: IWM260420C00276000}
```

Chronological trail for a trade reconciliation had to fix:

```
04-20 06:42:40 PT  scanner-TSLA      open              opened TSLA260420P00397500 @ $4.49 signal=SHORT_OB
04-20 06:45:06 PT  exit_manager      verify_close_fail position did not flatten after 3s — releasing lock, will retry
                                                        details: {reason: ROLL, ib_con_id: 871662141}  [level=error]
04-20 06:46:19 PT  reconciliation    reconcile_adopt   IB orphan → adopted TSLA..P00397500 2x @ $4.49 SHORT
                                                        details: {ib_con_id: 871662141}              [level=warn]
```

That last example is exactly the TSLA incident from 2026-04-20.
Before the audit trail existed, you had to stitch that story together
from bot.log. Now it's three rows, ordered, with structured context.

---

## 6. Writing audit rows from new code

**If your code mutates a trade's state, you must call
`log_trade_action()`.** That's the rule — otherwise you've introduced
a "mystery action."

```python
from strategy.audit import log_trade_action

log_trade_action(
    trade_id=trade["db_id"],
    action="close:MANUAL",
    actor="dashboard",                     # or the thread name
    message=f"closed {trade['symbol']} via dashboard at ${price:.2f}",
    level="info",                          # info / warn / error
    extra={
        "ticker": trade["ticker"],
        "symbol": trade["symbol"],
        "exit_price": price,
        "triggered_by": "user_click",
    },
)
```

Rules:
- Pick an action from the canonical list in §2 or extend it (and update
  this doc).
- `actor` should be the component or thread name — matches the
  `system_log.component` column and the Threads page row names.
- `extra` is merged into `details` alongside the canonical keys.
  Prefer flat keys; don't nest dicts more than one level deep.
- The helper is silent on failure. It will not raise into your trade
  flow.

### What NOT to use audit for

- Periodic heartbeats / status updates → use `update_thread_status`
  instead (Threads page)
- Per-bar indicator logs / signal logs → use `log.info` only
  (bot.log, not `system_log`)
- Errors → use `strategy.error_handler.handle_error` which already
  writes to `system_log` with the right shape

Audit is specifically for **trade state transitions**.

---

## 7. Operational playbook

### "What happened to trade X?"

1. Trades tab → find the trade → click **Audit**. Done.

### "Why did reconciliation adopt this orphan?"

Look for the `reconcile_adopt` row. Its `details` has `ib_con_id`.
Then search audit for that con_id or the symbol on the other trades
around the same time — you'll usually find a `verify_close_fail` from
the exit_manager a minute earlier that explains why the DB row wasn't
closed properly.

### "Did a bracket ever get cancelled for this trade?"

SQL:
```sql
SELECT created_at, message, details
FROM   system_log
WHERE  (details->>'trade_id')::int = <id>
  AND  details->>'action' = 'cancel_bracket'
ORDER BY created_at;
```

### "Did any trade fail verification recently?"

```sql
SELECT created_at, component, message, details
FROM   system_log
WHERE  details->>'action' = 'verify_close_fail'
  AND  created_at > now() - interval '1 day'
ORDER BY created_at DESC;
```

Each one represents a close that was retried — not necessarily a bug,
but worth investigating to see whether the retry succeeded.

### "Show me all roll chains from today"

```sql
WITH rolls AS (
  SELECT  (details->>'from_trade_id')::int AS from_id,
          (details->>'trade_id')::int       AS to_id,
          created_at
  FROM    system_log
  WHERE   details->>'action' = 'roll_open'
    AND   created_at::date = current_date
)
SELECT from_id, to_id, created_at FROM rolls
ORDER BY created_at;
```

---

## 8. Invariants

The design guarantees these properties — if you see a violation,
it's a bug worth fixing:

1. **Every trade in `trades` has at least one `AUDIT open` row.**
   Corollary: if `status=closed`, there's also a matching `AUDIT close:*`
   row.
2. **Verification rows always precede close rows.**
   The sequence `verify_close_ok` → `close:*` is atomic from the
   reader's perspective (same `_atomic_close` invocation).
3. **`verify_close_fail` is never followed by `close:*` for the same
   `trade_id` in the same minute.** If it is, the verification is
   lying about its guarantees.
4. **Every `reconcile_adopt` has a matching IB position at the time of
   writing.** (Reconcile is the source, not a guess.)
5. **Roll chains form a DAG.** `roll_open` rows with `from_trade_id=X`
   imply a `close:ROLL` row on X. No two `roll_open` rows should share
   a `from_trade_id`.

---

## 9. Limitations & future work

- **No per-order audit.** We log the trade-level transitions; we don't
  log every intermediate IB order event (submit/cancel/modify). Those
  live in `bot.log` only. A future `order_audit` table could capture
  them if we need finer-grained investigation.
- **Bracket-cancel action is not yet called everywhere it should be.**
  `cancel_all_orders_and_verify` logs to bot.log but doesn't currently
  call `log_trade_action("cancel_bracket", ...)`. TODO: add that so
  the Audit modal explicitly shows each cancel.
- **No retention policy.** `system_log` grows forever. At 10k
  trades/year × ~10 audit rows each = 100k rows/year. Not a problem
  yet; design a TTL + archive when we hit 10M rows.
- **`from_trade_id`/`to_trade_id` only set for rolls.** If we later
  add split/merge semantics (e.g. partial close), define new link
  keys here.

---

## 10. File map

| File | Responsibility |
|------|----------------|
| `strategy/audit.py` | The `log_trade_action()` helper |
| `strategy/trade_entry_manager.py` | Writes `open` rows |
| `strategy/exit_manager.py` | Writes `close:*`, `verify_close_ok`, `verify_close_fail`, `roll_open` |
| `strategy/reconciliation.py` | Writes `reconcile_close`, `reconcile_adopt` |
| `dashboard/routes/trades.py` | `GET /api/trades/{id}/audit` endpoint |
| `dashboard/frontend/src/components/TradeTable.tsx` | Audit button + modal |
| `docs/logging_and_audit.md` | This doc |
| `tests/unit/test_audit.py` | Schema + level + silent-failure tests |

Related design docs:

- `docs/roll_close_bug_fixes.md` — the three bugs whose fallout
  (incomplete closes, orphaned positions) motivated the audit trail
- `docs/backtest_analytics_design.md` — backtest-side slice/dice; not
  connected to the live audit but shares the idea of filterable
  per-trade detail
