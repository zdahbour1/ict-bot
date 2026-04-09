"""
Exit Manager — orchestrates trade monitoring, exit decisions, and position closure.

Responsibilities:
- Monitor open trades every 5 seconds (batch IB price fetch)
- Evaluate exit conditions (TP, SL, trail, time, EOD, roll)
- Execute exits: cancel brackets → verify position → sell
- Update DB and CSV for all state changes
- Process UI commands (close from dashboard)
- Persist trade state to open_trades.json

The exit manager is the SINGLE AUTHORITY for closing trades.
Bracket orders on IB are safety nets only.
"""
import logging
import re
import threading
import time
import json
import os
from datetime import datetime, date
import pytz

from alerts.emailer import send_trade_result_email
from strategy.exit_conditions import evaluate_exit, update_trailing_stop
from strategy.exit_executor import execute_exit, execute_roll, cancel_bracket_orders, verify_position_exists
from strategy.trade_logger import log_trade_result, close_trade_in_db, collect_exit_enrichment
from strategy.reconciliation import periodic_reconciliation
import config

log = logging.getLogger(__name__)
PT = pytz.timezone("America/Los_Angeles")

TRADES_FILE = os.path.join(os.path.dirname(__file__), "..", "open_trades.json")


def _serialize_trade(trade: dict) -> dict:
    t = {}
    for k, v in trade.items():
        if isinstance(v, datetime):
            t[k] = v.isoformat()
        elif isinstance(v, float) and (v != v):
            t[k] = 0.0
        else:
            t[k] = v
    return t


def _deserialize_trade(t: dict) -> dict:
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


def _is_expired(symbol: str) -> bool:
    match = re.match(r'^[A-Z]+(\d{6})[CP]\d+$', symbol)
    if not match:
        return False
    exp_str = match.group(1)
    try:
        exp_date = date(2000 + int(exp_str[:2]), int(exp_str[2:4]), int(exp_str[4:6]))
        return exp_date < date.today()
    except (ValueError, IndexError):
        return False


