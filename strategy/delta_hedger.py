"""Delta-hedging loop for delta-neutral trades (ENH-049 Stage 1+2).

One background thread monitors every open ``delta_neutral`` trade on a
short interval (default 30 seconds), computes the aggregate option
delta from BS greeks, and if the net delta drifts beyond a
configurable shares-band it fires a stock BUY/SELL to rebalance.

The goal is the iron-condor's namesake: **delta-neutral**. Without a
dynamic hedge the condor picks up directional exposure the moment the
underlying moves off the body. Monitoring + rebalancing keeps the
trade focused on theta / vega / gamma.

Runtime contract:
- Pure dispatch thread. No state beyond a per-trade hedge-shares
  counter it reads from ``trades.hedge_shares``.
- All DB writes go through the writer's ``_safe_db`` path so a DB
  hiccup never kills the thread.
- All IB calls go through the client's connection pool (``buy_stock``
  / ``sell_stock`` already exist in ``broker.ib_orders``).
- Gated by ``DN_DELTA_HEDGE_ENABLED`` setting (default False for
  safety — user flips via dashboard).

Design: see ``docs/delta_neutral_dynamic_hedging.md`` (to be written)
and the LinkedIn reference "Beyond Directional Bets" by Bejar-Garcia.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# Defaults — all overridable from the settings table
_DEFAULT_INTERVAL_SEC = 30
_DEFAULT_BAND_SHARES = 20          # |net_delta| must exceed this to rebalance
_DEFAULT_SIGMA = 0.20              # BS IV if we don't have a live vol surface
_DEFAULT_RATE = 0.04
_SETTING_PREFIX = "DN_"


def _lookup_variant(strategy_name: Optional[str]):
    """Return the DNVariant for ``strategy_name``, or None when the
    name is not a registered variant (e.g., legacy 'delta_neutral').
    Never raises — the hedger must keep running even if the registry
    is unavailable for some reason."""
    if not strategy_name:
        return None
    try:
        from strategy.delta_neutral_variants import VARIANT_BY_NAME
        return VARIANT_BY_NAME.get(strategy_name)
    except Exception:
        return None


def _dte_days(expiry_yyyymmdd: Optional[str],
              now: Optional[datetime] = None) -> float:
    """Days between now and option expiry. Clamped to >= 0."""
    if not expiry_yyyymmdd:
        return 7.0
    try:
        exp = datetime.strptime(expiry_yyyymmdd, "%Y%m%d").date()
    except Exception:
        return 7.0
    today = (now or datetime.now(timezone.utc)).date()
    return max((exp - today).days, 0.0)


def compute_trade_net_delta(legs: list[dict], underlying_price: float,
                             sigma: float = _DEFAULT_SIGMA,
                             r: float = _DEFAULT_RATE,
                             now: Optional[datetime] = None) -> float:
    """Net *share-equivalent* delta across the multi-leg option position.

    For each leg: delta = BS delta * contracts_open * multiplier * sign
    (sign = +1 for LONG leg, -1 for SHORT). Sum across legs.

    A positive return means the position is net LONG the underlying —
    sell shares to flatten. Negative means net SHORT — buy shares.
    """
    from backtest_engine.option_pricer import bs_greeks
    total = 0.0
    for leg in legs:
        qty = int(leg.get("contracts_open") or 0)
        if qty == 0:
            continue
        right = (leg.get("right") or "C").upper()
        sec_type = (leg.get("sec_type") or "OPT").upper()
        strike = float(leg.get("strike") or underlying_price)
        mult = int(leg.get("multiplier") or 100)
        direction = (leg.get("direction") or "LONG").upper()
        sign = 1 if direction == "LONG" else -1
        # STK legs have delta=1; skip the option pricer.
        if sec_type == "STK":
            total += sign * qty  # share-count, not contracts
            continue
        dte = _dte_days(leg.get("expiry"), now=now)
        T = dte / 365.0
        model = "black76" if sec_type == "FOP" else "bs"
        g = bs_greeks(underlying_price, strike, T, r, sigma, right,
                      model=model)
        total += sign * g.delta * qty * mult
    return total


def compute_rebalance_order(net_delta: float,
                             current_hedge_shares: int,
                             band: int) -> tuple[str, int] | None:
    """Decide what stock order (if any) to fire.

    Net position delta we need to flatten = net_delta + current_hedge_shares.
    (Hedge_shares is positive for long stock, negative for short stock.)
    If abs(flat_target) <= band → no action.
    Otherwise return (action, shares) where action is 'BUY' or 'SELL'
    and shares is the *absolute* integer count we need to trade to move
    to zero.
    """
    residual = net_delta + current_hedge_shares
    if abs(residual) <= band:
        return None
    # Positive residual = net long → need to SELL shares to flatten.
    if residual > 0:
        return ("SELL", int(round(residual)))
    return ("BUY", int(round(-residual)))


# ── The loop ────────────────────────────────────────────────────

class DeltaHedger:
    """Long-lived thread that wakes every N seconds, scans open DN
    trades, and rebalances stock hedge where needed."""

    def __init__(self, client, interval_sec: Optional[int] = None,
                 band_shares: Optional[int] = None):
        self.client = client
        self.interval_sec = interval_sec or _DEFAULT_INTERVAL_SEC
        self.band_shares = band_shares or _DEFAULT_BAND_SHARES
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Remember the last-known flag state so we emit a system_log
        # row only on transitions (avoid spamming 2 rows every 30s).
        self._last_enabled: Optional[bool] = None
        self._last_trade_count: int = -1
        # ENH-049 Stage 3: price at the last rebalance check per ticker.
        # When the underlying moves by more than a configurable fraction
        # since the last check, we skip the 30s sleep and rebalance
        # immediately. Cheap event-driven trigger on top of the timer.
        self._last_price_by_ticker: dict[str, float] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="delta-hedger", daemon=True)
        self._thread.start()
        log.info(f"[DELTA-HEDGER] started — interval={self.interval_sec}s "
                 f"band={self.band_shares} shares")
        _update_thread_row("idle",
                           f"started — interval={self.interval_sec}s "
                           f"band={self.band_shares}")
        _sys_log("info",
                 f"Delta-hedger thread started "
                 f"(interval={self.interval_sec}s, band={self.band_shares})",
                 {"interval_sec": self.interval_sec,
                  "band_shares": self.band_shares})

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._one_pass()
            except Exception as e:
                log.error(f"[DELTA-HEDGER] pass error: {e}", exc_info=True)
            # ENH-049 Stage 3: event-driven trigger.
            # Instead of a flat 30-sec sleep, check every second for a
            # large underlying move on any open DN ticker. If any
            # ticker moves by more than DN_EVENT_TRIGGER_BPS basis
            # points, break out of the sleep and rebalance now.
            # Falls back to the normal interval_sec timer when nothing
            # triggers.
            total_sleep_steps = int(self.interval_sec * 2)
            for _ in range(total_sleep_steps):
                if self._stop.is_set():
                    break
                if self._event_trigger_fired():
                    break
                time.sleep(0.5)

    def _event_trigger_fired(self) -> bool:
        """Poll last-known prices for open DN tickers; return True when
        any move exceeds the threshold. Updates the cache as a
        side-effect."""
        try:
            from db.settings_cache import get_bool, get_float
            if not get_bool("DN_EVENT_DRIVEN_HEDGE", default=False):
                return False
            threshold_bps = get_float("DN_EVENT_TRIGGER_BPS",
                                       default=30.0)  # 30 bps = 0.30%
        except Exception:
            return False
        try:
            trades = _fetch_open_dn_trades()
        except Exception:
            return False
        if not trades:
            return False
        for t in trades:
            ticker = t.get("ticker")
            if not ticker:
                continue
            try:
                px = float(self.client.get_realtime_equity_price(ticker))
            except Exception:
                continue
            prev = self._last_price_by_ticker.get(ticker)
            self._last_price_by_ticker[ticker] = px
            if prev is None or prev <= 0:
                continue
            move_bps = abs(px - prev) / prev * 10_000.0
            if move_bps >= threshold_bps:
                log.info(f"[DELTA-HEDGER] event-trigger: {ticker} moved "
                         f"{move_bps:.0f}bps ({prev:.2f} → {px:.2f}) — "
                         f"firing early rebalance")
                _sys_log("info",
                         f"Event-driven trigger: {ticker} moved "
                         f"{move_bps:.0f}bps (threshold {threshold_bps:.0f}bps)",
                         {"ticker": ticker, "prev": prev, "now": px,
                          "move_bps": round(move_bps, 1)})
                return True
        return False

    def _is_enabled(self) -> bool:
        """Read the DN_DELTA_HEDGE_ENABLED flag from the settings cache."""
        try:
            from db.settings_cache import get_bool
            return get_bool("DN_DELTA_HEDGE_ENABLED", default=False)
        except Exception:
            return False

    def _refresh_config(self) -> None:
        try:
            from db.settings_cache import get_int
            self.interval_sec = get_int("DN_REBALANCE_INTERVAL_SEC",
                                         default=self.interval_sec) or self.interval_sec
            self.band_shares = get_int("DN_DELTA_BAND_SHARES",
                                        default=self.band_shares) or self.band_shares
        except Exception:
            pass

    def _one_pass(self) -> None:
        enabled = self._is_enabled()
        # Log only the flag-transition, not every heartbeat.
        if enabled != self._last_enabled:
            if self._last_enabled is not None:
                _sys_log("info",
                         f"DN_DELTA_HEDGE_ENABLED flipped "
                         f"{self._last_enabled} → {enabled}",
                         {"enabled": enabled})
            self._last_enabled = enabled
        if not enabled:
            _update_thread_row("idle",
                               "DN_DELTA_HEDGE_ENABLED is false — monitor only")
            return
        self._refresh_config()
        trades = _fetch_open_dn_trades()
        # Log trade-count transitions only (0→N or N→0 or N→M).
        if len(trades) != self._last_trade_count:
            _sys_log("info",
                     f"Open DN trade count: {self._last_trade_count} → "
                     f"{len(trades)}",
                     {"count": len(trades),
                      "trade_ids": [t.get("trade_id") for t in trades]})
            self._last_trade_count = len(trades)
        if not trades:
            _update_thread_row("running",
                               f"no open DN trades | interval={self.interval_sec}s "
                               f"band={self.band_shares}")
            return
        _update_thread_row("running",
                           f"rebalancing {len(trades)} DN trade(s) "
                           f"(band={self.band_shares})")
        for t in trades:
            try:
                self._rebalance_one(t)
            except Exception as e:
                log.warning(
                    f"[DELTA-HEDGER] trade_id={t.get('trade_id')} skip: {e}")
                _sys_log("error",
                         f"Rebalance skip trade_id={t.get('trade_id')} "
                         f"ticker={t.get('ticker')}: {type(e).__name__}: {e}",
                         {"trade_id": t.get("trade_id"),
                          "ticker": t.get("ticker"),
                          "error": str(e)[:300]})

    def _rebalance_one(self, trade: dict) -> None:
        ticker = trade["ticker"]
        trade_id = trade["trade_id"]
        legs = trade.get("legs") or []
        if not legs:
            return
        # Per-variant config: skip trades whose variant has
        # delta_hedge=False, and honor hedge_delta_band_shares override
        # (ZDN uses 10 shares, canonical V5 uses the global ~20).
        variant = _lookup_variant(trade.get("strategy_name"))
        if variant is not None and variant.delta_hedge is False:
            return
        band_shares = self.band_shares
        if variant is not None and variant.hedge_delta_band_shares > 0:
            band_shares = variant.hedge_delta_band_shares
        # Quote the underlying once per trade
        try:
            underlying = float(self.client.get_realtime_equity_price(ticker))
        except Exception as e:
            log.warning(f"[DELTA-HEDGER] {ticker}: quote failed ({e}) "
                        f"— skipping rebalance this tick")
            return
        net_delta = compute_trade_net_delta(legs, underlying)
        current_hedge = int(trade.get("hedge_shares") or 0)
        action_plan = compute_rebalance_order(
            net_delta, current_hedge, band_shares)
        log.info(f"[DELTA-HEDGER] {ticker} tid={trade_id} "
                 f"net_delta={net_delta:+.1f} hedge={current_hedge:+d} "
                 f"band=±{band_shares} "
                 f"action={action_plan}")
        if action_plan is None:
            return
        action, shares = action_plan
        _sys_log("info",
                 f"{ticker} tid={trade_id}: planning {action} {shares}x "
                 f"(net_delta={net_delta:+.1f}, hedge={current_hedge:+d}, "
                 f"band=±{band_shares})",
                 {"ticker": ticker, "trade_id": trade_id,
                  "net_delta": round(net_delta, 2),
                  "current_hedge": current_hedge,
                  "action": action, "shares": shares})
        # Place the hedge order. Stamp IB Order Ref with the parent
        # trade's ref + '-hedge' so the hedge is traceable to its DN
        # trade in TWS Activity tab.
        hedge_ref = f"hedge-{ticker}-tid{trade_id}"
        try:
            method = (self.client.buy_stock if action == "BUY"
                      else self.client.sell_stock)
            result = method(ticker, shares, order_ref=hedge_ref)
            fill_price = (result or {}).get("fill_price") or 0.0
            order_id = (result or {}).get("order_id")
        except Exception as e:
            log.error(f"[DELTA-HEDGER] {ticker} {action} {shares} failed: {e}")
            _record_hedge_event(trade_id, ticker, action, shares, 0.0,
                                None, net_delta, error=str(e)[:200])
            _sys_log("error",
                     f"{ticker} tid={trade_id}: {action} {shares}x FAILED "
                     f"({type(e).__name__}: {e})",
                     {"ticker": ticker, "trade_id": trade_id,
                      "error": str(e)[:300]})
            return
        # Update envelope + audit record
        signed_delta = shares if action == "BUY" else -shares
        new_hedge = current_hedge + signed_delta
        _update_trade_hedge_shares(trade_id, new_hedge)
        _record_hedge_event(trade_id, ticker, action, shares, fill_price,
                            order_id, net_delta)
        log.info(f"[DELTA-HEDGER] {ticker} {action} {shares}x "
                 f"fill=${fill_price:.2f} → hedge_shares={new_hedge:+d}")
        _sys_log("info",
                 f"{ticker} tid={trade_id}: {action} {shares}x "
                 f"fill=${fill_price:.2f} → hedge_shares={new_hedge:+d}",
                 {"ticker": ticker, "trade_id": trade_id,
                  "action": action, "shares": shares,
                  "fill_price": fill_price,
                  "new_hedge_shares": new_hedge,
                  "order_id": order_id})


# ── DB helpers ───────────────────────────────────────────────────

def _fetch_open_dn_trades() -> list[dict]:
    """Open delta_neutral trades with their legs, as dicts."""
    try:
        from db.connection import get_session
        from sqlalchemy import text
        session = get_session()
        if session is None:
            return []
        try:
            # Match legacy delta_neutral plus every registered DN
            # variant (v1_…v5b_, zdn_*). Any strategy whose name starts
            # with 'v' (v1..v5b) or 'zdn_' is part of the DN family and
            # is eligible for delta hedging. The per-variant
            # `delta_hedge` flag is what actually decides whether we
            # fire stock orders; we still fetch so the dashboard shows
            # the computed net_delta.
            trade_rows = session.execute(text(
                """
                SELECT t.id, t.ticker, COALESCE(t.hedge_shares, 0), s.name
                  FROM trades t
                  JOIN strategies s ON s.strategy_id = t.strategy_id
                 WHERE t.status='open'
                   AND (s.name = 'delta_neutral'
                        OR s.name LIKE 'v%'
                        OR s.name LIKE 'zdn_%')
                """
            )).fetchall()
            if not trade_rows:
                return []
            out: list[dict] = []
            for tid, ticker, hedge_shares, strategy_name in trade_rows:
                legs = session.execute(text(
                    """
                    SELECT leg_index, leg_role, sec_type, symbol, underlying,
                           strike, "right", expiry, multiplier,
                           direction, contracts_open
                      FROM trade_legs
                     WHERE trade_id=:id AND leg_status='open'
                       AND contracts_open > 0
                    """
                ), {"id": tid}).fetchall()
                out.append({
                    "trade_id": tid,
                    "ticker": ticker,
                    "hedge_shares": int(hedge_shares or 0),
                    "strategy_name": strategy_name,
                    "legs": [{
                        "leg_index": row[0], "leg_role": row[1],
                        "sec_type": row[2], "symbol": row[3],
                        "underlying": row[4],
                        "strike": float(row[5]) if row[5] is not None else None,
                        "right": row[6], "expiry": row[7],
                        "multiplier": int(row[8] or 100),
                        "direction": row[9],
                        "contracts_open": int(row[10] or 0),
                    } for row in legs]
                })
            return out
        finally:
            session.close()
    except Exception as e:
        log.debug(f"_fetch_open_dn_trades failed: {e}")
        return []


def _update_trade_hedge_shares(trade_id: int, new_hedge: int) -> None:
    try:
        from db.connection import get_session
        from sqlalchemy import text
        session = get_session()
        if session is None:
            return
        try:
            session.execute(text(
                "UPDATE trades SET hedge_shares=:h, updated_at=NOW() "
                "WHERE id=:id"
            ), {"h": int(new_hedge), "id": int(trade_id)})
            session.commit()
        finally:
            session.close()
    except Exception as e:
        log.warning(f"_update_trade_hedge_shares failed: {e}")


def _update_thread_row(status: str, message: str) -> None:
    """Heartbeat into thread_status so the Threads dashboard shows
    the hedger is alive. Never raises."""
    try:
        from db.writer import update_thread_status
        update_thread_status("delta-hedger", None, status, message)
    except Exception:
        pass


def _sys_log(level: str, message: str, details: dict | None = None) -> None:
    """Write to the ``system_log`` table under component='delta-hedger'
    so the Threads page log viewer (which filters by component) can
    surface our events. Never raises."""
    try:
        from db.writer import add_system_log
        add_system_log("delta-hedger", level, message, details or {})
    except Exception:
        pass


def _record_hedge_event(trade_id: int, ticker: str, action: str,
                        shares: int, fill_price: float,
                        order_id: Optional[int], net_delta_before: float,
                        error: Optional[str] = None) -> None:
    try:
        from db.connection import get_session
        from sqlalchemy import text
        session = get_session()
        if session is None:
            return
        try:
            session.execute(text(
                """
                INSERT INTO delta_hedges
                  (trade_id, ticker, action, shares, fill_price,
                   order_id, net_delta_before, error, created_at)
                VALUES
                  (:tid, :tk, :ac, :sh, :fp, :oid, :nd, :err, NOW())
                """
            ), {"tid": int(trade_id), "tk": ticker, "ac": action,
                "sh": int(shares), "fp": float(fill_price or 0.0),
                "oid": order_id, "nd": float(net_delta_before),
                "err": error})
            session.commit()
        finally:
            session.close()
    except Exception as e:
        log.debug(f"_record_hedge_event failed: {e}")
