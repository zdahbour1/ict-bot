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


# ── Shared entry-manager thread status ───────────────────────────
# All per-ticker TradeEntryManager instances share a single
# "entry-manager" row in thread_status so the Threads page shows a
# live feed of entry activity across all tickers, not just the
# per-scanner rows (which get overwritten by post-scan idle messages).
def _update_entry_thread(stage: str, ticker: str, detail: str = "") -> None:
    """Update the shared 'entry-manager' thread_status row AND write a
    system_log entry tagged ``entry-manager`` so the Threads page log
    filter surfaces it alongside reconcile/exit events.

    Thread-safe because update_thread_status uses an UPSERT on thread_name.
    Silent on failure — thread status is observational, must not break
    the trade flow.
    """
    msg = f"{stage.upper()}: {ticker}"
    if detail:
        msg += f" — {detail}"
    try:
        from db.writer import update_thread_status, add_system_log
        # Status row — one per stage, overwrites previous
        update_thread_status("entry-manager", None, stage, msg)
        # System_log — append-only trail. Use level by stage severity.
        level = ("error" if stage == "failed"
                 else "warn" if stage == "blocked"
                 else "info")
        add_system_log("entry-manager", level, msg)
    except Exception:
        pass


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

    def __init__(self, client, exit_manager, ticker: str,
                 strategy_id: int = 1, strategy_name: str = "ict",
                 plugin_instance=None):
        self.client = client
        self.exit_manager = exit_manager
        self.ticker = ticker
        # Phase 4: per-strategy scoping. The open-trade lock below is
        # keyed by (strategy_id, ticker) so Strategy A's SPY position
        # does NOT block Strategy B from entering SPY.
        self.strategy_id = strategy_id
        self.strategy_name = strategy_name
        # Phase 6 (multi-strategy v2): optional plugin reference so
        # multi-leg strategies (iron condor, spreads) can override
        # ``place_legs(signal)`` and route through the multi-leg path.
        # None = classic single-leg flow preserved exactly.
        self.plugin_instance = plugin_instance
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
            t.get("ticker") == self.ticker
            and (t.get("strategy_id") in (None, self.strategy_id))
            for t in self.exit_manager.open_trades
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
            t.get("ticker") == self.ticker
            and (t.get("strategy_id") in (None, self.strategy_id))
            for t in self.exit_manager.open_trades
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
        # EOD gate (before everything else — cheap, and blocks entries
        # in the last N minutes before market close so a fresh bracket
        # doesn't get torn down 30 seconds later by the EOD sweep).
        # See strategy/market_hours.py and docs/market_hours_guards.md.
        try:
            from strategy.market_hours import get_market_clock
            clock = get_market_clock()
            if not clock.entries_allowed():
                if clock.is_past_close():
                    return False, "market closed (past EOD cutoff)"
                if clock.in_eod_sweep_window():
                    return (False,
                            f"EOD sweep window ({clock.minutes_until_close():.0f}m "
                            f"to close) — no new entries")
                # Before TRADE_WINDOW_START
                return False, "before trading window"
        except Exception:
            # Clock failure must not gate trades open — fall through
            # to the other checks. Monitoring will catch any bad state.
            pass

        # Already in a trade for this (strategy, ticker)?
        # Per-strategy lock (Phase 4): Strategy A's SPY open does NOT
        # block Strategy B from entering SPY. Backed by the
        # idx_trades_open_per_strategy_ticker partial unique index.
        ticker_has_open = any(
            t.get("ticker") == self.ticker
            and (t.get("strategy_id") in (None, self.strategy_id))
            for t in self.exit_manager.open_trades
        )
        if ticker_has_open or self._entry_pending:
            return False, "already in trade"

        # ENH-037: cross-strategy exposure cap per underlying.
        # With 4 strategies running concurrently, all could pile into
        # SPY simultaneously — unbounded concentration. Cap the total
        # open trades (any strategy) on the same ticker at
        # MAX_CONCURRENT_PER_UNDERLYING (default 2, configurable).
        try:
            from db.settings_cache import get_int
            cap = get_int("MAX_CONCURRENT_PER_UNDERLYING", default=2)
        except Exception:
            cap = 2
        if cap > 0:
            same_underlying = sum(
                1 for t in self.exit_manager.open_trades
                if t.get("ticker") == self.ticker
            )
            if same_underlying >= cap:
                return False, (
                    f"cross-strategy cap hit ({same_underlying}/{cap} "
                    f"open on {self.ticker})"
                )

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

    def _ib_preflight_check(self) -> tuple[bool, str]:
        """
        Check IB directly for existing positions or open orders for this ticker.
        This is a SAFETY NET — even if DB shows no open trades, IB might have
        positions or bracket orders from a prior session, crash recovery, etc.

        Returns (clear, reason). If not clear, caller should reconcile and retry.
        """
        try:
            # Check 1: Any positions on IB for this ticker?
            positions = self.client.get_ib_positions_raw()
            for p in positions:
                if p.get("ticker") == self.ticker and p.get("qty", 0) != 0:
                    qty = p.get("qty", 0)
                    con_id = p.get("conId")
                    symbol = p.get("symbol", "")
                    log.warning(f"[{self.ticker}] IB PRE-FLIGHT: Found existing position! "
                                f"qty={qty} conId={con_id} symbol={symbol}")
                    handle_error(f"trade_entry-{self.ticker}", "ib_preflight_position_exists",
                                 RuntimeError(f"Position exists on IB: qty={qty} conId={con_id}"),
                                 context={"ticker": self.ticker, "qty": qty,
                                          "conId": con_id, "symbol": symbol})
                    return False, f"IB position exists: qty={qty} conId={con_id}"

            # Check 2: Any open orders on IB for this ticker?
            open_orders = self.client.find_open_orders_for_contract(None, "")
            ticker_orders = []
            for o in open_orders:
                # Match by ticker symbol in the contract
                order_symbol = o.get("symbol", "")
                if self.ticker in order_symbol or (o.get("conId") and
                        any(p.get("ticker") == self.ticker and p.get("conId") == o["conId"]
                            for p in positions)):
                    ticker_orders.append(o)

            if ticker_orders:
                log.warning(f"[{self.ticker}] IB PRE-FLIGHT: Found {len(ticker_orders)} open order(s)! "
                            f"{[f'orderId={o['orderId']} type={o['orderType']} status={o['status']}' for o in ticker_orders]}")
                handle_error(f"trade_entry-{self.ticker}", "ib_preflight_orders_exist",
                             RuntimeError(f"{len(ticker_orders)} open orders on IB for {self.ticker}"),
                             context={"ticker": self.ticker, "orders": ticker_orders})
                return False, f"{len(ticker_orders)} open orders on IB"

            return True, "ok"

        except Exception as e:
            # If we can't check IB, don't block the trade — log and proceed
            log.warning(f"[{self.ticker}] IB pre-flight check failed: {e} — proceeding")
            return True, "ok (preflight failed, proceeding)"

    # ── Trade execution ───────────────────────────────────

    def enter(self, signal: Signal, bars_1m: pd.DataFrame = None) -> dict | None:
        """
        Attempt to enter a trade for the given signal.

        Pre-flight checks:
        1. can_enter() — DB check (open trades, limits, cooldown)
        2. _ib_preflight_check() — IB check (positions, open orders)
        Only if BOTH pass do we place the order.
        """
        # Top-line log so the order flow is easy to follow in bot.log:
        # "signal found → pre-flight → place order → bracket back".
        log.info(
            f"[{self.ticker}] SIGNAL→ORDER: {signal.signal_type} "
            f"entry=${signal.entry_price:.2f} sl=${signal.sl:.2f} tp=${signal.tp:.2f} "
            f"— running pre-flight"
        )
        _update_entry_thread("preflight", self.ticker,
                             f"{signal.signal_type} @ ${signal.entry_price:.2f}")

        allowed, reason = self.can_enter()
        if not allowed:
            log.info(f"[{self.ticker}] Entry blocked: {reason}")
            _update_entry_thread("blocked", self.ticker, reason)
            return None

        # IB pre-flight: check IB directly for existing positions/orders
        ib_clear, ib_reason = self._ib_preflight_check()
        if not ib_clear:
            log.warning(f"[{self.ticker}] Entry blocked by IB pre-flight: {ib_reason}")
            _update_entry_thread("blocked", self.ticker, f"IB pre-flight: {ib_reason}")
            # Trigger reconciliation to sync DB with IB reality
            try:
                from strategy.reconciliation import periodic_reconciliation
                log.info(f"[{self.ticker}] Triggering reconciliation due to IB/DB mismatch...")
                periodic_reconciliation(self.client, self.exit_manager)
                self.exit_manager.invalidate_cache()
            except Exception as e:
                log.warning(f"[{self.ticker}] Reconciliation failed: {e}")
            return None

        # Phase 6 multi-strategy v2: if the plugin declares a multi-leg
        # entry via place_legs(), route through the multi-leg path and
        # skip the single-leg option_selector flow.
        legs = self._collect_plugin_legs(signal)
        if legs:
            return self._enter_multi_leg(signal, legs)

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
                # Phase 4: stamp strategy_id so insert_trade() persists
                # it correctly and the (strategy_id, ticker) unique
                # index accepts concurrent strategies on same ticker.
                trade["strategy_id"] = self.strategy_id

                # Entry-time enrichment (Greeks, VIX, indicators)
                self._enrich_trade(trade, bars_1m)

                # Register with exit manager (writes to DB)
                self.exit_manager.add_trade(trade)
                self._trades_today += 1
                self._last_trade_time = datetime.now(PT)
                log.info(f"[{self.ticker}] Trade #{self._trades_today}/{MAX_TRADES_PER_DAY} opened: {trade.get('symbol')}")

                # Audit trail — which thread opened this trade, with
                # bracket IDs so the full story is reconstructable
                from strategy.audit import log_trade_action
                log_trade_action(
                    trade.get("db_id"), "open", f"scanner-{self.ticker}",
                    f"opened {trade.get('symbol')} @ "
                    f"${trade.get('entry_price', 0):.2f} "
                    f"signal={signal.signal_type}",
                    extra={
                        "ticker": self.ticker,
                        "symbol": trade.get("symbol"),
                        "direction": trade.get("direction"),
                        "contracts": trade.get("contracts"),
                        "entry_price": trade.get("entry_price"),
                        "ib_order_id": trade.get("ib_order_id"),
                        "ib_perm_id": trade.get("ib_perm_id"),
                        "ib_tp_order_id": trade.get("ib_tp_order_id"),
                        "ib_sl_order_id": trade.get("ib_sl_order_id"),
                        "signal_type": signal.signal_type,
                        "signal_entry": signal.entry_price,
                        "signal_sl": signal.sl,
                        "signal_tp": signal.tp,
                    },
                )

                # Update thread status (both the scanner row and the
                # shared entry-manager row)
                self._update_thread_status(f"Trade #{self._trades_today} opened")
                _update_entry_thread("filled", self.ticker,
                                     f"{trade.get('symbol', '?')} @ "
                                     f"${trade.get('entry_price', 0):.2f}")

            return trade

        except concurrent.futures.TimeoutError:
            self._handle_timeout(signal)
            _update_entry_thread("failed", self.ticker, "timeout (see bot.log)")
            return None

        except Exception as e:
            self._entry_pending = False
            self._errors_today += 1
            _update_entry_thread("failed", self.ticker, f"exception: {type(e).__name__}")
            handle_error(f"scanner-{self.ticker}", "trade_entry", e,
                         context={"ticker": self.ticker}, critical=True)
            return None

    # ── Multi-leg entry (Phase 6) ─────────────────────────
    def _collect_plugin_legs(self, signal: Signal):
        """Ask the plugin for a multi-leg spec. Returns list[dict] of
        leg dicts ready for ``place_multi_leg_order`` or None/[] if the
        plugin is single-leg."""
        if self.plugin_instance is None:
            return None
        place_legs = getattr(self.plugin_instance, "place_legs", None)
        if place_legs is None:
            return None
        try:
            leg_specs = place_legs(signal)
        except Exception as e:
            log.warning(f"[{self.ticker}] plugin.place_legs raised: {e} — "
                        f"falling back to single-leg path")
            return None
        if not leg_specs:
            return None
        # Accept LegSpec dataclass or plain dict
        out = []
        for ls in leg_specs:
            if hasattr(ls, "__dict__"):
                out.append({k: v for k, v in ls.__dict__.items()
                            if not k.startswith("_")})
            elif isinstance(ls, dict):
                out.append(dict(ls))
            else:
                log.warning(f"[{self.ticker}] place_legs returned unknown "
                            f"type {type(ls).__name__} — skipping")
        return out

    def _enter_multi_leg(self, signal: Signal, legs: list[dict]) -> dict | None:
        """Place N legs in one OCA group + write envelope+legs atomically.

        Phase 6 scope: entry only. Exit-across-legs is a follow-up.
        """
        try:
            from db.trade_ref import generate_trade_ref
            order_ref = generate_trade_ref(
                self.ticker, strategy_name=self.strategy_name)
        except Exception as e:
            log.warning(f"[{self.ticker}] trade_ref generation failed: {e} — "
                        f"proceeding untagged")
            order_ref = None

        log.info(f"[{self.ticker}] MULTILEG ENTRY: {len(legs)} legs "
                 f"ref={order_ref} strategy={self.strategy_name}")
        _update_entry_thread("placing", self.ticker,
                             f"multi-leg x{len(legs)} ref={order_ref or '—'}")

        # ENH-046: opt into IB BAG/combo orders for defined-risk spreads.
        # Read the toggle from the DB settings table so it can be flipped
        # at runtime from the dashboard without a bot restart. Falls
        # back to the ``config`` default when the DB row is absent.
        try:
            from db.settings_cache import get_bool
            use_combo = get_bool(
                "USE_COMBO_ORDERS_FOR_MULTI_LEG",
                default=bool(getattr(config,
                                      "USE_COMBO_ORDERS_FOR_MULTI_LEG",
                                      False)),
            )
        except Exception:
            use_combo = bool(getattr(config,
                                      "USE_COMBO_ORDERS_FOR_MULTI_LEG",
                                      False))
        self._entry_pending = True
        try:
            if use_combo and hasattr(self.client, "place_combo_order"):
                result = self.client.place_combo_order(
                    legs, order_ref=order_ref,
                    action="BUY",        # IB treats spreads as BUY-the-combo
                    limit_price=None,     # MKT for now; ENH-046-v2 = net-price
                )
            else:
                result = self.client.place_multi_leg_order(
                    legs, order_ref=order_ref)
        except Exception as e:
            self._entry_pending = False
            self._errors_today += 1
            handle_error(f"scanner-{self.ticker}", "multi_leg_entry", e,
                         context={"ticker": self.ticker,
                                  "n_legs": len(legs),
                                  "combo": use_combo},
                         critical=True)
            _update_entry_thread("failed", self.ticker,
                                 f"multi-leg exception: {type(e).__name__}")
            return None

        if not result:
            self._entry_pending = False
            return None

        trade_envelope = {
            "strategy_id": self.strategy_id,
            "ticker": self.ticker,
            "signal_type": signal.signal_type,
            "client_trade_id": order_ref,
            "n_legs": len(legs),
            "ib_client_id": result.get("ib_client_id"),
        }

        try:
            from db.writer import insert_multi_leg_trade
            import config as _config
            account = getattr(_config, "IB_ACCOUNT", "paper") or "paper"
            trade_id = insert_multi_leg_trade(trade_envelope, result,
                                               account=account)
        except Exception as e:
            handle_error(f"scanner-{self.ticker}", "multi_leg_db_insert", e,
                         context={"ticker": self.ticker,
                                  "oca_group": result.get("oca_group")},
                         critical=True)
            trade_id = None

        self._trades_today += 1
        self._last_trade_time = datetime.now(PT)
        self._entry_pending = False

        trade_dict = {
            "db_id": trade_id,
            "ticker": self.ticker,
            "strategy_id": self.strategy_id,
            "signal": signal.signal_type,
            "client_trade_id": order_ref,
            "n_legs": len(legs),
            "oca_group": result.get("oca_group"),
            "legs": result.get("legs", []),
            "ib_client_id": result.get("ib_client_id"),
        }
        _update_entry_thread(
            "filled" if result.get("all_filled") else "partial",
            self.ticker,
            f"multi-leg x{len(legs)} fills={result.get('fills_received', 0)}",
        )
        log.info(f"[{self.ticker}] MULTILEG #{self._trades_today} opened: "
                 f"trade_id={trade_id} fills="
                 f"{result.get('fills_received', 0)}/{len(legs)} "
                 f"oca={result.get('oca_group')}")
        return trade_dict

    def _place_order_with_timeout(self, signal: Signal) -> dict | None:
        """Place order via thread pool with 30s timeout + 5s recovery.

        The ThreadPoolExecutor stays alive during the entire recovery window
        so the future can still deliver a result after the initial timeout.

        Timeout is 60s (not 30s) because with 17+ tickers placing orders
        simultaneously, the IB worker queue can back up significantly.
        """
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            leg = "PUT" if signal.direction == "SHORT" else "CALL"

            # Human-readable correlation ID — tagged on every bracket
            # leg's orderRef + stored in trades.client_trade_id.
            # Format: TICKER-YYMMDD-NN (e.g. INTC-260421-01).
            # See docs/ib_db_correlation.md.
            try:
                from db.trade_ref import generate_trade_ref
                # Pass strategy_name so the prefix matches THIS strategy
                # (was defaulting to ACTIVE_STRATEGY → every non-ICT trade
                # got 'ict-' prefix, causing UNIQUE collisions and silent
                # entry failures — 2026-04-23 bug).
                order_ref = generate_trade_ref(
                    self.ticker, strategy_name=self.strategy_name)
            except Exception as e:
                # Never block an entry on correlation-ID generation;
                # fall back to untagged orders.
                log.warning(f"[{self.ticker}] trade_ref generation failed: {e} — proceeding untagged")
                order_ref = None

            log.info(
                f"[{self.ticker}] PLACING ORDER: signal={signal.signal_type} "
                f"leg={leg} ref={order_ref or '—'} "
                f"(entry=${signal.entry_price:.2f} "
                f"sl=${signal.sl:.2f} tp=${signal.tp:.2f})"
            )
            _update_entry_thread("placing", self.ticker,
                                 f"{signal.signal_type} {leg} "
                                 f"@ ${signal.entry_price:.2f} "
                                 f"ref={order_ref or '—'}")
            # Pass strategy_id so the selector can look up ticker sec_type
            # (ENH-034: FOP branch). select_* tolerate None for backward compat.
            if signal.direction == "SHORT":
                future = pool.submit(select_and_enter_put, self.client,
                                      self.ticker, order_ref, self.strategy_id)
            else:
                future = pool.submit(select_and_enter, self.client,
                                      self.ticker, order_ref, self.strategy_id)

            try:
                return future.result(timeout=60)
            except concurrent.futures.TimeoutError:
                # Order may have been placed on IB but we timed out waiting.
                # Give 10 more seconds before giving up.
                log.warning(f"[{self.ticker}] Trade entry timed out (60s) — "
                            f"waiting 10 more seconds for recovery...")
                self._errors_today += 1

                orphan_trade = None
                try:
                    orphan_trade = future.result(timeout=10)
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
                    # Could not recover from future — check IB for orphaned fills
                    adopted = self._check_orphaned_fills()
                    if adopted:
                        # Successfully adopted from IB fill data
                        return adopted
                    handle_error(f"scanner-{self.ticker}", "trade_entry_timeout",
                                 TimeoutError("Trade entry timed out after 70s"),
                                 context={"ticker": self.ticker, "recovered": False},
                                 critical=True)
                    raise concurrent.futures.TimeoutError()
        finally:
            pool.shutdown(wait=False)

    def _check_orphaned_fills(self) -> dict | None:
        """Check IB for fills that occurred but weren't tracked.

        If an orphaned fill is found, build a trade dict and adopt it
        immediately instead of waiting for reconciliation.
        """
        try:
            fill = self.client.check_recent_fills(self.ticker)
            if fill:
                log.warning(f"[{self.ticker}] FOUND ORPHANED IB FILL — adopting: {fill}")

                # Build trade dict from fill data
                fill_price = fill.get("price", 0)
                symbol = fill.get("symbol", "").strip()
                qty = int(abs(fill.get("qty", 0)))
                side = fill.get("side", "BOT")
                direction = "LONG" if side == "BOT" else "SHORT"

                trade = {
                    "ticker": self.ticker,
                    "symbol": symbol,
                    "contracts": qty,
                    "entry_price": fill_price,
                    "profit_target": round(fill_price * (1 + config.PROFIT_TARGET), 2),
                    "stop_loss": round(fill_price * (1 - config.STOP_LOSS), 2),
                    "entry_time": datetime.now(PT),
                    "direction": direction,
                    "ib_order_id": fill.get("order_id"),
                    "_adopted_from_fill": True,
                    "strategy_id": self.strategy_id,
                }

                # Register with exit manager (writes to DB)
                self.exit_manager.add_trade(trade)
                self._trades_today += 1
                self._last_trade_time = datetime.now(PT)
                log.info(f"[{self.ticker}] Orphaned trade adopted: {symbol} "
                         f"{qty}x @ ${fill_price:.2f} ({direction})")

                handle_error(f"scanner-{self.ticker}", "orphaned_ib_fill_adopted",
                             RuntimeError(f"Trade filled on IB after timeout — adopted: {symbol}"),
                             context={"ticker": self.ticker, "fill": str(fill),
                                      "adopted": True})
                return trade
            else:
                log.info(f"[{self.ticker}] No IB fills found — order may not have been placed.")
                self._entry_pending = False
                return None
        except Exception as e:
            handle_error(f"scanner-{self.ticker}", "check_orphan_fills", e,
                         context={"ticker": self.ticker})
            return None

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
