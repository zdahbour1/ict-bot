# Roll-Mechanics Live Test Runbook
**Purpose.** Force at least one natural ICT roll to fire today so we
validate Fix A (cross-client bracket visibility), Fix B (SHORT
direction check), and Fix C (verify close + straggler sweep) on a
real rolled trade.  Section D of `docs/market_hours_validation.md` is
the merge gate this covers.

**Duration.** 1–3 hours of market time (depending on how fast a
trade reaches the lowered threshold).

**Prerequisites.**
- Market is open (we're at ~9:15 PT; plenty of time today).
- TWS is running and logged in.
- Bot may be running or stopped when we start — either is fine.
- `bot_manager.py` sidecar is up (checked with `curl http://localhost:9000/health`).

---

## Step 0 — Pre-flight check (2 min)

```bash
# Bot state
curl -s http://localhost:9000/status | python -m json.tool

# Current ICT config
docker exec ict-bot-postgres-1 psql -U ict_bot -d ict_bot -c "
SELECT key, value FROM settings
 WHERE strategy_id = 1 AND key IN ('ROLL_ENABLED','ROLL_THRESHOLD','PROFIT_TARGET','STOP_LOSS');"

# Open trades (so we know what might roll)
docker exec ict-bot-postgres-1 psql -U ict_bot -d ict_bot -c "
SELECT id, ticker, symbol, direction, contracts_open,
       entry_price, current_price, pnl_pct, peak_pnl_pct, updated_at
  FROM trades WHERE status='open'
  ORDER BY peak_pnl_pct DESC;"
```

Record the **current ROLL_THRESHOLD** value (should be `0.80`). You'll
revert to this at the end.

**Red flags — stop and fix before continuing:**
- Any open trade has `updated_at` stuck at `created_at` (= monitoring
  gap; re-run the symbol-normalization SQL from commit `088e494`).
- `ROLL_ENABLED` is not `true`.

## Step 1 — Lower ROLL_THRESHOLD to 0.15 (1 min)

```bash
docker exec ict-bot-postgres-1 psql -U ict_bot -d ict_bot -c "
UPDATE settings
   SET value = '0.15', updated_at = now()
 WHERE strategy_id = 1 AND key = 'ROLL_THRESHOLD'
RETURNING strategy_id, key, value, updated_at;"
```

Why 0.15:
- Low enough that any winning trade above +15% will roll
- High enough to exclude losing trades (avoids false positives)
- ICT's default profit_target is +100% — plenty of headroom above 15%
  for a trade to keep winning after the first roll

The setting is read live by `exit_conditions.check_roll_trigger` on
every monitor cycle, so **no restart is required** — but if the bot
is currently STOPPED, we'll start it fresh in Step 2 anyway.

## Step 2 — Start bot (if stopped) + verify (2 min)

```bash
# If bot is stopped:
curl -X POST http://localhost:9000/start
sleep 5
curl -s http://localhost:9000/status | python -m json.tool

# Verify setting is live
tail -f bot.log | grep -iE "roll|RECONCILE|MONITOR" &
# Ctrl-C out of tail after you see a few MONITOR lines
```

Watch for:
- `[RECONCILE] Done: ...` from startup reconcile
- `[TICKER] MONITOR db_id=... | P&L:+X.X% | Peak:+X.X% |` lines for
  every open trade
- `entry-manager` row on Threads page showing `idle` or activity

## Step 3 — Wait for a roll (~30 min to a few hours)

A roll fires when a monitored trade's **peak P&L** exceeds
`ROLL_THRESHOLD` (0.15 now) AND price pulls back from that peak by
the configured retrace. For ICT during market hours, on lowered
threshold, expect the first roll within 30-60 min if any ticker has
a favorable move.

### Live watch commands (run each in its own terminal / Bash session)

**Terminal A — live roll + close events in bot.log:**
```bash
tail -F bot.log | grep -aE "SIGNAL→ORDER|PLACING ORDER|ROLL START|ROLL COMPLETE|ROLL ABORT|VERIFY CLOSE|ATOMIC CLOSE|BRACKET BUY|STEP 0: reqAllOpenOrders"
```

**Terminal B — audit trail for any closed/rolled trade in last hour:**
```bash
# Find the most recent closed trade
docker exec ict-bot-postgres-1 psql -U ict_bot -d ict_bot -tAc "
SELECT id FROM trades
 WHERE status='closed' AND exit_reason='ROLL'
   AND updated_at > now() - interval '1 hour'
 ORDER BY updated_at DESC LIMIT 1;"
# Then pull its audit trail:
curl -s http://localhost/api/trades/<id>/audit | python -m json.tool
```

**Terminal C — watch DB state diff (every 30s):**
```bash
while true; do
  clear; date '+%H:%M:%S PT'
  docker exec ict-bot-postgres-1 psql -U ict_bot -d ict_bot -c "
    SELECT id, ticker, status, contracts_open,
           round(pnl_pct*100,1) AS pnl_pct,
           round(peak_pnl_pct*100,1) AS peak,
           exit_reason
      FROM trades WHERE updated_at > now() - interval '30 min'
      ORDER BY updated_at DESC LIMIT 15;"
  sleep 30
done
```

## Step 4 — Verify the roll flow (when you see one)

The moment a roll fires, the audit trail for the OLD leg must contain,
in this order:

```
AUDIT open              (original entry — hours earlier)
AUDIT verify_close_ok   (post-close IB verification passed)
AUDIT close:ROLL        (DB marks old leg closed with reason ROLL)
```

And the NEW leg's audit trail must contain:

```
AUDIT open              (new leg opened with its own bracket)
AUDIT roll_open         (wrapper row linking from_trade_id → old leg)
```

### Checklist — all must be TRUE to pass the gate

- [ ] `bot.log` shows `STEP 0: reqAllOpenOrders → N trades visible
      (cross-client merge)` during the close  *[Fix A]*
- [ ] `bot.log` shows `CLOSE STEP 5 RESULT: Sell order sent` without
      any preceding `EXECUTE EXIT ABORTED` message  *[Fix B]*
- [ ] `bot.log` shows `VERIFY CLOSE: position=0 after <1s`
      *[Fix C happy path]* — OR `VERIFY CLOSE: N straggler orders
      found after close — cancelling defensively: [...]`
      *[Fix C recovery path, even better]*
- [ ] Audit trail shows `verify_close_ok` → `close:ROLL` for old leg
- [ ] Audit trail shows `roll_open` for new leg with `from_trade_id`
      pointing at old leg
- [ ] No `reconcile_adopt` rows appear for this symbol in the next
      60 seconds after the roll

### Red flags — stop immediately

```
VERIFY CLOSE FAILED: position still N after 3s — releasing lock
```
→ Fix C is protecting us but the underlying close failed. Grab the
audit, inspect IB, may need to close manually in TWS.

```
EXECUTE EXIT ABORTED — NEGATIVE position on IB (qty=-N)
```
→ We're already net-short somewhere. Same bug category as IWM yesterday.
Stop bot with `curl -X POST http://localhost:9000/stop`, diagnose.

```
reconcile_adopt row appears ~60s after the close
```
→ Roll half-completed: we closed in DB but IB still has the position
(or created a new one we don't know about). Stop and diagnose.

## Step 5 — Revert before leaving unattended (1 min)

**This is non-optional.** 0.15 is a testing value; leaving it at 0.15
will cause the bot to roll hyperactively after-hours.

```bash
docker exec ict-bot-postgres-1 psql -U ict_bot -d ict_bot -c "
UPDATE settings
   SET value = '0.80', updated_at = now()
 WHERE strategy_id = 1 AND key = 'ROLL_THRESHOLD'
RETURNING strategy_id, key, value, updated_at;"

# Verify
docker exec ict-bot-postgres-1 psql -U ict_bot -d ict_bot -c "
SELECT key, value FROM settings WHERE strategy_id=1 AND key='ROLL_THRESHOLD';"
```

You do NOT need to restart the bot — the setting is read live.

## Step 6 — Declare success + proceed to merge

If the checklist in Step 4 is fully ticked for at least one rolled
trade:

1. Note the trade IDs (old + new leg) in whatever journal you keep.
2. Confirm no lingering open positions need attention.
3. Green-light the merge. I'll run:
   - Full regression suite (`python -m pytest tests/ -q`)
   - Final containers smoke test
   - Fast-forward merge `feature/profitability-research` → `feature/dashboard`
   - Push + delete `feature/enh-019-backtest`
   - Update `RESTART_PROMPT.md`

---

## FAQ

**Q. What if no trade hits +15% today?**
Try lowering to `0.10` after an hour. If still nothing, the market
is just that flat — try again tomorrow. Do NOT go below `0.05` (rolls
at that level start chewing into normal noise).

**Q. What if I see the IWM-style bug actually happen?**
It shouldn't — Fix C's straggler sweep is designed to prevent it.
But if you see a `reconcile_adopt` for a NEGATIVE position after a
close, stop the bot, close manually in TWS, and we root-cause.

**Q. Can I do this with ORB or VWAP active instead?**
Yes, change strategy_id=1 to the target strategy's ID. But do one
thing at a time — if ORB is active and a weird behavior appears,
we won't know if it was the roll fix or the strategy switch.

**Q. Can the roll fire from manual "Close" button in the UI?**
No. Manual close → `AUDIT close:MANUAL`, not `close:ROLL`. To exercise
the roll path specifically, the bot must decide to roll on its own.
