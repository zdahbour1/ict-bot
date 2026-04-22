"""
Live Scanner — runs every minute during market hours (6:30 AM - 1:00 PM PT).
- Inside 07:00–09:00 PT  → signal email + trade entry
- Outside that window    → signal email only (alert, no trade placed)

Architecture: Scanner is a thin orchestrator that delegates to:
  - SignalEngine: pure signal detection (no side effects)
  - TradeEntryManager: trade entry decisions and execution
"""
import logging
import threading
import time
from datetime import datetime, date
import pytz
import pandas as pd

from data.provider import get_bars_1m
from data.ib_provider import get_bars_1m_ib
from data.aggregator import aggregate
from strategy.levels import get_all_levels
from strategy.signal_engine import SignalEngine
from strategy.trade_entry_manager import TradeEntryManager
from strategy.error_handler import handle_error
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
    ema = bars_1h["close"].ewm(span=config.EMA_PERIOD_1H, adjust=False).mean()
    last_close = bars_1h["close"].iloc[-1]
    last_ema = ema.iloc[-1]
    if last_close > last_ema:
        return "BULLISH"
    elif last_close < last_ema:
        return "BEARISH"
    return "NEUTRAL"


class Scanner:
    """
    Thin orchestrator: fetch data → detect signals → attempt trades.

    Delegates signal detection to SignalEngine (pure, no side effects)
    and trade entry to TradeEntryManager (handles limits, cooldowns, IB).
    """

    def __init__(self, client, exit_manager, ticker=None, scan_offset=0,
                 strategy_id: int = 1, strategy_name: str = "ict",
                 strategy_instance=None):
        self.client = client
        self.exit_manager = exit_manager
        self.ticker = ticker or config.TICKER
        self._scan_offset = scan_offset

        # Multi-strategy awareness (Phase 4). ICT keeps the fast path —
        # SignalEngine directly — and every other strategy goes through
        # the BaseStrategy plugin interface (returns list[Signal]).
        self.strategy_id = strategy_id
        self.strategy_name = strategy_name
        if strategy_name == "ict" or strategy_instance is None:
            self.signal_engine = SignalEngine(self.ticker)
            self.plugin = None
        else:
            self.signal_engine = None
            self.plugin = strategy_instance

        self.trade_manager = TradeEntryManager(
            client, exit_manager, self.ticker,
            strategy_id=strategy_id, strategy_name=strategy_name,
        )

        self._stop = threading.Event()
        self._scans_today = 0
        self._last_date = None

    def _thread_key(self) -> str:
        """Unique scanner thread_status key per (strategy, ticker).

        ICT keeps the legacy key ``scanner-<TICKER>`` so dashboards and
        existing log filters remain unchanged. Every other strategy gets
        ``scanner-<strategy_name>-<TICKER>`` so concurrent strategies on
        the same ticker each show their own row.
        """
        if self.strategy_name == "ict":
            return f"scanner-{self.ticker}"
        return f"scanner-{self.strategy_name}-{self.ticker}"

    def _alerts_today(self) -> int:
        """Unified alert counter across SignalEngine (ICT) and plugins."""
        if self.signal_engine is not None:
            return self.signal_engine.alerts_today
        if self.plugin is not None:
            return int(getattr(self.plugin, "alerts_today", 0) or 0)
        return 0

    def start(self):
        thread = threading.Thread(target=self._loop, daemon=True, name=self._thread_key())
        thread.start()
        log.info(f"[{self.ticker}] Scanner started (strategy={self.strategy_name}) — active 6:30 AM–1:00 PM PT.")

    def stop(self):
        self._stop.set()

    def _loop(self):
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
        """
        now_pt = datetime.now(PT)

        # Reset daily counters at midnight
        today = now_pt.date()
        if self._last_date != today:
            self._last_date = today
            self._scans_today = 0
            if self.signal_engine is not None:
                self.signal_engine.reset_daily()
            elif self.plugin is not None:
                try:
                    self.plugin.reset_daily()
                except Exception:
                    pass
            self.trade_manager.reset_daily()
            log.info(f"New trading day: {today}. Counters reset.")

        hour = now_pt.hour
        minute = now_pt.minute
        is_weekend = now_pt.weekday() >= 5

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
        # ── Housekeeping: check pending state and trade closures ──
        self.trade_manager.check_pending_state()

        # Detect trade closures → set cooldown, clear signal engine setups
        was_in_trade = self.trade_manager._was_in_trade
        self.trade_manager.check_trade_closed()
        if was_in_trade and not self.trade_manager._was_in_trade:
            if self.signal_engine is not None:
                self.signal_engine.clear_seen_setups()

        in_market, in_trade_window, is_weekend = self._check_windows()

        now_pt = datetime.now(PT)
        now_str = now_pt.strftime('%H:%M')

        if is_weekend:
            mode = "WEEKEND ANALYSIS (no emails)"
        elif not in_market:
            mode = "AFTER HOURS ANALYSIS (no emails)"
        elif in_trade_window:
            mode = "TRADE MODE"
        else:
            mode = "ALERT-ONLY MODE"

        # Keep literal "Running ICT scan" for ICT (log-line compatibility).
        if self.strategy_name == "ict":
            log.info(f"[{self.ticker}] Running ICT scan at {now_str} PT [{mode}]...")
        else:
            log.info(f"[{self.ticker}] Running {self.strategy_name.upper()} scan at {now_str} PT [{mode}]...")

        # ── Update thread status in DB ────────────────────
        self._scans_today += 1
        try:
            from db.writer import update_thread_status
            update_thread_status(
                self._thread_key(), self.ticker, "scanning",
                f"Scanning at {now_str} PT [{mode}]",
                scans_today=self._scans_today,
                trades_today=self.trade_manager.trades_today,
                alerts_today=self._alerts_today(),
                error_count=self.trade_manager.errors_today,
            )
        except Exception as e:
            handle_error(self._thread_key(), "update_thread_status", e)

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

        # ── Compute significant levels ───────────────────
        levels = get_all_levels(bars_1m, bars_1h, bars_4h)
        if not levels:
            log.warning("No levels computed. Skipping.")
            return

        # ── EMA trend bias (informational) ────────────────
        ema_bias = _get_ema_bias(bars_1h, now_pt)
        log.info(f"EMA bias: {ema_bias} (informational only — not filtering signals)")

        # ── Detect signals (pure, no side effects) ────────
        if self.plugin is not None:
            # Plugin path — BaseStrategy.detect(..., ticker=...) → list[Signal]
            try:
                signals = self.plugin.detect(bars_1m, bars_1h, bars_4h, levels, self.ticker)
            except TypeError:
                # Older plugins may not accept ticker kwarg
                signals = self.plugin.detect(bars_1m, bars_1h, bars_4h, levels)
        else:
            signals = self.signal_engine.detect(bars_1m, bars_1h, bars_4h, levels)

        # ── Process signals ──────────────────────────────
        for signal in signals:
            log.info(
                f"{'='*55}\n"
                f"[{self.ticker}] ICT SIGNAL: {signal.signal_type} [{mode}]\n"
                f"Entry:  ${signal.entry_price:.2f}\n"
                f"SL:     ${signal.sl:.2f}\n"
                f"TP:     ${signal.tp:.2f}\n"
                f"Raided: {signal.details.get('raid', {}).get('raided_level', 'N/A')} "
                f"@ ${signal.details.get('raid', {}).get('raided_price', 0):.2f}\n"
                f"{'='*55}"
            )

            trade = None

            if in_trade_window:
                # Attempt trade entry via TradeEntryManager
                trade = self.trade_manager.enter(signal, bars_1m=bars_1m)
                if trade:
                    if self.signal_engine is not None:
                        self.signal_engine.mark_used(signal.setup_id)
                    elif self.plugin is not None:
                        try:
                            self.plugin.mark_used(signal.setup_id)
                        except Exception:
                            pass
            else:
                log.info("Signal outside trade window — alert only, no trade placed.")

            # ── Send email if a trade was opened ─────────
            if trade and in_market:
                # Convert Signal back to raw dict for emailer compatibility
                raw_signal = signal.details.get("_raw", {})
                raw_signal["ticker"] = self.ticker
                send_signal_email(raw_signal, trade)
            elif signals:
                log.info(f"Signal detected (no email — no trade placed): "
                         f"{signal.signal_type} @ ${signal.entry_price:.2f}")

        # ── Post-scan: update thread status to idle ───────
        try:
            from db.writer import update_thread_status
            next_scan = (datetime.now(PT) + __import__('datetime').timedelta(seconds=60)).strftime('%H:%M')
            update_thread_status(
                self._thread_key(), self.ticker, "idle",
                f"Scan #{self._scans_today} done at {now_str} PT | {len(signals)} signals | Next ~{next_scan} PT",
                scans_today=self._scans_today,
                trades_today=self.trade_manager.trades_today,
                alerts_today=self._alerts_today(),
                error_count=self.trade_manager.errors_today,
            )
        except Exception as e:
            handle_error(self._thread_key(), "post_scan_thread_update", e)
