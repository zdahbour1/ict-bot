# Market-Hours Guards

## Motivation — the 2026-04-20 afternoon cascade

At ~13:19 PT (19 minutes after the options market closed at 13:00
PT) the exit manager was still trying to close positions. Each
attempt:

1. Issued a fresh MKT SELL
2. `_verify_close_on_ib` polled for flatten → position didn't change
   (market closed, order sat `PreSubmitted`)
3. `AUDIT verify_close_fail` → release lock → next monitor cycle
4. Next cycle found the previous unfilled SELL, cancelled it, sent
   another one → GOTO 2

Over ~20 minutes 30+ MKT SELL orders piled up on IB across 11
tickers. None could fill. The only way to stop it was to kill the
bot process.

## The fix — three gates keyed off one clock

```
settings:
  EOD_HARD_CUTOFF_HOUR_PT   = 13   # US equity options close (1 PM PT / 4 PM ET)
  EOD_HARD_CUTOFF_MINUTE_PT = 0
  EOD_CLOSE_LEAD_MINUTES    = 5    # sweep starts 5 min before cutoff
```

Gives us three distinct windows on any trading day:

```
 ─── Morning ───┐            ┌── EOD sweep ──┐┌── Hands off ──
 TRADE_WINDOW_   │   normal    │  force-close  ││  do NOTHING
 START (06:30)   │  operation  │  all open     ││  (all exits +
                 │             │  trades with  ││   entries
                 │             │  reason=EOD   ││   blocked)
                 │             │               ││
   (no entries   │             │               ││
    pre-market)  │   entries   │   entries     ││
                 │   allowed   │   blocked     ││
                 │             │               ││
    06:30────────┴────────12:55┴────────13:00──┴──────→ time
                                       │
                                 hard cutoff
```

### Gate 1 — hard cutoff (`_check_exits`)

```python
clock = get_market_clock()
if clock.is_past_close():
    return   # no exit attempts; market won't fill anything
```

First thing in `_check_exits`. Zero orders sent after 13:00 PT.

### Gate 2 — EOD sweep (`_check_exits`)

```python
if clock.in_eod_sweep_window():
    self._run_eod_sweep(trades)
    return   # skip normal TP/SL/ROLL logic during sweep
```

`_run_eod_sweep` iterates every open trade and calls `_atomic_close`
with `reason='EOD'`. Uses the same atomic-lock + bracket-cancel +
verify machinery as TP/SL exits, so all the safety guarantees
apply. Each trade is stamped with `_eod_closed_this_session` so
the sweep is idempotent across the 5 polling cycles inside the
window.

The 5-minute lead ensures MKT SELLs fill while the market still
accepts orders — they can't sit `PreSubmitted` if they've already
filled.

### Gate 3 — entry cutoff (`TradeEntryManager.can_enter`)

```python
if not clock.entries_allowed():
    return False, "EOD sweep window" | "market closed" | "before window"
```

First check in `can_enter`, before any of the "already in trade"
or "daily limit" logic. The scanner can fire signals all it wants
— the entry manager refuses them.

Combined with Gate 2, this prevents: *"a new bracket opens at
12:58 PT, gets torn down 90 seconds later by the EOD sweep."*

## Configuration via settings table

All three values are in the `settings` table with `strategy_id IS
NULL` (global, not per-strategy). Change them with plain SQL —
no code changes needed:

```sql
-- Close 10 min before market close instead of 5
UPDATE settings SET value='10'
 WHERE key='EOD_CLOSE_LEAD_MINUTES' AND strategy_id IS NULL;

-- Accommodate a non-standard market close (holidays, DST edge cases)
UPDATE settings SET value='12'
 WHERE key='EOD_HARD_CUTOFF_HOUR_PT' AND strategy_id IS NULL;
```

The values are re-read on every monitor cycle (cheap — it's one
indexed SELECT), so changes take effect within seconds. No bot
restart required.

## The MarketClock abstraction

```python
from strategy.market_hours import get_market_clock

clock = get_market_clock()    # snapshot now

clock.is_past_close()         # bool
clock.in_eod_sweep_window()   # bool
clock.entries_allowed()       # bool — combines start-window + EOD
clock.minutes_until_close()   # float
clock.minutes_until_eod_sweep()
```

Pure functions of the snapshot. Tests can build a clock at an
arbitrary time:

```python
from strategy.market_hours import get_market_clock
from datetime import datetime
import pytz
PT = pytz.timezone("America/Los_Angeles")
clock = get_market_clock(now=PT.localize(datetime(2026, 4, 21, 12, 58)))
assert clock.in_eod_sweep_window()
assert not clock.entries_allowed()
```

## What this does NOT do (limitations)

- **Weekends / holidays**: the clock doesn't know Mon-Fri vs
  weekends. Before market open, `entries_allowed()` is False (good);
  after close, `is_past_close()` is True (good). But on a holiday
  the EOD sweep would still run at 12:55 PT, closing whatever is
  open. Currently that's harmless because positions shouldn't exist
  overnight, but future work could add a holiday calendar.
- **Early closes** (day after Thanksgiving, Christmas Eve): these
  close at 10:00 PT. Today our hard cutoff is fixed at 13:00 PT.
  For now, lower the setting manually the morning of the early
  close. A proper fix would read the exchange calendar.
- **Non-equity contracts**: futures options trade nearly 24h. The
  current guard assumes US equity options hours. When we ship FOP
  (see `docs/fop_live_trading_design.md`) this guard needs a
  per-ticker override.

## Testing

19 unit tests in `tests/unit/test_market_hours.py` cover every
boundary (12:54 vs 12:55, exactly 13:00, after close, pre-market,
etc.) plus integration with `TradeEntryManager.can_enter`. Run:

```bash
python -m pytest tests/unit/test_market_hours.py -v
```

## Related docs

- `docs/bracket_rollback_semantics.md` — the strict-cancel abort
  behavior that got retriggered repeatedly in the afternoon
  cascade this guard eliminates at the source.
- `docs/logging_and_audit.md` — `close:EOD` is a new canonical
  audit action, to be added to §2's vocabulary.
