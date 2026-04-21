# Ticker Thread Lifecycle — Opens, Monitors, Closes, Rolls

**Scope.** This doc describes what each **per-ticker scanner thread** does,
which threads are involved in opening/closing a trade, and when rolls
happen. It's a map of the current (2026-04-20) architecture — not a
proposal. For the proposed refactor that would move close execution
onto the ticker thread, see `docs/thread_owned_close.md`.

---

## 1. The thread cast

The running bot has **six classes of threads** all operating on the
same `trades` database:

| Thread | Count | Lives on | Job |
|--------|------:|----------|-----|
| **scanner-`TICKER`** | N (one per ticker) | daemon Thread, ticker-specific IB client | detect signals, call into entry manager |
| **exit_manager** | 1 (shared) | daemon Thread, exit-mgr IB client | monitor all open trades, decide exits, execute closes |
| **reconciliation** | 0 (runs *inside* exit_manager) | — | called every ~60s from exit_manager's loop |
| **bot-main** | 1 | process main | heartbeat + stop/start coordination |
| **webhook** | 1 | Flask Thread | receive TradingView webhooks (unused in ICT path) |
| **orphan-detector** | 0 (runs *inside* reconciliation) | — | stateful detector invoked in PASS 3 of reconcile |

Each of the N scanner threads has its **own** IB client (from a pool of
~3 connections; tickers are deterministically hashed onto connections
via `pool.get_scanner_connection(ticker)`). The exit_manager has a
dedicated IB client (clientId = base_id).

This asymmetry — entries on one client, exits on another — is the
source of the cross-client cancel bug discussed in
`docs/bracket_cancel_strict_verification.md` and
`docs/thread_owned_close.md`.

---

## 2. Ticker-thread lifecycle

```
Scanner thread (created in main.py, started once per ticker)
┌─────────────────────────────────────────────────────────────────┐
│  __init__                                                       │
│    • ticker name + IB client + shared contract cache            │
│    • scan offset (stagger ticker starts across 60s)             │
│    • signal_engine, trade_entry_manager instances               │
│                                                                 │
│  start() ─────────────► daemon Thread runs _loop()              │
│                                                                 │
│  _loop():                                                        │
│    while not stopped:                                           │
│       _scan()   ◄────────── one scan cycle per 60s              │
│       sleep 60                                                   │
│                                                                 │
│  stop() ─ sets an Event; _loop exits after current scan         │
└─────────────────────────────────────────────────────────────────┘
```

Each scan cycle is idempotent: if a signal fires and no trade opens
(entry blocked), next cycle tries again with the same signal
suppression logic.

---

## 3. What a scan does — step by step

Inside `Scanner._scan()`:

### 3.1 Housekeeping (runs every scan)

```
trade_manager.check_pending_state()   — clear _entry_pending flag if stuck
detect_trade_closures()               — if a trade closed since last scan,
                                        set cooldown, clear signal setups
```

### 3.2 Window check

```
now_pt = datetime.now(PT)
in_market, in_trade_window, is_weekend = _check_windows()
```

The thread hard-short-circuits based on these:

| State | Scanner behavior |
|-------|------------------|
| weekend | skip scan, log once |
| pre-market (before 06:30 PT) | skip |
| in_market + in_trade_window | full scan with entries allowed |
| market open but past TRADE_WINDOW_END (13:00 PT) | **analyze only** — signals detected + emailed but no entries |
| after market close | skip |

(As of commit `fad09c4` the entry manager also enforces an EOD lead
window — entries blocked 5 min before 13:00 PT. The scanner itself
doesn't need that guard; `can_enter()` returns False during the
window.)

### 3.3 Pull bars from IB

Three timeframes:
- **1-min bars** — for raid / iFVG / OB detection
- **1-hour bars** — for EMA bias
- **4-hour bars** — for trend filter

These come from `data/ib_provider.py` using the scanner's IB client —
real IB historical data, not yfinance.

### 3.4 Compute levels + EMA bias

```
levels = compute_levels(...)    # PDH, PDL, 1H, 4H, OR high/low
ema_bias = _get_ema_bias(bars_1h, now_pt)   # informational only
```

### 3.5 Detect signals (pure)

```
signals = self.signal_engine.detect(bars_1m, bars_1h, bars_4h, levels)
```

`SignalEngine.detect()` is side-effect-free — it just returns a list
of `Signal` dataclasses. Internal state (alerts sent today, setups
consumed, dedup cache) lives on the engine and persists across scans.

### 3.6 Process each signal

