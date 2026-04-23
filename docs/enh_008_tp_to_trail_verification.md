# ENH-008 — TP-to-Trail live verification

**Status:** Code exists; needs live-fire confirmation
**Owner:** runs during tomorrow's market session

## What it does

When a trade hits its TP level, instead of closing immediately it
**moves the stop-loss up to the TP level** and lets the position
continue running. The new SL becomes the floor — the trade can either
extend further (more profit) or retrace to the former-TP (now-SL)
level and close at break-even-on-gains.

## Where it lives

`strategy/exit_conditions.py::check_tp_to_trail` — evaluated inside
`evaluate_exit` on every monitor cycle. Gated by `config.TP_TO_TRAIL`
(default `true`) and triggered when `current_pnl_pct >= PROFIT_TARGET`
for the first time on a trade.

## Observable signals on the dashboard

1. Trade's `peak_pnl_pct` column crosses the TP threshold.
2. `dynamic_sl_pct` column jumps from its original negative value
   (e.g. -0.60) to a positive value equal to the trade's peak.
3. `ib_sl_price` updates — the IB bracket's stop price is modified
   to match the new dynamic SL.
4. Bot log line: `[TICKER] Bracket SL → $X.XX` where X.XX is the new
   (higher) stop price.

## Verification checklist for tomorrow

1. Force a winner: pick a ticker with a likely-in-the-money setup
   (small TP multiple) or lower `PROFIT_TARGET` to 0.5 in settings so
   a common move triggers TP.
2. Watch the Trades tab: when the trade crosses TP, check that
   `dynamic_sl_pct` flips from negative → positive **without** the
   trade closing.
3. Confirm TWS shows the bracket SL order `auxPrice` has been bumped
   to match.
4. Log search: `SELECT message FROM system_log WHERE component ILIKE 'exit_manager' AND message ILIKE '%Bracket SL%' AND created_at > NOW() - INTERVAL '1 hour';`

## If it doesn't fire

Fallback diagnostics:

- `SELECT value FROM settings WHERE key='TP_TO_TRAIL';` — must be `true`
- Check `check_tp_to_trail` is called in the monitor loop:
  `strategy/exit_manager.py` — confirm `update_trailing_stop(trade, pnl_pct)`
  is invoked every cycle.
- Inspect a specific trade's `peak_pnl_pct` — if it never crossed
  `PROFIT_TARGET`, the feature had no trigger, not a bug.

## Known gap

The TP-to-trail SL modification uses `update_bracket_sl` which
re-submits the IB order. If the bracket has been cancelled in the
meantime (e.g. by OCA from sister fill), the modify silently fails.
`system_log` will contain `Failed to update bracket SL: ...`. This
is rare but worth watching; there's no scheduled fix unless it
becomes frequent.
