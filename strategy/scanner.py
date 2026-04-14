"""
Live Scanner — runs every minute during market hours (6:30 AM - 1:00 PM PT).
- Inside 07:00–09:00 PT  → signal email + trade entry
- Outside that window    → signal email only (alert, no trade placed)
"""
import logging
import threading
import time
from datetime import datetime, date
import pytz
import pandas as pd
import numpy as np

from data.provider import get_bars_1m
from data.ib_provider import get_bars_1m_ib
from data.aggregator import aggregate
from strategy.levels import get_all_levels
from strategy.ict_long import run_strategy
from strategy.ict_short import run_strategy_short
from strategy.option_selector import select_and_enter, select_and_enter_put
from strategy.indicators import compute_snapshot
from strategy.error_handler import handle_error, safe_call
from alerts.emailer import send_signal_email
import config

log = logging.getLogger(__name__)

PT = pytz.timezone("America/Los_Angeles")

# Full market hours window (PT) — scanner runs during this entire time
MARKET_OPEN_PT      = 6    # 6:30 AM PT
MARKET_OPEN_MIN_PT  = 30
MARKET_CLOSE_PT     = 13   # 1:00 PM PT

# ── Major news events (PT times) ─────────────────────────
import datetime as _dt
NEWS_EVENTS = [
    (_dt.date(2026, 1, 29), 11,  0,  "FOMC Decision"),
    (_dt.date(2026, 1, 29), 11, 30,  "FOMC Press Conference"),
    (_dt.date(2026, 2,  7),  5, 30,  "NFP Jobs Report"),
    (_dt.date(2026, 2, 12),  5, 30,  "CPI"),
    (_dt.date(2026, 2, 13),  5, 30,  "PPI"),
    (_dt.date(2026, 2, 14),  5, 30,  "Retail Sales"),
    (_dt.date(2026, 2, 26),  5, 30,  "GDP"),
    (_dt.date(2026, 3,  7),  5, 30,  "NFP Jobs Report"),
    (_dt.date(2026, 3, 12),  5, 30,  "CPI"),
    (_dt.date(2026, 3, 13),  5, 30,  "PPI"),
    (_dt.date(2026, 3, 14),  5, 30,  "Retail Sales"),
    (_dt.date(2026, 3, 19), 11,  0,  "FOMC Decision"),
    (_dt.date(2026, 3, 19), 11, 30,  "FOMC Press Conference"),
    (_dt.date(2026, 3, 26),  5, 30,  "GDP"),
    # Add future events here monthly
]


def _is_near_news(now_pt: datetime) -> tuple:
    """Returns (True, label) if now_pt is within NEWS_BUFFER_MIN of a major event."""
    for (ev_date, ev_h, ev_m, label) in NEWS_EVENTS:
        ev_dt = PT.localize(_dt.datetime(ev_date.year, ev_date.month, ev_date.day, ev_h, ev_m))
        diff  = abs((now_pt - ev_dt).total_seconds() / 60)
        if diff <= config.NEWS_BUFFER_MIN:
            return True, label
    return False, ""


def _get_ema_bias(bars_1h: pd.DataFrame, now_pt: datetime) -> str:
    """Returns BULLISH, BEARISH, or NEUTRAL based on 1H 20 EMA."""
    if bars_1h.empty or len(bars_1h) < config.EMA_PERIOD_1H:
        return "NEUTRAL"
    ema    = bars_1h["close"].ewm(span=config.EMA_PERIOD_1H, adjust=False).mean()
    last_close = bars_1h["close"].iloc[-1]
    last_ema   = ema.iloc[-1]
    if last_close > last_ema:
        return "BULLISH"
    elif last_close < last_ema:
        return "BEARISH"
    return "NEUTRAL"


MAX_TRADES_PER_DAY  = 8    # max trades per day

