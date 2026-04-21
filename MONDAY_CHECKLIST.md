# Monday Live-Trading Validation — feature/arch-003-ib-client-split

You're flipping to this branch to confirm the `ib_client.py` mixin refactor
is behaviorally identical to the monolith during real market hours. The
pre-market static verification has already been done (see
`tests/unit/test_ib_client_api_parity.py` — 13 checks passing) — this
checklist is what to watch during the session.

## Before the bell (ideally 15 min before market open)

1. **Checkout the branch:**
   ```
   git checkout feature/arch-003-ib-client-split
   git pull
   ```
2. **Rebuild any containers that include broker code.** The API container
   doesn't — only the bot process does. Restart the bot:
   - Stop via dashboard OR `curl -X POST http://localhost:9000/stop`
   - Start via dashboard OR `curl -X POST http://localhost:9000/start`
3. **Run the regression suite one more time** (click Run All in the
   Tests tab or `PYTEST_DB_REPORT=1 pytest tests/`). Expected:
   all green except the `integration` tests if you're not running them.
4. **Verify bot.log shows:**
   - `[IB] Connecting to Interactive Brokers…`
   - `Connected to IB — accounts: […]`
   - `Pool connected: 4 connections`
   No import errors. No tracebacks on startup.

## What to watch during the first scan cycle

The split changed *how* methods are organized, not *what* they do. So the
smoke test is: does every IB round-trip we used to make still happen?

1. **Contract qualification** — every scanner's first tick should log:
   ```
   [IB] Qualifying contract for QQQ...
   ```
   If you see a `NoneType has no attribute 'conId'` it means
   `_occ_to_contract` is broken in the split.

2. **Price fetches** — in the dashboard, open trades' `current_price`
   column should update within 5–10 seconds of the bot coming up. If
   they stay at 0 or stale forever, `get_option_prices_batch` isn't
   being dispatched correctly by the IB pool.

3. **First entry** — when a signal fires and the bot opens a trade,
   the log should show:
   ```
   [IB] BRACKET BUY: 2x QQQ... — parent=... permId=... conId=...
        status=Filled fill=$X.XX TP=... SL=...
   ```
   If `permId` or `conId` is None, `_ib_place_bracket` return shape
   changed. This would show up immediately in the new `test_signatures_match`
   test, but watch anyway.

4. **First exit** — when TP/SL fires or you manually close, the close
   flow is the critical path. Watch for:
   ```
   DB: locked trade N for close
   [IB] Cancel sent for orderId=…
   [IB] … → Cancelled
   [IB] Verified position qty=0
   DB: close committed
   ```
   If you see `could not obtain lock` repeatedly on the SAME trade
   without the close ever completing, the mixin split has a retry bug.

## Specific regressions to rule out

| # | What to watch for | Why it'd point to the split |
|---|---|---|
| 1 | `AttributeError: 'IBClient' object has no attribute '_ib_...'` | A private helper wasn't copied to its mixin file |
| 2 | Silent double-fills or phantom fills | `_submit_to_ib` dispatch got confused about which queue (legacy mode) |
| 3 | `Flex option detected` on a non-flex symbol | `_check_not_flex` is now in `ib_orders.py` — confirm it's still re-exported from `broker.ib_client` (test covers this) |
| 4 | VIX fetch returns None when it shouldn't | `ib_market_data.py` import of `Index` happens inside `_ib_get_vix` — confirm no circular |
| 5 | Reconciliation finds 0 positions when IB has positions | `get_ib_positions_raw` timeout or return-shape mismatch |
| 6 | Scanner threads hang on second iteration | IB pool `submit()` blocking in a mixin method that used to hold the lock differently |

## Pass criteria

Flip back to `feature/dashboard` if:
- Any first-opened trade fails to write bracket order IDs to DB
- Any close takes >10 seconds on the lock step without NOWAIT skipping
- `bot.log` has more than 3 IB-related tracebacks in the first 30 min
- Dashboard `/trades` stops updating mid-session

Keep on the split branch if:
- First 5 trades of the day (entries + exits) complete the full flow
  with the same log lines as pre-split
- No net-new error patterns in `bot.log`
- `/api/health` stays green
- The Tests tab still shows all suites passing (run once mid-session)

## After the session (Monday evening)

Whatever happens, capture a snapshot:

```
# Quick before/after comparison
docker exec ict-bot-postgres-1 psql -U ict_bot -d ict_bot -c \
  "SELECT COUNT(*), MIN(entry_time), MAX(exit_time) FROM trades \
   WHERE DATE(entry_time) = CURRENT_DATE;"
```

If the day looked clean, merge `feature/arch-003-ib-client-split` →
`feature/dashboard`. If anything's off, open a bug in `docs/backlog.md`
with the log lines and revert to the monolith version.