```python
for signal in signals:
    log_big_banner(signal)
    trade = None
    if in_trade_window:
        trade = self.trade_manager.enter(signal, bars_1m=bars_1m)
        if trade:
            self.signal_engine.mark_used(signal.setup_id)
    if trade and in_market:
        send_signal_email(...)
```

The **ticker thread is what calls `trade_manager.enter(signal)`**.
That call chains through to:

```
TradeEntryManager.enter(signal)
  → can_enter() gates        (DB: already in trade? cooldown? EOD? daily limit?)
  → _ib_preflight_check()    (IB: positions exist? open orders?)
  → _place_order_with_timeout(signal)
      → ThreadPoolExecutor(max_workers=1).submit(select_and_enter, client, ticker)
      → future.result(timeout=60)
  → if trade succeeds:
      → enrich_trade(trade)           (VIX, indicators, Greeks)
      → exit_manager.add_trade(trade) (writes to DB via insert_trade)
      → audit: 'open'
```

**The entry runs on the ticker thread** — but the actual IB order
placement happens on a short-lived worker thread spawned by the
ThreadPoolExecutor. That worker routes into the ticker's IB client
(clientId N+1 in the pool) and runs on that client's IB event loop
thread. So strictly: *signal detection and gating → ticker thread;
order placement → the ticker's IB client thread*.

After `enter()` returns, the ticker thread **has no further role in
the trade's life** (except via the DB source of truth that everyone
reads). It goes back to `sleep(60)` and scans again next minute.

### 3.7 Post-scan status update

```
update_thread_status("scanner-TICKER", ...)   # Threads-page row
```

---

## 4. What happens AFTER the trade is opened

Once `add_trade()` writes the row, ownership passes to the
**exit_manager thread**. The exit manager has a single `_monitor_loop`
polling every ~7 seconds.

```
exit_manager._monitor_loop():
  while not stopped:
    _check_exits()               — single pass over every open trade
    if reconcile_due:
      periodic_reconciliation()  — PASS 1/2/3/4
    heartbeat every 30s
```

### 4.1 `_check_exits()` — the per-cycle sweep

```
now_pt = get_market_clock().now_pt

# Gate 1 — hard cutoff (13:00 PT)
if clock.is_past_close():
    return

# Gate 2 — EOD sweep window (12:55 – 13:00 PT)
if clock.in_eod_sweep_window():
    _run_eod_sweep(trades)       # force-close everything, reason='EOD'
    return

# Normal path: iterate every open trade
trades = list(self.open_trades)

# Remove expired contracts
for t in trades:
    if _is_expired(t.symbol):
        close_trade(db_id, ..., "EXPIRED")

# Batch fetch option prices
batch_prices = client.get_option_prices_batch(symbols)

# Bulk DB update with current P&L
for t in trades:
    update_trade_price(db_id, price, pnl_pct, pnl_usd, peak_pnl_pct, dynamic_sl_pct)

# Process exits per trade
for t in trades:
    # Trail update → modify bracket SL on IB if needed
    update_trailing_stop(t, pnl_pct)
    if new_sl != old_sl and t has ib_sl_order_id:
        client.update_bracket_sl(t.ib_sl_order_id, new_sl_price)

    # Decide if an exit should fire
    exit_info = evaluate_exit(t, pnl_pct, now_pt)
    if exit_info.should_exit:
        _atomic_close(t, price, result, exit_info.reason, ...)
```

### 4.2 Exit triggers (from `strategy/exit_conditions.py::evaluate_exit`)

| Reason | Condition |
|--------|-----------|
| `TP` | pnl_pct ≥ profit_target |
| `SL` | pnl_pct ≤ dynamic_sl_pct (bracket SL already on IB; this is a backup catch) |
| `TIME_EXIT` | trade age ≥ `MAX_HOLD_MINUTES` OR approaching expiry same-day |
| `ROLL` | see 4.3 |
| `EOD` | fired by `_run_eod_sweep` at 12:55 PT, not by `evaluate_exit` |
| `MANUAL` | dashboard `/api/trades/{id}/close` endpoint |
| `RECONCILE` | reconcile PASS 1 sees DB row with no IB position |

### 4.3 Roll trigger

Specifically in `check_roll_condition(trade, pnl_pct)`:

```
roll = config.ROLL_ENABLED
       AND pnl_pct ≥ ROLL_THRESHOLD (default 0.80, user lowered to 0.15 for testing)
       AND not already rolled in this chain
       AND time-to-expiry < ROLL_MIN_DAYS

When roll fires:
  trade['_should_roll'] = True
  exit_info.reason = 'ROLL at +X%'
  exit_info.should_exit = True
```

Then `_atomic_close` is called with `should_roll=True`. Inside:

```
_atomic_close(trade, price, result='WIN', reason='ROLL', should_roll=True):
  lock DB row
  execute_roll(client, live_trade, pnl_pct)
    # 1. cancel_all_orders_and_verify (the bracket children)
    # 2. SELL the current position
    # 3. verify close on IB
    # 4. re-enter at next-ATM strike (same direction)
    # returns the NEW trade dict
  verify_close_on_ib(old trade)
  audit 'close:ROLL' on old
  finalize_close(old_db_id)
  exit_manager.add_trade(rolled)     ← this inserts a new DB row
  audit 'roll_open' on new, from_trade_id=old_db_id
```

The ticker thread is **not involved** in the roll. The exit manager
does everything — cancel, close, re-enter, register new trade. A side
effect: the new trade placement uses the exit manager's IB client
(clientId = base_id), not the scanner's client. Same cross-client
pattern that causes bracket-cancel issues; see the thread-owned-close
proposal for why this should change.

---

## 5. Close authority — who actually closes a trade?

One line summary: **ONLY `_atomic_close()` closes trades.** (ARCH-005.)

Who can call `_atomic_close`:

| Caller | Trigger |
|--------|---------|
| `exit_manager._check_exits` | TP/SL/ROLL/TIME_EXIT reached |
| `exit_manager._run_eod_sweep` | 12:55–13:00 PT window |
| `exit_manager._process_ui_commands` | dashboard Close button |
| `strategy.reconciliation.periodic_reconciliation` PASS 1 | DB-open trade vanished from IB |

None of those run on the ticker thread. The ticker thread never
closes a trade.

### The atomic close contract

`_atomic_close(trade, price, result, reason, pnl_pct, should_roll,
reason_detail='')`:

1. `lock_trade_for_close(trade_id)` — DB row-level `SELECT ... FOR
   UPDATE NOWAIT`; if can't acquire → bail (some other caller is
   already closing it).
2. Read **live** trade state from the lock (not in-memory cache).
3. IB work:
   - `execute_roll()` if rolling, else `execute_exit()` — inside each:
     `cancel_all_orders_and_verify` → `SELL` → bracket-fired check.
4. `_verify_close_on_ib()` — poll for position=0 (3s), sweep
   stragglers. If position went **negative**, call
   `_recover_negative_position()` — defensive BUY to flatten.
5. `collect_exit_enrichment()`.
6. `log_trade_result()` — CSV log.
7. `finalize_close()` — DB update + release lock.
8. Post-lock: email, audit, `add_trade(rolled)` for rolls.

See `docs/roll_close_bug_fixes.md` for the Fix A/B/C layers and
`docs/bracket_cancel_strict_verification.md` for the MSFT fix.

---

## 6. Timeline of a typical trade

```
  t=0   scanner-INTC detects LONG_OB signal on 1m close
        calls trade_manager.enter(signal)
        IB client places bracket BUY (parent + TP LMT + SL STP)
        insert_trade() writes db_id=1073
        audit: 'open'   [actor=scanner-INTC]

  t+5s  exit_manager._check_exits sees new open trade in cache
        starts monitoring price, P&L, trailing stop

  t+2min  price moves up to +15%
          trail SL lifts from -60% to -30% of entry
          client.update_bracket_sl(SL_order_id, new_price)

  t+15min  peak_pnl_pct hits +80%, roll condition triggers
           _atomic_close(t, price, 'WIN', 'ROLL', should_roll=True)
           execute_roll:
             cancel TP + SL
             SELL 2 @ market (on exit-mgr's IB client)
             select_and_enter: new ATM strike
             BUY 2 new @ market with fresh bracket
           finalize_close(old db_id=1073)
           add_trade(new)      → db_id=1105
           audit: 'close:ROLL'  [actor=exit_manager]
           audit: 'roll_open'   [actor=exit_manager, from_trade_id=1073]

  t+60min  new trade hits TP ($6.50) — bracket TP fills on IB
           exit_manager.check_bracket_orders_active sees qty=0
           _atomic_close with reason='TP', should_roll=False
           finalize_close(db_id=1105)
           audit: 'close:TP'   [actor=exit_manager]

  end-of-day 12:55 PT:  (only if trade still open)
           _run_eod_sweep iterates all open trades
           _atomic_close(t, price, 'SCRATCH', 'EOD', should_roll=False)
           audit: 'close:EOD'
  13:00 PT: exit_manager goes silent until next day
```

---

## 7. What can the ticker thread NOT do

- **Can't close a trade.** Only exit_manager does that via `_atomic_close`.
- **Can't cancel bracket orders.** Those were placed on the ticker's
  client; exit_manager cancels them via cross-client fan-out
  (`cancel_order_by_id` iterates pool connections — commit
  `1a15d50`).
