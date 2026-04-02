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
from data.aggregator import aggregate
from strategy.levels import get_all_levels
from strategy.ict_long import run_strategy
from strategy.ict_short import run_strategy_short
from strategy.option_selector import select_and_enter, select_and_enter_put
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
MIN_MINUTES_BETWEEN = 15   # minimum minutes between trades

class Scanner:
    def __init__(self, client, exit_manager):
        self.client          = client
        self.exit_manager    = exit_manager
        self._stop           = threading.Event()
        self._alerts_today   = 0
        self._trades_today   = 0
        self._last_date      = None
        self._seen_setups    = set()
        self._last_trade_time = None  # PT datetime of last trade entry

    def start(self):
        thread = threading.Thread(target=self._loop, daemon=True)
        thread.start()
        log.info("Scanner started — active 6:30 AM–1:00 PM PT (trades only 07:00–09:00 PT).")

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._scan()
            except Exception as e:
                log.error(f"Scanner error: {e}", exc_info=True)
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
            self._alerts_today    = 0
            self._trades_today    = 0
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
        # Reset seen setups when a trade just closed (transition from in-trade to no-trade)
        now_in_trade = len(self.exit_manager.open_trades) > 0
        if getattr(self, '_was_in_trade', False) and not now_in_trade:
            log.info("Trade closed — resetting seen setups for fresh signals.")
            self._seen_setups = set()
        self._was_in_trade = now_in_trade

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

        log.info(f"Running ICT scan at {now_str} PT [{mode}]...")

        # ── News filter ───────────────────────────────────
        near_news, news_label = _is_near_news(now_pt)
        if near_news:
            log.info(f"NEWS FILTER: Skipping scan — {news_label} within {config.NEWS_BUFFER_MIN} min.")
            return

        # ── Fetch and aggregate bars ─────────────────────
        bars_1m = get_bars_1m(config.TICKER, days_back=5)
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

            log.info(
                f"{'='*55}\n"
                f"ICT SIGNAL: {signal['signal_type']} [{mode}]\n"
                f"Entry:  ${signal['entry_price']:.2f}\n"
                f"SL:     ${signal['sl']:.2f}\n"
                f"TP:     ${signal['tp']:.2f}\n"
                f"Raided: {signal['raid']['raided_level']} "
                f"@ ${signal['raid']['raided_price']:.2f}\n"
                f"{'='*55}"
            )

            trade = None

            if in_trade_window:
                # ── Check: already in a trade? ────────────────
                if len(self.exit_manager.open_trades) > 0:
                    log.info("Already in an open trade — skipping entry, sending alert-only email.")
                    signal["alert_only"] = True

                else:
                    # ── Check: daily trade limit ──────────────
                    if self._trades_today >= MAX_TRADES_PER_DAY:
                        log.info(f"Max trades per day ({MAX_TRADES_PER_DAY}) reached — skipping entry.")
                        continue

                    # ── Check: 15 min gap between trades ─────
                    if self._last_trade_time is not None:
                        mins_since = (datetime.now(PT) - self._last_trade_time).total_seconds() / 60
                        if mins_since < MIN_MINUTES_BETWEEN:
                            log.info(f"Too soon since last trade ({mins_since:.1f} min) — waiting {MIN_MINUTES_BETWEEN} min gap.")
                            continue

                    # ── Enter the trade ───────────────────────
                    try:
                        direction = signal.get("direction", "LONG")
                        if direction == "SHORT":
                            trade = select_and_enter_put(self.client)
                        else:
                            trade = select_and_enter(self.client)

                        if trade:
                            trade["signal"]    = signal["signal_type"]
                            trade["ict_entry"] = signal["entry_price"]
                            trade["ict_sl"]    = signal["sl"]
                            trade["ict_tp"]    = signal["tp"]
                            self.exit_manager.add_trade(trade)
                            self._seen_setups.add(setup_id)  # only block after actual entry
                            self._trades_today += 1
                            self._last_trade_time = datetime.now(PT)
                            log.info(f"Trade #{self._trades_today}/{MAX_TRADES_PER_DAY} today opened.")
                    except Exception as e:
                        log.error(f"Trade entry failed: {e}", exc_info=True)
            else:
                # ── Outside trade window: alert only ─────────
                log.info("Signal outside trade window — sending alert-only email, no trade placed.")
                signal["alert_only"] = True  # flag for emailer to note in subject

            # ── Send email only if a trade was actually opened ───────────
            if trade and in_market:
                send_signal_email(signal, trade)
            else:
                log.info(f"Signal detected (no email — no trade placed): {signal['signal_type']} @ ${signal['entry_price']:.2f}")
