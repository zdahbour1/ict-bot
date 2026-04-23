# Tomorrow's test plan — 2026-04-24

Shipped the evening of 2026-04-23. Every item below has unit coverage
(`python -m pytest tests/unit/`, 490+ passed). Tomorrow's job is to
validate them against live IB paper trading.

## Pre-market checklist (before 06:30 PT)

1. **TWS up, API enabled** (config port 7497 paper).
2. `curl -X POST http://localhost:9000/start` — bot alive.
3. Dashboard → **Settings** tab → confirm these are populated:
   - `DN_DELTA_HEDGE_ENABLED` — start **false**, flip **true** mid-session
   - `DN_EVENT_DRIVEN_HEDGE` — **true**
   - `DN_EVENT_TRIGGER_BPS` — 30 (0.30% triggers early rebalance)
   - `DN_DELTA_BAND_SHARES` — 20
   - `DN_COMBO_AUTO_LIMIT` — **true** (IB slippage fix)
   - `DN_COMBO_LIMIT_SLIP_BPS` — 200 (2% slippage buffer)
   - `MAX_CONCURRENT_PER_UNDERLYING` — 2 (ENH-037)
   - `RECONCILE_AUTO_CANCEL_DUP_BRACKETS` — **true** (ENH-044)
   - `AUTO_RESTART_ON_STRATEGY_TOGGLE` — **true** (ENH-033)
4. Dashboard → **Threads** tab → **strategy filter** dropdown visible, pick `delta_neutral` to isolate (ENH-043).
5. Dashboard → **Trades** tab → **per-strategy P&L cards** render (ENH-040). Toggle **Compact / Normal** density button (ENH-010).

---

## Tests to run during market hours

### 1. IB slippage fix (combo LimitOrder)

**Setup:** let DN scanner fire first iron condor naturally.

**Expected:**
- TWS Activity tab shows **one BAG order**, `LMT` type (not MKT), net price ≈ (short_call + short_put) − (long_call + long_put) − 2% buffer.
- `system_log component='delta-hedger'` line: `COMBO-LIMIT net_premium=$X.XX action=SELL slip=200bps → limit=$Y.YY`.

**Fail modes to watch for:**
- Any leg quote missing → falls back to MKT (still fills but with slippage). Check `[COMBO-LIMIT] quote failed` warnings.

### 2. ENH-043 Threads strategy filter

- Open Threads tab, pick each strategy from dropdown; confirm scanner count drops to just that strategy's tickers.
- Pick `delta_neutral` → should show `delta-hedger` + the DN ticker scanners only.

### 3. ENH-044 PASS 5 auto-cleanup

- Let bot run 15 min with live DN entries.
- Open TWS Activity: should see **zero** duplicate or unreferenced SELL option orders. Every working bracket should map to exactly one open DB trade.
- `system_log` every ~2 min: `Reconciliation: …` line. On a cycle where dups existed, audit log gets `pass5_dup_bracket_cleanup` entry.

### 4. ENH-040 + ENH-039 per-strategy P&L cards

- After first few trades: cards at top of Trades tab should show P&L, win rate, trade count per strategy.
- Click a card → filters table to that strategy (re-click to clear).
- `total_commission` column pulls from `trade_legs.commission` (zeros until ENH-050 Stage D backfill runs).

### 5. ENH-033 auto-restart on strategy toggle

- Dashboard → Strategies tab → disable `orb` → bot should stop and restart within ~5 s.
- Dashboard "Bot status" banner updates to green.
- `curl http://localhost:9000/status` shows a fresh `pid` and `started_at`.
- Re-enable `orb` → same bounce.

### 6. ENH-037 cross-strategy exposure cap

- Manually enter ICT trades on SPY until 2 open. Next signal on SPY from any strategy logs:
  `entry-manager … BLOCKED: SPY — cross-strategy cap hit (2/2 open on SPY)`.
- Raise `MAX_CONCURRENT_PER_UNDERLYING` to 3 in Settings → within 5 s new signal goes through.

