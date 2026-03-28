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
"""
import logging
import threading
import time
from datetime import datetime
import pytz

from broker.tastytrade_client import TastytradeClient
from alerts.emailer import send_trade_result_email
import config

log = logging.getLogger(__name__)
PT  = pytz.timezone("America/Los_Angeles")


class ExitManager:
    def __init__(self, client: TastytradeClient):
        self.client       = client
        self.open_trades  = []
        self._lock        = threading.Lock()
        self._stop_event  = threading.Event()

    def add_trade(self, trade: dict):
        # Initialise trailing stop state when trade is added
        trade["peak_pnl_pct"] = 0.0
        trade["dynamic_sl_pct"] = -config.STOP_LOSS
        with self._lock:
            self.open_trades.append(trade)
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
                        trade["dynamic_sl_pct"] = 0.00   # breakeven

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
                        # Determine exit reason + result
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

                        # Close the position
                        direction = trade.get("direction", "LONG")
                        if direction == "SHORT":
                            self.client.sell_put(trade["symbol"], trade["contracts"])
                        else:
                            self.client.sell_call(trade["symbol"], trade["contracts"])

                        self._log_result(trade, current_price, result, reason)
                        send_trade_result_email(trade, result, current_price)
                    else:
                        still_open.append(trade)

                except Exception as e:
                    log.error(f"Error monitoring {trade['symbol']}: {e}")
                    still_open.append(trade)

            self.open_trades = still_open

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
        import csv, os
        log_path   = "trades.csv"
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
