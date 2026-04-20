"""Orphan bracket order detector.

Runs as PASS 3 of the periodic reconciliation cycle. Finds working
SELL orders on IB that have no matching open DB trade and no
positive IB position to sell from — these would flip us short if
they fire.

Multi-phase: an orphan must be seen across TWO scans separated by
the grace period before any action is taken. Avoids false positives
from races (DB commit lag, entry-in-flight, etc.).

See ``docs/orphan_bracket_detector.md`` for the full design.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)


# Default grace period (override via settings table).  Orders
# identified on one scan must still be orphaned on a later scan
# AT LEAST this many seconds later before we act.
DEFAULT_GRACE_PERIOD_SEC = 60


class OrphanBracketDetector:
    """Stateful detector. One instance is shared across reconcile cycles
    so the suspect dict persists.

    Thread-safety: ``scan()`` is invoked from the exit_manager loop, the
    same thread that owns the periodic reconciliation call. No external
    concurrent access expected. If that changes, add a lock around
    ``self.suspect_orders``.
    """

    # Terminal states — order is definitely gone.
    _TERMINAL = {"Cancelled", "ApiCancelled", "Inactive", "Filled"}
    # Statuses where the order is actively working.
    _ACTIVE = {"Submitted", "PreSubmitted", "PendingSubmit"}

    def __init__(self, grace_period_sec: Optional[float] = None,
                 auto_cancel: bool = True):
        self.suspect_orders: dict[int, float] = {}  # orderId → first_seen_ts
        self.grace_period_sec = (
            grace_period_sec
            if grace_period_sec is not None
            else self._load_grace_period()
        )
        self.auto_cancel = auto_cancel

    @staticmethod
    def _load_grace_period() -> float:
        """Read grace period from the settings table; fall back to default."""
        try:
            from db.connection import get_session
            from sqlalchemy import text
            session = get_session()
            if session is None:
                return DEFAULT_GRACE_PERIOD_SEC
            row = session.execute(
                text("SELECT value FROM settings "
                     "WHERE key='ORPHAN_GRACE_PERIOD_SEC' AND strategy_id IS NULL"),
            ).fetchone()
            session.close()
            if row and row[0]:
                return float(row[0])
        except Exception:
            pass
        return DEFAULT_GRACE_PERIOD_SEC

    def scan(self, client, open_db_con_ids: set[int],
             ib_positions_by_con_id: dict[int, int]) -> list[dict]:
        """Run one detector cycle.

        Parameters
        ----------
        client : BrokerClient
            Live IB client with ``get_all_working_orders`` +
            ``cancel_order_by_id``. Must have been refreshed via
            ``refresh_all_open_orders()`` beforehand.
        open_db_con_ids : set[int]
            Every conId with an open DB trade. Source of truth.
        ib_positions_by_con_id : dict[int, int]
            Every IB position qty keyed by conId.

        Returns
        -------
        list[dict]
            The orders we identified + attempted to cancel in this
            cycle. Empty on normal cycles.
        """
        now = time.time()
        seen_this_cycle: set[int] = set()
        cancelled_this_cycle: list[dict] = []

        try:
            all_orders = client.get_all_working_orders()
        except Exception as e:
            log.warning(f"[ORPHAN] Could not fetch working orders: {e}")
            return []

        for order in all_orders:
            order_id = order.get("orderId")
            if order_id is None:
                continue
            seen_this_cycle.add(order_id)

            # ── Filter: only sell-side bracket children ──
            if order.get("action") != "SELL":
                continue
            if order.get("status") not in self._ACTIVE:
                continue
            if (order.get("parentId") or 0) == 0:
                # Standalone order — could be user's manual TWS entry.
                # Leaving it alone is the safer default.
                continue

            con_id = order.get("conId")
            if not con_id:
                continue

            # ── Hygiene: matching DB trade → NOT an orphan ──
            if con_id in open_db_con_ids:
                # Legitimate bracket for an open trade. Clear suspicion.
                self.suspect_orders.pop(order_id, None)
                continue

            # ── Hygiene: we hold a long position → NOT an orphan ──
            # Either reconcile PASS 2 will adopt it, or the user holds
            # it manually. Selling a long position is fine (just closes).
            if ib_positions_by_con_id.get(con_id, 0) > 0:
                self.suspect_orders.pop(order_id, None)
                continue

            # ── Candidate orphan: SELL, no DB trade, no long position ──
            first_seen = self.suspect_orders.get(order_id)
            if first_seen is None:
                # First time seeing this — mark suspect, don't act yet.
                self.suspect_orders[order_id] = now
                log.info(
                    f"[ORPHAN-WATCH] SELL order flagged SUSPECT: "
                    f"orderId={order_id} permId={order.get('permId')} "
                    f"conId={con_id} symbol={order.get('symbol')} "
                    f"type={order.get('orderType')} "
                    f"price=${order.get('lmtPrice') or order.get('auxPrice')} "
                    f"(grace={self.grace_period_sec:.0f}s)"
                )
                continue

            age = now - first_seen
            if age < self.grace_period_sec:
                # Still within grace — wait for next cycle.
                continue

            # ── CONFIRMED ORPHAN ──
            order["_orphan_age_sec"] = round(age, 1)
            result = self._handle_orphan(client, order)
            cancelled_this_cycle.append(result)

            # Whether we cancelled or not, remove from suspect — if it
            # survives the action, it'll be re-detected next cycle.
            self.suspect_orders.pop(order_id, None)

        # ── Housekeeping: prune suspects that vanished ──
        stale = [oid for oid in self.suspect_orders
                 if oid not in seen_this_cycle]
        for oid in stale:
            del self.suspect_orders[oid]
        if stale:
            log.debug(
                f"[ORPHAN-WATCH] Pruned {len(stale)} resolved suspect(s): "
                f"{stale}"
            )

        return cancelled_this_cycle

    def _handle_orphan(self, client, order: dict) -> dict:
        """Cancel + audit a confirmed orphan. Never raises."""
        from strategy.audit import log_trade_action

        order_id = order.get("orderId")
        con_id = order.get("conId")
        symbol = order.get("symbol", "")
        age = order.get("_orphan_age_sec", 0)

        if not self.auto_cancel:
            log.warning(
                f"[ORPHAN] DETECTED (auto_cancel=False, not acting): "
                f"orderId={order_id} conId={con_id} symbol={symbol} "
                f"suspect_for={age:.0f}s"
            )
            log_trade_action(
                None, "orphan_detected_not_cancelled", "reconciliation",
                f"orphan bracket SELL orderId={order_id} {symbol} "
                f"suspect_for={age:.0f}s (auto_cancel disabled)",
                level="warn",
                extra={
                    "orderId": order_id,
                    "permId":   order.get("permId"),
                    "conId":    con_id,
                    "symbol":   symbol,
                    "orderType": order.get("orderType"),
                    "price_level": order.get("lmtPrice") or order.get("auxPrice"),
                    "age_suspected_sec": age,
                },
            )
            return {**order, "_outcome": "detected_only"}

        log.warning(
            f"[ORPHAN] CONFIRMED: cancelling orphan bracket orderId={order_id} "
            f"permId={order.get('permId')} conId={con_id} symbol={symbol} "
            f"type={order.get('orderType')} "
            f"price=${order.get('lmtPrice') or order.get('auxPrice')} "
            f"suspect_for={age:.0f}s"
        )

        try:
            client.cancel_order_by_id(order_id)
            outcome = "cancel_sent"
        except Exception as e:
            log.error(f"[ORPHAN] Cancel failed for orderId={order_id}: {e}")
            outcome = f"cancel_failed: {e}"

        log_trade_action(
            None, "cancel_orphan_bracket", "reconciliation",
            f"orphan bracket SELL cancelled: orderId={order_id} {symbol} "
            f"({order.get('orderType')} @ "
            f"${order.get('lmtPrice') or order.get('auxPrice')})"
            f" suspect_for={age:.0f}s — outcome: {outcome}",
            level="warn",
            extra={
                "orderId":    order_id,
                "permId":     order.get("permId"),
                "conId":      con_id,
                "symbol":     symbol,
                "orderType":  order.get("orderType"),
                "action":     order.get("action"),
                "price_level": order.get("lmtPrice") or order.get("auxPrice"),
                "parentId":   order.get("parentId"),
                "age_suspected_sec": age,
                "outcome":    outcome,
            },
        )
        return {**order, "_outcome": outcome}