### 7. ENH-049 Stage 3 event-driven delta hedge

- Flip `DN_DELTA_HEDGE_ENABLED` to **true**.
- Pick a volatile DN ticker (COIN, MSTR, TSLA).
- When the underlying jerks 0.3%+ in a minute, expect `system_log`:
  `Event-driven trigger: TICKER moved Xbps (threshold 30bps)`.
- A rebalance fires within 1 second of the trigger, NOT waiting the full 30s.

### 8. ENH-048 MNQ / MES futures scanners

- Confirm MNQ/MES scanner threads show activity in the Threads tab log viewer.
- `system_log` from their scanner should show bars being fetched (no more
  `Error 200: No security definition`).
- Any ICT signal on MNQ/MES can be ignored for now — goal today is just
  "data flows through the scanner."

### 9. ENH-035 production IV detection

- Watch a DN PREFLIGHT log line. `iv_source=bs_implied` means the ATM
  option chain quote worked; `iv_source=proxy` means fallback was used.
- If bs_implied is reliably hit, raise `DELTA_NEUTRAL_IV_THRESHOLD` back
  toward 0.25–0.35 so only genuinely high-IV names produce entries.

### 10. ENH-045 ref-less backfill (one-shot, optional)

- `DATABASE_URL=postgresql://ict_bot:ict_bot_dev@localhost:5432/ict_bot python scripts/backfill_refless_orders.py` (dry-run).
- If it finds candidates, run with `--apply`. Verify in TWS Activity that
  the previously blank Order Ref columns now show `strategy-TICKER-date-NN`.

### 11. ENH-047 leg drill-down (data quality)

- Expand a new (post-this-release) multi-leg trade in Trades tab.
- Per-leg rows show strike, right, expiry, current price, per-leg P&L.
- Legs 1-3 `current_price` updates every monitor cycle, not stuck at
  entry (ENH from yesterday's fix).

### 12. ENH-001 MVP quote latency

- Informational: `[IB] symbol: bid=X.XX ask=Y.YY mid=Z.ZZ` lines in bot.log
  should appear within ~200 ms of signal instead of the old 1.5 s.

---

## After market-close backtest validation

Once market closes:

```bash
python run_backtest_engine.py --strategy delta_neutral \
  --tickers SPY,QQQ,IWM,COIN,TSLA --days 10 \
  --run-name "DN-post-hedger-$(date +%Y%m%d)"
```

- Confirm the run writes 4 legs per trade in `backtest_trade_legs`.
- Compare total_pnl vs commission in the `backtest_runs` summary.
- If the hedger Stage 3 event-trigger fires during a 2% intraday move,
  backtest should show rebalance events in audit output.

## Known deferrals (not in scope today)

- ENH-050 Stage A/B/C — combo per-leg fill-price fallback (design doc
  in `docs/enh_050_combo_leg_fill_price.md`). Ship Monday morning.
- ENH-024 — plugin framework. Auditor agent confirmed substantially
  complete; no active phases to finish.
- ENH-018 auth / ENH-020 cloud-deploy / ENH-025 iOS / ENH-022 profiling
  — Q2 work.

## Rollback switches

If any change misbehaves live, flip the corresponding setting:

| Feature | Off switch |
|---|---|
| IB slippage fix (combo LMT) | `DN_COMBO_AUTO_LIMIT=false` |
| PASS 5 auto-cancel | `RECONCILE_AUTO_CANCEL_DUP_BRACKETS=false` |
| Auto-restart on toggle | `AUTO_RESTART_ON_STRATEGY_TOGGLE=false` |
| Event-driven hedge | `DN_EVENT_DRIVEN_HEDGE=false` |
| Cross-strategy cap | `MAX_CONCURRENT_PER_UNDERLYING=0` |
| Delta hedge loop (entire) | `DN_DELTA_HEDGE_ENABLED=false` |
| Combo orders (entire) | `USE_COMBO_ORDERS_FOR_MULTI_LEG=false` |

All settings live in the `settings` table and hot-reload within 5 s.