class Scanner:
    def __init__(self, client, exit_manager, ticker=None, scan_offset=0):
        self.client          = client
        self.exit_manager    = exit_manager
        self.ticker          = ticker or config.TICKER
        self._scan_offset    = scan_offset  # stagger scans across tickers
        self._stop           = threading.Event()
        self._scans_today    = 0
        self._alerts_today   = 0
        self._trades_today   = 0
        self._errors_today   = 0
        self._last_date      = None
        self._seen_setups    = set()
        self._last_trade_time = None  # PT datetime of last trade entry
        self._last_exit_time  = None  # PT datetime of last trade exit (for cooldown)
        self._entry_pending  = False  # True while an order is being placed

    def start(self):
        thread = threading.Thread(target=self._loop, daemon=True, name=f"scanner-{self.ticker}")
        thread.start()
        log.info(f"[{self.ticker}] Scanner started — active 6:30 AM–1:00 PM PT.")

    def stop(self):
        self._stop.set()

    def _loop(self):
        # Stagger start to avoid all tickers hitting yfinance at once
        if self._scan_offset > 0:
            time.sleep(self._scan_offset)
        while not self._stop.is_set():
            try:
                self._scan()
            except Exception as e:
                log.error(f"[{self.ticker}] Scanner error: {e}", exc_info=True)
            time.sleep(60)

    def _check_windows(self):
        """
        Returns (in_market, in_trade_window, is_weekend).
        in_market       = True if within 6:30 AM – 1:00 PM PT (Mon–Fri)
        in_trade_window = True if within trade window (Mon–Fri)
        is_weekend      = True if Saturday or Sunday
        Scanner runs 24/7 — emails only sent when in_market is True.
        """
        now_pt = datetime.now(PT)

        # Reset daily counters at midnight
        today = now_pt.date()
        if self._last_date != today:
            self._last_date       = today
            self._scans_today     = 0
            self._alerts_today    = 0
            self._trades_today    = 0
            self._errors_today    = 0
            self._seen_setups     = set()
            self._last_trade_time = None
            log.info(f"New trading day: {today}. Counters reset.")

        hour      = now_pt.hour
        minute    = now_pt.minute
        is_weekend = now_pt.weekday() >= 5  # 5=Saturday, 6=Sunday

        in_market = (
            not is_weekend and
            (hour > MARKET_OPEN_PT or (hour == MARKET_OPEN_PT and minute >= MARKET_OPEN_MIN_PT))
            and hour < MARKET_CLOSE_PT
        )

        in_trade_window = (
            not is_weekend and
            (hour > config.TRADE_WINDOW_START_PT or
             (hour == config.TRADE_WINDOW_START_PT and minute >= config.TRADE_WINDOW_START_MIN))
            and hour < config.TRADE_WINDOW_END_PT
        )

        return in_market, in_trade_window, is_weekend

    def _scan(self):
        # Clear pending flag if trade is now tracked, or after 2 min timeout
        if self._entry_pending:
            ticker_in_open = any(
                t.get("ticker") == self.ticker for t in self.exit_manager.open_trades
            )
            if ticker_in_open:
                self._entry_pending = False  # trade confirmed in exit manager
            elif self._last_trade_time:
                # Auto-clear if pending for more than 2 minutes (entry probably failed)
                elapsed = (datetime.now(PT) - self._last_trade_time).total_seconds()
                if elapsed > 120:
                    log.warning(f"[{self.ticker}] Entry pending flag stuck for {elapsed:.0f}s — clearing")
                    self._entry_pending = False

        # Reset seen setups when a trade just closed (transition from in-trade to no-trade)
        ticker_has_trade = any(
            t.get("ticker") == self.ticker for t in self.exit_manager.open_trades
        )
        if getattr(self, '_was_in_trade', False) and not ticker_has_trade:
            self._last_exit_time = datetime.now(PT)
            log.info(f"[{self.ticker}] Trade closed — cooldown {config.COOLDOWN_MINUTES} min before next entry.")
            self._seen_setups = set()
            self._entry_pending = False
        self._was_in_trade = ticker_has_trade

        in_market, in_trade_window, is_weekend = self._check_windows()

        now_pt  = datetime.now(PT)
        now_str = now_pt.strftime('%H:%M')

        if is_weekend:
            mode = "WEEKEND ANALYSIS (no emails)"
        elif not in_market:
            mode = "AFTER HOURS ANALYSIS (no emails)"
        elif in_trade_window:
            mode = "TRADE MODE"
        else:
            mode = "ALERT-ONLY MODE"

        log.info(f"[{self.ticker}] Running ICT scan at {now_str} PT [{mode}]...")

        # ── Update thread status in DB ────────────────────
        self._scans_today += 1
        try:
            from db.writer import update_thread_status
            update_thread_status(
                f"scanner-{self.ticker}", self.ticker, "scanning",
                f"Scanning at {now_str} PT [{mode}]",
                scans_today=self._scans_today,
                trades_today=self._trades_today,
                alerts_today=self._alerts_today,
                error_count=self._errors_today,
            )
        except Exception as e:
            handle_error(f"scanner-{self.ticker}", "update_thread_status", e)

        # ── News filter ───────────────────────────────────
        near_news, news_label = _is_near_news(now_pt)
        if near_news:
            log.info(f"NEWS FILTER: Skipping scan — {news_label} within {config.NEWS_BUFFER_MIN} min.")
            return

        # ── Fetch and aggregate bars (IB real-time) ──────
        if hasattr(self.client, '_submit_to_ib'):
            bars_1m = get_bars_1m_ib(self.client, self.ticker, days_back=5)
        else:
            bars_1m = get_bars_1m(self.ticker, days_back=5)
        if bars_1m.empty:
            log.warning("No data returned. Skipping scan.")
            return

        bars_1h = aggregate(bars_1m, "1h")
        bars_4h = aggregate(bars_1m, "4h")

        if bars_1m.empty or len(bars_1m) < 30:
            log.warning("Not enough bars after aggregation.")
            return

        # Scan last 120 bars (2 hours) for setups
        bars_scan = bars_1m.iloc[-120:]

        # ── Compute significant levels ───────────────────
        levels = get_all_levels(bars_1m, bars_1h, bars_4h)
        if not levels:
            log.warning("No levels computed. Skipping.")
            return

        # ── EMA trend bias ────────────────────────────────
        ema_bias = _get_ema_bias(bars_1h, datetime.now(PT))
        log.info(f"1H EMA bias: {ema_bias}")

        # ── Run ICT long + short strategies ─────────────
        signals_long  = run_strategy(
            bars_scan, bars_1h, bars_4h, levels,
            alerts_today=self._alerts_today
        )
        signals_short = run_strategy_short(
            bars_scan, bars_1h, bars_4h, levels,
            alerts_today=self._alerts_today,
            max_alerts=config.MAX_ALERTS_PER_DAY
        )

        # EMA filter removed — all signals allowed in both directions
        log.info(f"EMA bias: {ema_bias} (informational only — not filtering signals)")
        signals = signals_long + signals_short

        # ── Deduplicate signals by (signal_type, entry_price) ─
        # Keeps only the first signal per unique type+entry combo
        # prevents duplicate emails when same setup fires from
        # slightly different SL levels
        seen_combos = {}
        deduped = []
        for sig in signals:
            key = (sig["signal_type"], round(sig["entry_price"], 2))
            if key not in seen_combos:
                seen_combos[key] = True
                deduped.append(sig)
            else:
                log.info(f"DUPLICATE SIGNAL filtered: {sig['signal_type']} @ ${sig['entry_price']:.2f} — already queued.")
        signals = deduped

        # ── Process signals ──────────────────────────────
        for signal in signals:
            setup_id = signal["setup_id"]
            if setup_id in self._seen_setups:
                continue  # already traded this setup

            signal["ticker"] = self.ticker

            log.info(
                f"{'='*55}\n"
                f"[{self.ticker}] ICT SIGNAL: {signal['signal_type']} [{mode}]\n"
                f"Entry:  ${signal['entry_price']:.2f}\n"
                f"SL:     ${signal['sl']:.2f}\n"
                f"TP:     ${signal['tp']:.2f}\n"
                f"Raided: {signal['raid']['raided_level']} "
                f"@ ${signal['raid']['raided_price']:.2f}\n"
                f"{'='*55}"
            )

            trade = None

            if in_trade_window:
                # ── Check: already in a trade for THIS ticker? ─
                ticker_has_open = any(
                    t.get("ticker") == self.ticker for t in self.exit_manager.open_trades
                )
                if ticker_has_open or self._entry_pending:
                    log.info(f"[{self.ticker}] Already has an open trade — skipping entry.")
                    signal["alert_only"] = True

                else:
                    # ── Check: daily trade limit ──────────────
                    if self._trades_today >= MAX_TRADES_PER_DAY:
                        log.info(f"Max trades per day ({MAX_TRADES_PER_DAY}) reached — skipping entry.")
                        continue

                    # ── Check: cooldown after last trade exit ──
                    if self._last_exit_time is not None:
                        mins_since_exit = (datetime.now(PT) - self._last_exit_time).total_seconds() / 60
                        if mins_since_exit < config.COOLDOWN_MINUTES:
                            remaining = config.COOLDOWN_MINUTES - mins_since_exit
                            log.info(f"[{self.ticker}] Cooldown active — {remaining:.1f} min remaining before next entry.")
                            continue

                    # ── Enter the trade (with timeout) ────────
                    try:
                        import concurrent.futures
                        self._entry_pending = True
                        direction = signal.get("direction", "LONG")
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                            if direction == "SHORT":
                                future = pool.submit(select_and_enter_put, self.client, self.ticker)
                            else:
                                future = pool.submit(select_and_enter, self.client, self.ticker)
                            trade = future.result(timeout=30)  # 30s max for option entry

                        if trade:
                            trade["signal"]    = signal["signal_type"]
                            trade["ict_entry"] = signal["entry_price"]
                            trade["ict_sl"]    = signal["sl"]
                            trade["ict_tp"]    = signal["tp"]

                            # ── Entry-time enrichment ─────────
                            ctx = {"ticker": self.ticker, "symbol": trade.get("symbol")}
                            trade["entry_indicators"] = safe_call(
                                compute_snapshot, bars_1m,
                                component=f"scanner-{self.ticker}", operation="compute_snapshot",
                                default={}, context=ctx)
                            trade["entry_stock_price"] = safe_call(
                                self.client.get_realtime_equity_price, self.ticker,
                                component=f"scanner-{self.ticker}", operation="get_equity_price",
                                default=None, context=ctx)
                            trade["entry_greeks"] = safe_call(
                                self.client.get_option_greeks, trade["symbol"],
                                component=f"scanner-{self.ticker}", operation="get_greeks",
                                default={}, context=ctx)
                            trade["entry_vix"] = safe_call(
                                self.client.get_vix,
                                component=f"scanner-{self.ticker}", operation="get_vix",
                                default=None, context=ctx)

                            self.exit_manager.add_trade(trade)
                            self._seen_setups.add(setup_id)  # only block after actual entry
                            self._trades_today += 1
                            self._last_trade_time = datetime.now(PT)
                            log.info(f"[{self.ticker}] Trade #{self._trades_today}/{MAX_TRADES_PER_DAY} today opened.")
                            # Immediately update DB with new trade count
                            try:
                                from db.writer import update_thread_status
                                update_thread_status(
                                    f"scanner-{self.ticker}", self.ticker, "idle",
                                    f"Trade #{self._trades_today} opened",
                                    scans_today=self._scans_today,
                                    trades_today=self._trades_today,
                                    alerts_today=self._alerts_today,
                                    error_count=self._errors_today,
                                )
                            except Exception as e:
                                handle_error(f"scanner-{self.ticker}", "post_trade_thread_update", e)
                    except concurrent.futures.TimeoutError:
                        # CRITICAL: Order may have been placed on IB but we timed out
                        # waiting for the result. We MUST check if the order filled.
                        log.warning(f"[{self.ticker}] Trade entry timed out (30s) — checking for orphaned IB order...")
                        self._errors_today += 1

                        # Wait a bit more for the future to complete
                        orphan_trade = None
                        try:
                            orphan_trade = future.result(timeout=5)  # Give 5 more seconds
                        except (concurrent.futures.TimeoutError, Exception):
                            pass

                        if orphan_trade:
                            # The order DID fill — we must track it
                            log.warning(f"[{self.ticker}] Timeout recovery: trade completed after timeout! Adopting.")
                            orphan_trade["signal"] = signal.get("signal_type", "UNKNOWN")
                            orphan_trade["ict_entry"] = signal.get("entry_price")
                            orphan_trade["ict_sl"] = signal.get("sl")
                            orphan_trade["ict_tp"] = signal.get("tp")
                            self.exit_manager.add_trade(orphan_trade)
                            self._trades_today += 1
                            self._last_trade_time = datetime.now(PT)
                            log.info(f"[{self.ticker}] Orphaned trade adopted: {orphan_trade['symbol']}")
                            handle_error(f"scanner-{self.ticker}", "trade_entry_timeout_recovered",
                                        TimeoutError("Trade entry timed out but was recovered"),
                                        context={"ticker": self.ticker, "symbol": orphan_trade.get("symbol")})
                        else:
                            # Order might still be pending on IB — check recent fills
                            log.warning(f"[{self.ticker}] Could not recover trade — checking IB for fills...")
                            try:
                                fill = self.client.check_recent_fills(self.ticker)
                                if fill:
                                    log.error(f"[{self.ticker}] FOUND ORPHANED IB FILL: {fill}")
                                    handle_error(f"scanner-{self.ticker}", "orphaned_ib_fill",
                                                RuntimeError(f"Trade filled on IB but not tracked: {fill}"),
                                                context={"ticker": self.ticker, "fill": str(fill)},
                                                critical=True)
                                    # The reconciliation will pick this up on next cycle
                                else:
                                    log.info(f"[{self.ticker}] No IB fills found — order may not have been placed.")
                                    self._entry_pending = False  # Safe to clear since nothing filled
                            except Exception as e3:
                                handle_error(f"scanner-{self.ticker}", "check_orphan_fills", e3,
                                            context={"ticker": self.ticker})

                        handle_error(f"scanner-{self.ticker}", "trade_entry_timeout",
                                    TimeoutError("Trade entry timed out after 30s"),
                                    context={"ticker": self.ticker, "recovered": orphan_trade is not None},
                                    critical=True)
                    except Exception as e:
                        self._entry_pending = False
                        self._errors_today += 1
                        handle_error(f"scanner-{self.ticker}", "trade_entry", e,
                                    context={"ticker": self.ticker}, critical=True)
            else:
                # ── Outside trade window: alert only ─────────
                log.info("Signal outside trade window — sending alert-only email, no trade placed.")
                signal["alert_only"] = True  # flag for emailer to note in subject

            # ── Send email only if a trade was actually opened ───────────
            if trade and in_market:
                send_signal_email(signal, trade)
            else:
                log.info(f"Signal detected (no email — no trade placed): {signal['signal_type']} @ ${signal['entry_price']:.2f}")

        # ── Post-scan: update thread status to idle ───────────
        try:
            from db.writer import update_thread_status
            update_thread_status(
                f"scanner-{self.ticker}", self.ticker, "idle",
                f"Scan #{self._scans_today} done at {now_str} PT | {len(signals) if 'signals' in dir() else 0} signals | Next scan ~{(datetime.now(PT) + __import__('datetime').timedelta(seconds=60)).strftime('%H:%M')} PT",
                scans_today=self._scans_today,
                trades_today=self._trades_today,
                alerts_today=self._alerts_today,
                error_count=self._errors_today,
            )
        except Exception as e:
            handle_error(f"scanner-{self.ticker}", "post_scan_thread_update", e)