class ExitManager:
    def __init__(self, client):
        self.client = client
        self.open_trades = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._load_trades()

    # ── Persistent storage ────────────────────────────────
    def _load_trades(self):
        if not os.path.exists(TRADES_FILE):
            return
        try:
            with open(TRADES_FILE, "r") as f:
                saved = json.load(f)
            self.open_trades = [_deserialize_trade(t) for t in saved]
            if self.open_trades:
                log.info(f"Resumed {len(self.open_trades)} open trade(s) from previous session")
        except Exception as e:
            log.warning(f"Could not load open trades: {e}")
            self.open_trades = []

    def _save_trades(self):
        try:
            with open(TRADES_FILE, "w") as f:
                json.dump([_serialize_trade(t) for t in self.open_trades], f, indent=2)
        except Exception as e:
            log.warning(f"Could not save open trades: {e}")

    def _clear_trades(self):
        try:
            if os.path.exists(TRADES_FILE):
                os.remove(TRADES_FILE)
        except Exception:
            pass

    # ── Trade management ──────────────────────────────────
    def add_trade(self, trade: dict):
        trade["peak_pnl_pct"] = 0.0
        trade["dynamic_sl_pct"] = -config.STOP_LOSS
        with self._lock:
            self.open_trades.append(trade)
            self._save_trades()
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

    # ── Main monitoring loop ──────────────────────────────
    def _monitor_loop(self):
        reconcile_counter = 0
        reconcile_interval = config.RECONCILIATION_INTERVAL_MIN * 60 // config.MONITOR_INTERVAL
        while not self._stop_event.is_set():
            self._check_exits()
            reconcile_counter += 1
            if reconcile_counter >= reconcile_interval:
                reconcile_counter = 0
                periodic_reconciliation(self.client, self)
            time.sleep(config.MONITOR_INTERVAL)

    def _check_exits(self):
        now_pt = datetime.now(PT)

        with self._lock:
            # ── Remove expired contracts ──────────────────
            active_trades = []
            for trade in self.open_trades:
                if _is_expired(trade["symbol"]):
                    ticker = trade.get("ticker", "UNK")
                    log.warning(f"[{ticker}] Contract EXPIRED: {trade['symbol']} — auto-closing")
                    close_trade_in_db(trade, trade.get("entry_price", 0), "LOSS", "EXPIRED CONTRACT", {})
                else:
                    active_trades.append(trade)
            self.open_trades = active_trades

            # ── Batch fetch all prices ────────────────────
            symbols = [t["symbol"] for t in self.open_trades]
            try:
                batch_prices = self.client.get_option_prices_batch(symbols)
            except Exception as e:
                log.warning(f"Batch price fetch failed: {e}")
                batch_prices = {}

            # ── Bulk DB update for priced trades ──────────
            if batch_prices:
                for trade in self.open_trades:
                    price = batch_prices.get(trade["symbol"])
                    if price and trade.get("db_id"):
                        entry = trade["entry_price"]
                        pnl = (price - entry) / entry if entry > 0 else 0
                        pnl_usd = (price - entry) * 100 * trade["contracts"]
                        try:
                            from db.writer import update_trade_price
                            update_trade_price(trade["db_id"], price, pnl, pnl_usd,
                                             trade.get("peak_pnl_pct", 0), trade.get("dynamic_sl_pct", -0.6))
                        except Exception:
                            pass

            # ── Process each trade ────────────────────────
            still_open = []
            for trade in self.open_trades:
                try:
                    current_price = batch_prices.get(trade["symbol"])
                    if current_price is None:
                        still_open.append(trade)
                        continue

                    entry_price = trade["entry_price"]
                    pnl_pct = (current_price - entry_price) / entry_price

                    # Update peak
                    if pnl_pct > trade["peak_pnl_pct"]:
                        trade["peak_pnl_pct"] = pnl_pct

                    # Update trailing stop + bracket SL on IB
                    old_sl = trade["dynamic_sl_pct"]
                    trade["dynamic_sl_pct"] = update_trailing_stop(trade, pnl_pct)
                    if trade["dynamic_sl_pct"] != old_sl and trade.get("ib_sl_order_id"):
                        new_sl_price = round(entry_price * (1 + trade["dynamic_sl_pct"]), 2)
                        try:
                            self.client.update_bracket_sl(trade["ib_sl_order_id"], new_sl_price)
                            log.info(f"[{trade.get('ticker')}] Bracket SL → ${new_sl_price:.2f}")
                        except Exception:
                            pass

                    log.info(
                        f"Monitoring {trade['symbol']} | "
                        f"${current_price:.2f} | "
                        f"P&L:{pnl_pct:+.1%} | "
                        f"Peak:{trade['peak_pnl_pct']:+.1%} | "
                        f"SL:{trade['dynamic_sl_pct']:+.1%}"
                    )

                    # ── Evaluate exit conditions ──────────
                    exit_info = evaluate_exit(trade, current_price, now_pt)

                    if exit_info:
                        result = exit_info["result"]
                        reason = exit_info["reason"]
                        should_roll = exit_info.get("should_roll", False)

                        # ══ Execute exit: cancel brackets → verify → sell ══
                        execute_exit(self.client, trade, reason)

                        # Verify the position was closed
                        if not verify_position_exists(self.client, trade):
                            log.info(f"[{trade.get('ticker')}] Position confirmed closed on IB")

                        # Collect enrichment and log
                        exit_enrichment = collect_exit_enrichment(self.client, trade)
                        log_trade_result(trade, current_price, result, reason, exit_enrichment)
                        close_trade_in_db(trade, current_price, result, reason, exit_enrichment)
                        send_trade_result_email(trade, result, current_price)

                        # Roll if needed
                        if should_roll:
                            rolled = execute_roll(self.client, trade, pnl_pct)
                            if rolled:
                                self.add_trade(rolled)

                        # Don't add to still_open — trade is closed
                    else:
                        still_open.append(trade)

                except Exception as e:
                    log.error(f"Error monitoring {trade.get('symbol', '?')}: {e}")
                    still_open.append(trade)

            self.open_trades = still_open

            # ── Process UI commands ───────────────────────
            self._process_ui_commands()

            # Save state
            if self.open_trades:
                self._save_trades()
            else:
                self._clear_trades()

    def _process_ui_commands(self):
        """Process close commands from the dashboard."""
        try:
            from db.writer import check_pending_commands, complete_command
            commands = check_pending_commands()
            for cmd in (commands or []):
                try:
                    target = None
                    for t in self.open_trades:
                        if t.get("db_id") == cmd["trade_id"]:
                            target = t
                            break
                    if target:
                        contracts = cmd.get("contracts") or target["contracts"]
                        execute_exit(self.client, target, f"UI CLOSE ({contracts}x)")
                        log.info(f"UI command executed: close {contracts}x {target['symbol']}")
                        complete_command(cmd["id"])
                    else:
                        complete_command(cmd["id"], error="Trade not found in open trades")
                except Exception as e:
                    complete_command(cmd["id"], error=str(e))
        except Exception:
            pass
