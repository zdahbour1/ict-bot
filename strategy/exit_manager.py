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
import config

log = logging.getLogger(__name__)
PT  = pytz.timezone("America/Los_Angeles")

TRADES_FILE = os.path.join(os.path.dirname(__file__), "..", "open_trades.json")


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
        log.info(f"Exit manager now tracking: {trade['symbol']}")

    def start(self):
        thread = threading.Thread(target=self._monitor_loop, daemon=True)
        thread.start()
        log.info("Exit manager started.")

    def stop(self):
        self._stop_event.set()

    def _monitor_loop(self):
        while not self._stop_event.is_set():
            self._check_exits()
            time.sleep(config.MONITOR_INTERVAL)

    def _check_exits(self):
        now_pt = datetime.now(PT)

        with self._lock:
            still_open = []
            for trade in self.open_trades:
                try:
                    current_price = self.client.get_option_price(trade["symbol"])
                    entry_price   = trade["entry_price"]
                    pnl_pct       = (current_price - entry_price) / entry_price

                    # ── Update peak P&L ───────────────────────
                    if pnl_pct > trade["peak_pnl_pct"]:
                        trade["peak_pnl_pct"] = pnl_pct

                    # ── Trailing stop logic ───────────────────
                    if trade["peak_pnl_pct"] >= 0.20:
                        trade["dynamic_sl_pct"] = trade["peak_pnl_pct"] - 0.10
                    elif trade["peak_pnl_pct"] >= 0.10:
                        trade["dynamic_sl_pct"] = 0.00

                    # ── Time exit (90 minutes) ────────────────
                    entry_time = trade.get("entry_time")
                    bars_held  = 0
                    if entry_time:
                        elapsed_min = (datetime.now(PT) - entry_time).total_seconds() / 60
                        bars_held   = elapsed_min
                    time_exit = bars_held >= 90

                    # ── EOD exit (1:00 PM PT) ─────────────────
                    eod_exit = now_pt.hour >= 13

                    # ── Check exit conditions ─────────────────
                    hit_tp = pnl_pct >= config.PROFIT_TARGET
                    hit_sl = pnl_pct <= trade["dynamic_sl_pct"]

                    log.info(
                        f"Monitoring {trade['symbol']} | "
                        f"Current: ${current_price:.2f} | "
                        f"P&L: {pnl_pct:+.1%} | "
                        f"Peak: {trade['peak_pnl_pct']:+.1%} | "
                        f"SL: {trade['dynamic_sl_pct']:+.1%}"
                    )

                    if hit_tp or hit_sl or time_exit or eod_exit:
                        if hit_tp:
                            result = "WIN"
                            reason = "TAKE PROFIT"
                        elif hit_sl and trade["dynamic_sl_pct"] == 0.0:
                            result = "SCRATCH"
                            reason = "BREAKEVEN"
                        elif hit_sl and trade["dynamic_sl_pct"] > 0:
                            result = "WIN"
                            reason = "TRAIL STOP"
                        elif hit_sl:
                            result = "LOSS"
                            reason = "STOP LOSS"
                        elif time_exit:
                            result = "WIN" if pnl_pct > 0 else "LOSS" if pnl_pct < 0 else "SCRATCH"
                            reason = "TIME EXIT (90min)"
                        else:
                            result = "WIN" if pnl_pct > 0 else "LOSS"
                            reason = "EOD EXIT"

                        direction = trade.get("direction", "LONG")
                        if direction == "SHORT":
                            self.client.sell_put(trade["symbol"], trade["contracts"])
                        else:
                            self.client.sell_call(trade["symbol"], trade["contracts"])

                        self._log_result(trade, current_price, result, reason)
                        send_trade_result_email(trade, result, current_price)
                        # Don't add to still_open — trade is closed
                    else:
                        still_open.append(trade)

                except Exception as e:
                    log.error(f"Error monitoring {trade['symbol']}: {e}")
                    still_open.append(trade)

            self.open_trades = still_open

            # Save after every check
            if self.open_trades:
                self._save_trades()
            else:
                self._clear_trades()

    def _log_result(self, trade: dict, exit_price: float, result: str, reason: str):
        pnl_pct = (exit_price - trade["entry_price"]) / trade["entry_price"] * 100
        pnl_usd = (exit_price - trade["entry_price"]) * 100 * trade["contracts"]
        log.info(
            f"{'='*50}\n"
            f"TRADE CLOSED — {result} ({reason})\n"
            f"Symbol:  {trade['symbol']}\n"
            f"Entry:   ${trade['entry_price']:.2f}\n"
            f"Exit:    ${exit_price:.2f}\n"
            f"P&L:     {pnl_pct:+.1f}%  (${pnl_usd:+.2f})\n"
            f"{'='*50}"
        )
        import csv
        log_path     = "trades.csv"
        write_header = not os.path.exists(log_path)
        with open(log_path, "a", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(["entry_time", "symbol", "direction", "contracts",
                                 "entry_price", "exit_price", "pnl_pct",
                                 "pnl_usd", "result", "reason"])
            writer.writerow([
                trade.get("entry_time"), trade["symbol"],
                trade.get("direction", "LONG"), trade["contracts"],
                trade["entry_price"], exit_price,
                round(pnl_pct, 2), round(pnl_usd, 2), result, reason
            ])
