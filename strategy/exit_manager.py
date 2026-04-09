"""
Exit Manager
Runs in the background and monitors open trades every 30 seconds.
Exits when:
  - Option up 100%              → Take Profit
  - Option down 60%             → Stop Loss
  - Trailing stop hit           → up 10% moves SL to breakeven
                                  up 20% trails at peak - 10%
  - 90 minutes elapsed          → Time Exit (avoids theta decay)
  - 1:00 PM PT                  → EOD Exit

Persistent storage: open trades saved to open_trades.json on every update.
On restart, any saved trades are reloaded and monitoring resumes automatically.
"""
import logging
import threading
import time
import json
import os
from datetime import datetime
import pytz

from alerts.emailer import send_trade_result_email
from strategy.indicators import compute_snapshot
import config

log = logging.getLogger(__name__)
PT  = pytz.timezone("America/Los_Angeles")

TRADES_FILE = os.path.join(os.path.dirname(__file__), "..", "open_trades.json")
LOGS_DIR    = os.path.join(os.path.dirname(__file__), "..", "logs")

# Thread-safe lock for CSV writes (shared across all ExitManager instances)
_csv_lock = threading.Lock()


def _serialize_trade(trade: dict) -> dict:
    """Convert trade dict to JSON-serializable format."""
    t = {}
    for k, v in trade.items():
        if isinstance(v, datetime):
            t[k] = v.isoformat()
        elif isinstance(v, float) and (v != v):  # NaN check
            t[k] = 0.0
        else:
            t[k] = v
    return t


def _deserialize_trade(t: dict) -> dict:
    """Restore trade dict from JSON format."""
    entry = t.get("entry_time")
    if isinstance(entry, str):
        try:
            dt = datetime.fromisoformat(entry)
            if dt.tzinfo is None:
                dt = PT.localize(dt)
            else:
                dt = dt.astimezone(PT)
            t["entry_time"] = dt
        except Exception:
            t["entry_time"] = datetime.now(PT)
    return t