- **Can't roll a trade.** That's an exit-manager-driven "close old +
  open new" operation.
- **Can't see the realtime P&L of its own trades.** The ticker thread
  has no read-back loop after entry; it lives on its 60-second scan
  cadence, agnostic of the trade's subsequent life.

## 8. What the ticker thread MUST do (and today reliably does)

- **Exactly one `add_trade()` call** per signal that passed all gates.
  The `_entry_pending` flag is the single-entry gate within the
  ticker thread's `can_enter()`. ARCH-006 guarantees duplicate
  prevention at the DB layer.
- **Stamp the ticker thread's IB client** on the bracket orders by
  virtue of placing them through that client. (Proposed:
  `trades.owning_client_id` column — not yet populated; see
  `docs/thread_owned_close.md`.)
- **Update its thread_status row every scan cycle** so the Threads
  page shows liveness.
- **Respect EOD gates**: if `can_enter()` returns False with reason
  "EOD sweep window" or "market closed", do not retry.

---

## 9. Failure modes this design tolerates

| Failure | Who notices | Behavior |
|---------|-------------|----------|
| Ticker thread crashes | reconcile | On restart, reconcile PASS 1 closes stale DB trades with `exit_reason='RECONCILE'` if their IB position is gone. Scanner is restarted on next bot cycle. |
| Entry placed but DB write fails | reconcile | PASS 2 adopts the IB position with `reconcile_adopt` audit. |
| Ticker thread doesn't fire for minutes (stuck on IB call) | exit_manager + Threads page | `update_thread_status` timestamp goes stale; dashboard marks scanner as `dead`. |
| Close runs while ticker thread tries a concurrent entry | row-level DB lock | `lock_trade_for_close` uses `FOR UPDATE NOWAIT`; one side fails gracefully. |
| Bracket cancel fails cross-client (Error 10147) | exit_manager | `cancel_order_by_id` fans out across pool connections; one of them owns the order and succeeds. |
| Position left naked after close aborts | reconcile PASS 4 | Detects `ib_tp_status ∈ {Cancelled, MISSING}`; `_restore_brackets_for` places fresh TP+SL. |

---

## 10. Known architectural smell (proposal pending)

The cross-client pattern — entry on ticker's client, exit on
exit_manager's client — creates two related problems:

1. **Cancel asymmetry** — the exit_manager can't directly cancel
   bracket children placed by the ticker's client. IB returns Error
   10147 silently. Worked around via fan-out (`1a15d50`) and
   compensating-transaction (`ce55dce`).
2. **Order-ID namespace collisions** — each client has its own
   orderId counter. Routing cancels by orderId across clients is
   unreliable; we now prefer `permId` (globally unique).

The proper fix is in `docs/thread_owned_close.md`: let the ticker
thread close its own trades. The exit_manager becomes orchestrator
(monitor, decide, message) instead of executor. Four-phase rollout.
Not implemented yet; waits for your go.

---

## 11. File map

| File | Role |
|------|------|
| `strategy/scanner.py` | `Scanner` class — the per-ticker thread |
| `strategy/trade_entry_manager.py` | `TradeEntryManager` — gates + entry flow |
| `strategy/option_selector.py` | `select_and_enter` / `select_and_enter_put` — strike pick + order placement |
| `strategy/exit_manager.py` | `ExitManager` — monitoring, `_check_exits`, `_atomic_close` |
| `strategy/exit_executor.py` | `execute_exit`, `execute_roll`, `cancel_all_orders_and_verify` |
| `strategy/exit_conditions.py` | `evaluate_exit`, `check_roll_condition`, `check_tp_to_trail` |
| `strategy/reconciliation.py` | `periodic_reconciliation` + PASS 1/2/3/4 |
| `strategy/market_hours.py` | `MarketClock`, `get_market_clock()` |
| `broker/ib_pool.py` | `IBConnectionPool`, `get_scanner_connection(ticker)` |
| `main.py` | Thread creation (scanners + exit_manager + reconcile) |

---

## 12. Related docs

- `docs/logging_and_audit.md` — full audit vocabulary + trace
- `docs/roll_close_bug_fixes.md` — Fix A/B/C from Sunday
- `docs/bracket_cancel_strict_verification.md` — MSFT incident
- `docs/bracket_rollback_semantics.md` — compensating-transaction pattern
- `docs/orphan_bracket_detector.md` — PASS 3 detector
- `docs/market_hours_guards.md` — EOD sweep + hard cutoff
- `docs/thread_owned_close.md` — proposed architecture change
