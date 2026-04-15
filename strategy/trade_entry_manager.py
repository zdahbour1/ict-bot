"""
Trade Entry Manager — Orchestrates trade entry decisions and execution.

Handles trade entry gates (limits, cooldowns, position conflicts),
order placement via option_selector, enrichment, and registration
with exit_manager. Includes timeout recovery and orphan detection.
"""
import logging
import concurrent.futures
from datetime import datetime

import pytz
import pandas as pd

from strategy.signal_engine import Signal
from strategy.option_selector import select_and_enter, select_and_enter_put
from strategy.indicators import compute_snapshot
from strategy.error_handler import handle_error, safe_call
import config

log = logging.getLogger(__name__)

PT = pytz.timezone("America/Los_Angeles")

MAX_TRADES_PER_DAY = 8


class TradeEntryManager:
    """
    Manages trade entry decisions and execution for a single ticker.

    Responsibilities:
    - Check entry gates: position conflicts, daily limits, cooldowns
    - Place orders via option_selector (with 30s timeout + recovery)
    - Enrich trades with indicators, Greeks, VIX
    - Register trades with exit_manager
    - Track trade counts and last trade time
    - Update thread status in DB
    """

    def __init__(self, client, exit_manager, ticker: str):
        self.client = client
        self.exit_manager = exit_manager
        self.ticker = ticker
        self._trades_today: int = 0
        self._errors_today: int = 0
        self._last_trade_time: datetime | None = None
        self._last_exit_time: datetime | None = None
        self._entry_pending: bool = False
        self._was_in_trade: bool = False

    def reset_daily(self):
        """Reset daily counters. Called at midnight."""
        self._trades_today = 0
        self._errors_today = 0
        self._last_trade_time = None

    # ── Entry gate checks ─────────────────────────────────

    def check_pending_state(self):
        """Clear stale entry_pending flag if trade is confirmed or timed out."""
        if not self._entry_pending:
            return

        ticker_in_open = any(
            t.get("ticker") == self.ticker for t in self.exit_manager.open_trades
        )
        if ticker_in_open:
            self._entry_pending = False
        elif self._last_trade_time:
            elapsed = (datetime.now(PT) - self._last_trade_time).total_seconds()
            if elapsed > 120:
                log.warning(f"[{self.ticker}] Entry pending flag stuck for {elapsed:.0f}s — clearing")
                self._entry_pending = False

    def check_trade_closed(self):
        """Detect when a trade closes — set cooldown and clear state."""
        ticker_has_trade = any(
            t.get("ticker") == self.ticker for t in self.exit_manager.open_trades
        )
        if self._was_in_trade and not ticker_has_trade:
            self._last_exit_time = datetime.now(PT)
            log.info(f"[{self.ticker}] Trade closed — cooldown {config.COOLDOWN_MINUTES} min before next entry.")
            self._entry_pending = False
        self._was_in_trade = ticker_has_trade

    def can_enter(self) -> tuple[bool, str]:
        """
        Check all trade entry gates.
        Returns (allowed, reason).
        """
        # Already in a trade for this ticker?
        ticker_has_open = any(
            t.get("ticker") == self.ticker for t in self.exit_manager.open_trades
        )
        if ticker_has_open or self._entry_pending:
            return False, "already in trade"

        # Daily trade limit
        if self._trades_today >= MAX_TRADES_PER_DAY:
            return False, f"daily limit ({MAX_TRADES_PER_DAY}) reached"

        # Post-exit cooldown
        if self._last_exit_time is not None:
            mins_since_exit = (datetime.now(PT) - self._last_exit_time).total_seconds() / 60
            if mins_since_exit < config.COOLDOWN_MINUTES:
                remaining = config.COOLDOWN_MINUTES - mins_since_exit
                return False, f"cooldown active ({remaining:.1f}m remaining)"

        return True, "ok"

    # ── Trade execution ───────────────────────────────────

    def enter(self, signal: Signal, bars_1m: pd.DataFrame = None) -> dict | None:
        """
        Attempt to enter a trade for the given signal.

        Returns:
            trade dict if successful, None if entry was blocked or failed.
        """
        allowed, reason = self.can_enter()
        if not allowed:
            log.info(f"[{self.ticker}] Entry blocked: {reason}")
            return None

        self._entry_pending = True
        trade = None

        try:
            trade = self._place_order_with_timeout(signal)

            if trade:
                # Validate IB order confirmation
                if not config.DRY_RUN and not trade.get("ib_order_id") and not trade.get("ib_perm_id"):
                    log.error(f"[{self.ticker}] Trade dict has no IB order/perm ID — "
                              f"refusing to track.")
                    handle_error(f"scanner-{self.ticker}", "trade_no_ib_id",
                                 RuntimeError("Trade returned without IB order identifiers"),
                                 context={"ticker": self.ticker,
                                          "trade_keys": list(trade.keys()),
                                          "status": trade.get("status")},
                                 critical=True)
                    self._entry_pending = False
                    return None

                # Enrich trade with signal info
                trade["signal"] = signal.signal_type
                trade["ict_entry"] = signal.entry_price
                trade["ict_sl"] = signal.sl
                trade["ict_tp"] = signal.tp

                # Entry-time enrichment (Greeks, VIX, indicators)
                self._enrich_trade(trade, bars_1m)

                # Register with exit manager (writes to DB)
                self.exit_manager.add_trade(trade)
                self._trades_today += 1
                self._last_trade_time = datetime.now(PT)
                log.info(f"[{self.ticker}] Trade #{self._trades_today}/{MAX_TRADES_PER_DAY} opened: {trade.get('symbol')}")

                # Update thread status
                self._update_thread_status(f"Trade #{self._trades_today} opened")

            return trade

        except concurrent.futures.TimeoutError:
            self._handle_timeout(signal)
            return None

        except Exception as e:
            self._entry_pending = False
            self._errors_today += 1
            handle_error(f"scanner-{self.ticker}", "trade_entry", e,
                         context={"ticker": self.ticker}, critical=True)
            return None

    def _place_order_with_timeout(self, signal: Signal) -> dict | None:
        """Place order via thread pool with 30s timeout + 5s recovery.

        The ThreadPoolExecutor stays alive during the entire recovery window
        so the future can still deliver a result after the initial timeout.
        """
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            if signal.direction == "SHORT":
                future = pool.submit(select_and_enter_put, self.client, self.ticker)
            else:
                future = pool.submit(select_and_enter, self.client, self.ticker)

            try:
                return future.result(timeout=30)
            except concurrent.futures.TimeoutError:
                # Order may have been placed on IB but we timed out waiting.
                # Give 5 more seconds before giving up.
                log.warning(f"[{self.ticker}] Trade entry timed out (30s) — "
                            f"waiting 5 more seconds for recovery...")
                self._errors_today += 1

                orphan_trade = None
                try:
                    orphan_trade = future.result(timeout=5)
                except (concurrent.futures.TimeoutError, Exception):
                    pass

                if orphan_trade:
                    # Trade DID complete — recovered!
                    log.warning(f"[{self.ticker}] Timeout recovery: trade completed! Adopting.")
                    handle_error(f"scanner-{self.ticker}", "trade_entry_timeout_recovered",
                                 TimeoutError("Trade entry timed out but was recovered"),
                                 context={"ticker": self.ticker,
                                          "symbol": orphan_trade.get("symbol")})
                    return orphan_trade
                else:
                    # Could not recover — check IB for orphaned fills
                    self._check_orphaned_fills()
                    handle_error(f"scanner-{self.ticker}", "trade_entry_timeout",
                                 TimeoutError("Trade entry timed out after 35s"),
                                 context={"ticker": self.ticker, "recovered": False},
                                 critical=True)
                    raise concurrent.futures.TimeoutError()
        finally:
            pool.shutdown(wait=False)

    def _check_orphaned_fills(self):
        """Check IB for fills that occurred but weren't tracked."""
        try:
            fill = self.client.check_recent_fills(self.ticker)
            if fill:
                log.error(f"[{self.ticker}] FOUND ORPHANED IB FILL: {fill}")
                handle_error(f"scanner-{self.ticker}", "orphaned_ib_fill",
                             RuntimeError(f"Trade filled on IB but not tracked: {fill}"),
                             context={"ticker": self.ticker, "fill": str(fill)},
                             critical=True)
                # Reconciliation will pick this up on next cycle
            else:
                log.info(f"[{self.ticker}] No IB fills found — order may not have been placed.")
                self._entry_pending = False
        except Exception as e:
            handle_error(f"scanner-{self.ticker}", "check_orphan_fills", e,
                         context={"ticker": self.ticker})

    def _enrich_trade(self, trade: dict, bars_1m: pd.DataFrame = None):
        """Add entry-time enrichment data (indicators, Greeks, VIX)."""
        ctx = {"ticker": self.ticker, "symbol": trade.get("symbol")}

        if bars_1m is not None:
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

    def _update_thread_status(self, message: str):
        """Update thread_status in DB."""
        try:
            from db.writer import update_thread_status
            update_thread_status(
                f"scanner-{self.ticker}", self.ticker, "idle",
                message,
                scans_today=0,  # scanner manages scan count
                trades_today=self._trades_today,
                alerts_today=0,
                error_count=self._errors_today,
            )
        except Exception as e:
            handle_error(f"scanner-{self.ticker}", "post_trade_thread_update", e)

    # ── Properties ────────────────────────────────────────

    @property
    def trades_today(self) -> int:
        return self._trades_today

    @property
    def errors_today(self) -> int:
        return self._errors_today

    @property
    def entry_pending(self) -> bool:
        return self._entry_pending