class ExitManager:
    def __init__(self, client):
        self.client       = client
        self.open_trades  = []
        self._lock        = threading.Lock()
        self._stop_event  = threading.Event()
        self._load_trades()

    # ── Persistent storage ────────────────────────────────
    def _load_trades(self):
        """Load open trades from disk on startup."""
        if not os.path.exists(TRADES_FILE):
            return
        try:
            with open(TRADES_FILE, "r") as f:
                saved = json.load(f)
            self.open_trades = [_deserialize_trade(t) for t in saved]
            if self.open_trades:
                log.info(f"Resumed {len(self.open_trades)} open trade(s) from previous session:")
                for t in self.open_trades:
                    log.info(f"  {t['symbol']} | Entry: ${t['entry_price']:.2f} | Peak: {t.get('peak_pnl_pct', 0):+.1%}")
        except Exception as e:
            log.warning(f"Could not load open trades: {e}")
            self.open_trades = []

    def _save_trades(self):
        """Save current open trades to disk."""
        try:
            with open(TRADES_FILE, "w") as f:
                json.dump([_serialize_trade(t) for t in self.open_trades], f, indent=2)
        except Exception as e:
            log.warning(f"Could not save open trades: {e}")

    def _clear_trades(self):
        """Clear the trades file when all trades are closed."""
        try:
            if os.path.exists(TRADES_FILE):
                os.remove(TRADES_FILE)
        except Exception:
            pass

    # ── Trade management ──────────────────────────────────
    def add_trade(self, trade: dict):
        trade["peak_pnl_pct"]  = 0.0
        trade["dynamic_sl_pct"] = -config.STOP_LOSS
        with self._lock:
            self.open_trades.append(trade)
            self._save_trades()
        # Write to DB
        try:
            from db.writer import insert_trade
            db_id = insert_trade(trade, config.IB_ACCOUNT or "unknown")
            if db_id:
                trade["db_id"] = db_id
                log.info(f"Trade saved to DB: id={db_id} {trade.get('ticker')} {trade['symbol']}")
            else:
                log.warning(f"DB insert_trade returned None for {trade.get('ticker')}")
        except Exception as e:
            log.warning(f"DB insert_trade failed: {e}")
        log.info(f"Exit manager now tracking: {trade['symbol']}")

    def start(self):
        thread = threading.Thread(target=self._monitor_loop, daemon=True)
        thread.start()
        log.info("Exit manager started.")

    def stop(self):
        self._stop_event.set()

    def _monitor_loop(self):
        reconcile_counter = 0
        reconcile_interval = config.RECONCILIATION_INTERVAL_MIN * 60 // config.MONITOR_INTERVAL
        while not self._stop_event.is_set():
            self._check_exits()
            # Periodic IB reconciliation
            reconcile_counter += 1
            if reconcile_counter >= reconcile_interval:
                reconcile_counter = 0
                self._reconcile_with_ib()
            time.sleep(config.MONITOR_INTERVAL)

    def _reconcile_with_ib(self):
        """Compare bot's open_trades with IB's actual positions. Detect discrepancies."""
        try:
            ib_positions = self.client.get_ib_positions_raw()
        except Exception as e:
            log.debug(f"Reconciliation skipped: {e}")
            return

        if not ib_positions:
            return

        with self._lock:
            bot_symbols = {t["symbol"] for t in self.open_trades}
            ib_symbols = set()
            for p in ib_positions:
                # Build OCC-like symbol from IB position
                sym = p.get("symbol", "").replace(" ", "")
                if sym:
                    ib_symbols.add(sym)

            # Orphaned IB positions (on IB but bot doesn't know)
            for p in ib_positions:
                sym = p.get("symbol", "").replace(" ", "")
                if sym and sym not in bot_symbols and p["qty"] > 0:
                    log.warning(f"[RECONCILE] Orphaned IB position: {sym} qty={p['qty']} "
                                f"avg_cost=${p['avg_cost']:.2f} — NOT tracked by bot")
                    # Auto-adopt: create a trade entry
                    try:
                        trade = {
                            "ticker": p.get("ticker", "UNK"),
                            "symbol": sym,
                            "contracts": int(abs(p["qty"])),
                            "entry_price": p["avg_cost"],
                            "profit_target": p["avg_cost"] * (1 + config.PROFIT_TARGET),
                            "stop_loss": p["avg_cost"] * (1 - config.STOP_LOSS),
                            "entry_time": datetime.now(PT),
                            "direction": "SHORT" if p.get("right") == "P" else "LONG",
                            "_adopted": True,  # flag for tracking
                        }
                        trade["peak_pnl_pct"] = 0.0
                        trade["dynamic_sl_pct"] = -config.STOP_LOSS
                        self.open_trades.append(trade)
                        self._save_trades()
                        # Write to DB
                        try:
                            from db.writer import insert_trade
                            db_id = insert_trade(trade, config.IB_ACCOUNT or "unknown")
                            if db_id:
                                trade["db_id"] = db_id
                        except Exception:
                            pass
                        log.info(f"[RECONCILE] Adopted orphan: {sym} → tracking with "
                                 f"TP=${trade['profit_target']:.2f} SL=${trade['stop_loss']:.2f}")
                    except Exception as e:
                        log.error(f"[RECONCILE] Failed to adopt {sym}: {e}")

            # Phantom bot trades (bot thinks open, but not on IB)
            for trade in list(self.open_trades):
                sym = trade["symbol"]
                if sym not in ib_symbols and not trade.get("_adopted"):
                    log.warning(f"[RECONCILE] Phantom bot trade: {sym} — "
                                f"tracked by bot but NOT on IB. Removing.")
                    self.open_trades.remove(trade)
                    if trade.get("db_id"):
                        try:
                            from db.writer import mark_trade_errored
                            mark_trade_errored(trade["db_id"], "Phantom trade: not found on IB during reconciliation")
                        except Exception:
                            pass

            self._save_trades()

    def _check_exits(self):
        now_pt = datetime.now(PT)

        with self._lock:
            still_open = []
            for trade in self.open_trades:
                try:
                    try:
                        current_price = self.client.get_option_price(trade["symbol"], priority=True)
                    except (TimeoutError, Exception) as price_err:
                        log.warning(f"Price fetch failed for {trade.get('ticker')} {trade['symbol']}: {price_err}")
                        still_open.append(trade)
                        continue
                    entry_price   = trade["entry_price"]
                    pnl_pct       = (current_price - entry_price) / entry_price

                    # ── Update peak P&L ───────────────────────
                    if pnl_pct > trade["peak_pnl_pct"]:
                        trade["peak_pnl_pct"] = pnl_pct

                    # ── Trailing stop logic ───────────────────
                    # SL stays at -60% but resets relative to peak
                    # every time peak moves up by another 10% increment.
                    peak = trade["peak_pnl_pct"]
                    old_sl = trade["dynamic_sl_pct"]
                    steps = int(peak / 0.10)
                    if steps > 0:
                        trail_base = steps * 0.10
                        trade["dynamic_sl_pct"] = trail_base - config.STOP_LOSS

                    # ── Update bracket SL on IB if trail changed ──
                    if trade["dynamic_sl_pct"] != old_sl and trade.get("ib_sl_order_id"):
                        new_sl_price = round(entry_price * (1 + trade["dynamic_sl_pct"]), 2)
                        try:
                            self.client.update_bracket_sl(trade["ib_sl_order_id"], new_sl_price)
                            log.info(f"[{trade.get('ticker')}] Bracket SL updated → ${new_sl_price:.2f} ({trade['dynamic_sl_pct']:+.0%})")
                        except Exception:
                            pass

                    # ── Time exit (90 minutes) ────────────────
                    entry_time = trade.get("entry_time")
                    bars_held  = 0
                    if entry_time:
                        elapsed_min = (datetime.now(PT) - entry_time).total_seconds() / 60
                        bars_held   = elapsed_min
                    time_exit = bars_held >= 90

                    # ── EOD exit (1:00 PM PT) ─────────────────
                    eod_exit = now_pt.hour >= 13

                    # ── TP → Trailing Stop (don't hard exit, let it run) ─
                    hit_tp = False
                    if config.TP_TO_TRAIL and pnl_pct >= config.PROFIT_TARGET:
                        if not trade.get("_tp_trailed"):
                            # First time hitting TP: move SL to TP level instead of exiting
                            trade["dynamic_sl_pct"] = config.PROFIT_TARGET - config.STOP_LOSS
                            trade["_tp_trailed"] = True
                            log.info(f"[{trade.get('ticker')}] TP hit at {pnl_pct:+.1%} — "
                                     f"converting to trailing stop at {trade['dynamic_sl_pct']:+.0%}")
                            # Update bracket SL on IB
                            if trade.get("ib_sl_order_id"):
                                new_sl_price = round(entry_price * (1 + trade["dynamic_sl_pct"]), 2)
                                try:
                                    self.client.update_bracket_sl(trade["ib_sl_order_id"], new_sl_price)
                                except Exception:
                                    pass
                            # Cancel the TP bracket leg — we're trailing now
                            if trade.get("ib_tp_order_id"):
                                try:
                                    self.client.cancel_bracket_children(trade["ib_tp_order_id"])
                                    trade["ib_tp_order_id"] = None
                                except Exception:
                                    pass
                        # Don't set hit_tp = True — let it run
                    elif not config.TP_TO_TRAIL:
                        hit_tp = pnl_pct >= config.PROFIT_TARGET

                    # ── Option Rolling at ~70% ────────────────
                    roll_trade = None
                    if (config.ROLL_ENABLED and not trade.get("_rolled")
                            and pnl_pct >= config.ROLL_THRESHOLD * config.PROFIT_TARGET):
                        # Roll: close this position and open next strike
                        log.info(f"[{trade.get('ticker')}] Roll trigger at {pnl_pct:+.1%} — "
                                 f"closing and rolling to next strike")
                        trade["_rolled"] = True
                        # We'll handle the roll after closing this trade
                        # Set a flag so the close logic knows to roll
                        trade["_should_roll"] = True

                    hit_sl = pnl_pct <= trade["dynamic_sl_pct"]

                    # ── Update DB with live pricing ───────────
                    if trade.get("db_id"):
                        try:
                            from db.writer import update_trade_price
                            pnl_usd_live = (current_price - entry_price) * 100 * trade["contracts"]
                            update_trade_price(
                                trade["db_id"], current_price, pnl_pct,
                                pnl_usd_live, trade["peak_pnl_pct"], trade["dynamic_sl_pct"]
                            )
                        except Exception:
                            pass

                    log.info(
                        f"Monitoring {trade['symbol']} | "
                        f"Current: ${current_price:.2f} | "
                        f"P&L: {pnl_pct:+.1%} | "
                        f"Peak: {trade['peak_pnl_pct']:+.1%} | "
                        f"SL: {trade['dynamic_sl_pct']:+.1%}"
                    )

                    should_roll = trade.get("_should_roll", False)

                    if hit_tp or hit_sl or time_exit or eod_exit or should_roll:
                        if should_roll:
                            result = "WIN"
                            reason = f"ROLL (P&L={pnl_pct:+.0%})"
                        elif hit_tp:
                            result = "WIN"
                            reason = "TAKE PROFIT"
                        elif hit_sl and trade["dynamic_sl_pct"] > -config.STOP_LOSS:
                            result = "WIN" if pnl_pct > 0 else "LOSS" if pnl_pct < 0 else "SCRATCH"
                            reason = f"TRAIL STOP (SL={trade['dynamic_sl_pct']:+.0%})"
                        elif hit_sl:
                            result = "LOSS"
                            reason = "STOP LOSS"
                        elif time_exit:
                            result = "WIN" if pnl_pct > 0 else "LOSS" if pnl_pct < 0 else "SCRATCH"
                            reason = "TIME EXIT (90min)"
                        else:
                            result = "WIN" if pnl_pct > 0 else "LOSS"
                            reason = "EOD EXIT"

                        # Cancel bracket children before manual close
                        if trade.get("ib_tp_order_id") or trade.get("ib_sl_order_id"):
                            try:
                                tp_id = trade.get("ib_tp_order_id")
                                sl_id = trade.get("ib_sl_order_id")
                                ids = [i for i in [tp_id, sl_id] if i]
                                if ids:
                                    self.client.cancel_bracket_children(*ids)
                            except Exception:
                                pass

                        direction = trade.get("direction", "LONG")
                        if direction == "SHORT":
                            self.client.sell_put(trade["symbol"], trade["contracts"])
                        else:
                            self.client.sell_call(trade["symbol"], trade["contracts"])

                        # ── Exit-time enrichment ──────────────
                        exit_enrichment = {"exit_time": datetime.now(PT)}
                        try:
                            ticker = trade.get("ticker", "QQQ")
                            from data.ib_provider import get_bars_1m_ib
                            exit_bars = get_bars_1m_ib(self.client, ticker, days_back=2)
                            exit_enrichment["exit_indicators"] = compute_snapshot(exit_bars)
                        except Exception:
                            exit_enrichment["exit_indicators"] = {}
                        try:
                            exit_enrichment["exit_stock_price"] = self.client.get_realtime_equity_price(ticker)
                        except Exception:
                            exit_enrichment["exit_stock_price"] = None
                        try:
                            exit_enrichment["exit_greeks"] = self.client.get_option_greeks(trade["symbol"])
                        except Exception:
                            exit_enrichment["exit_greeks"] = {}
                        try:
                            exit_enrichment["exit_vix"] = self.client.get_vix()
                        except Exception:
                            exit_enrichment["exit_vix"] = None

                        self._log_result(trade, current_price, result, reason, exit_enrichment)
                        send_trade_result_email(trade, result, current_price)
                        # Close in DB
                        if trade.get("db_id"):
                            try:
                                from db.writer import close_trade as db_close_trade
                                db_close_trade(trade["db_id"], current_price, result, reason, exit_enrichment)
                                log.info(f"Trade closed in DB: id={trade['db_id']} {result} ({reason})")
                            except Exception as e:
                                log.warning(f"DB close_trade failed for id={trade.get('db_id')}: {e}")
                        else:
                            log.warning(f"Trade {trade.get('ticker')} has no db_id — cannot update DB")
                        # ── Option Rolling: open next strike ──
                        if should_roll and not time_exit and not eod_exit:
                            try:
                                ticker = trade.get("ticker", "QQQ")
                                direction = trade.get("direction", "LONG")
                                from strategy.option_selector import select_and_enter, select_and_enter_put
                                if direction == "SHORT":
                                    rolled_trade = select_and_enter_put(self.client, ticker)
                                else:
                                    rolled_trade = select_and_enter(self.client, ticker)
                                if rolled_trade:
                                    rolled_trade["signal"] = f"ROLL from {trade['symbol']}"
                                    rolled_trade["_rolled_from"] = trade["symbol"]
                                    self.add_trade(rolled_trade)
                                    log.info(f"[{ticker}] Rolled to {rolled_trade['symbol']} @ ${rolled_trade['entry_price']:.2f}")
                            except Exception as e:
                                log.error(f"[{trade.get('ticker')}] Roll failed: {e}")

                        # Don't add to still_open — trade is closed
                    else:
                        still_open.append(trade)

                except Exception as e:
                    log.error(f"Error monitoring {trade['symbol']}: {e}")
                    still_open.append(trade)

            self.open_trades = still_open

            # ── Check for UI commands (close trade requests) ──
            try:
                from db.writer import check_pending_commands, complete_command
                commands = check_pending_commands()
                for cmd in (commands or []):
                    try:
                        # Find the matching open trade by db_id
                        target = None
                        for t in self.open_trades:
                            if t.get("db_id") == cmd["trade_id"]:
                                target = t
                                break
                        if target:
                            direction = target.get("direction", "LONG")
                            contracts = cmd.get("contracts") or target["contracts"]
                            if direction == "SHORT":
                                self.client.sell_put(target["symbol"], contracts)
                            else:
                                self.client.sell_call(target["symbol"], contracts)
                            log.info(f"UI command executed: close {contracts}x {target['symbol']}")
                            complete_command(cmd["id"])
                        else:
                            complete_command(cmd["id"], error="Trade not found in open trades")
                    except Exception as e:
                        complete_command(cmd["id"], error=str(e))
            except Exception:
                pass

            # Save after every check
            if self.open_trades:
                self._save_trades()
            else:
                self._clear_trades()

    def _log_result(self, trade: dict, exit_price: float, result: str, reason: str,
                    exit_enrichment: dict = None):
        pnl_pct = (exit_price - trade["entry_price"]) / trade["entry_price"] * 100
        pnl_usd = (exit_price - trade["entry_price"]) * 100 * trade["contracts"]
        ticker  = trade.get("ticker", "UNK")
        log.info(
            f"{'='*50}\n"
            f"[{ticker}] TRADE CLOSED — {result} ({reason})\n"
            f"Symbol:  {trade['symbol']}\n"
            f"Entry:   ${trade['entry_price']:.2f}\n"
            f"Exit:    ${exit_price:.2f}\n"
            f"P&L:     {pnl_pct:+.1f}%  (${pnl_usd:+.2f})\n"
            f"{'='*50}"
        )
        import csv

        if exit_enrichment is None:
            exit_enrichment = {}

        # Extract entry enrichment from trade dict
        ei = trade.get("entry_indicators", {})
        eg = trade.get("entry_greeks", {})
        # Extract exit enrichment
        xi = exit_enrichment.get("exit_indicators", {})
        xg = exit_enrichment.get("exit_greeks", {})

        header = [
            "entry_time", "exit_time", "ticker", "symbol", "direction", "contracts",
            "entry_price", "exit_price", "pnl_pct", "pnl_usd", "result", "reason",
            "entry_stock_price", "exit_stock_price",
            "entry_delta", "entry_gamma", "entry_theta", "entry_vega",
            "exit_delta", "exit_gamma", "exit_theta", "exit_vega",
            "entry_vwap", "exit_vwap",
            "entry_vix", "exit_vix",
            "entry_rsi_14", "exit_rsi_14",
            "entry_sma_7", "entry_sma_10", "entry_sma_20", "entry_sma_50",
            "exit_sma_7", "exit_sma_10", "exit_sma_20", "exit_sma_50",
            "entry_ema_7", "entry_ema_10", "entry_ema_20", "entry_ema_50",
            "exit_ema_7", "exit_ema_10", "exit_ema_20", "exit_ema_50",
            "entry_macd_line", "entry_macd_signal", "entry_macd_histogram",
            "exit_macd_line", "exit_macd_signal", "exit_macd_histogram",
        ]

        row = [
            trade.get("entry_time"),
            exit_enrichment.get("exit_time"),
            ticker, trade["symbol"],
            trade.get("direction", "LONG"), trade["contracts"],
            trade["entry_price"], exit_price,
            round(pnl_pct, 2), round(pnl_usd, 2), result, reason,
            trade.get("entry_stock_price"),
            exit_enrichment.get("exit_stock_price"),
            eg.get("delta"), eg.get("gamma"), eg.get("theta"), eg.get("vega"),
            xg.get("delta"), xg.get("gamma"), xg.get("theta"), xg.get("vega"),
            ei.get("vwap"), xi.get("vwap"),
            trade.get("entry_vix"), exit_enrichment.get("exit_vix"),
            ei.get("rsi_14"), xi.get("rsi_14"),
            ei.get("sma_7"), ei.get("sma_10"), ei.get("sma_20"), ei.get("sma_50"),
            xi.get("sma_7"), xi.get("sma_10"), xi.get("sma_20"), xi.get("sma_50"),
            ei.get("ema_7"), ei.get("ema_10"), ei.get("ema_20"), ei.get("ema_50"),
            xi.get("ema_7"), xi.get("ema_10"), xi.get("ema_20"), xi.get("ema_50"),
            ei.get("macd_line"), ei.get("macd_signal"), ei.get("macd_histogram"),
            xi.get("macd_line"), xi.get("macd_signal"), xi.get("macd_histogram"),
        ]

        # ── Write to daily account CSV in logs/ directory ─────
        # Filename: {account}_{YYYYMMDD_HH24MISS}.csv (one per day)
        os.makedirs(LOGS_DIR, exist_ok=True)
        account = config.IB_ACCOUNT or "unknown"
        today_str = datetime.now(PT).strftime("%Y%m%d")

        # Find today's file for this account (reuse if exists)
        daily_file = None
        for fname in sorted(os.listdir(LOGS_DIR)):
            if fname.startswith(f"{account}_{today_str}") and fname.endswith(".csv"):
                daily_file = os.path.join(LOGS_DIR, fname)
                break

        # Create new daily file if none exists
        if daily_file is None:
            timestamp = datetime.now(PT).strftime("%Y%m%d_%H%M%S")
            daily_file = os.path.join(LOGS_DIR, f"{account}_{timestamp}.csv")

        # Thread-safe CSV write
        with _csv_lock:
            write_header = not os.path.exists(daily_file)
            with open(daily_file, "a", newline="") as f:
                writer = csv.writer(f)
                if write_header:
                    writer.writerow(header)
                writer.writerow(row)
            log.info(f"Trade logged to {daily_file}")
