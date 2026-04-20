"""Unit tests for OrphanBracketDetector (strategy/orphan_detector.py).

Covers the multi-phase suspect → confirm → cancel flow, the filters
that distinguish orphans from legitimate brackets, and the
``auto_cancel=False`` detection-only mode.

See ``docs/orphan_bracket_detector.md``.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _msft_sell_order(order_id=4383, **overrides):
    """Build a mock SELL bracket-child order. Overrides merge in."""
    base = {
        "orderId": order_id,
        "permId":  order_id + 1_000_000,
        "action":  "SELL",
        "orderType": "LMT",
        "status":  "Submitted",
        "conId":   874403104,
        "parentId": 4382,                 # bracket child
        "lmtPrice": 2.42,
        "auxPrice": 0.0,
        "symbol":  "MSFT260420C00417500",
        "totalQty": 2,
        "clientId": 3,
    }
    base.update(overrides)
    return base


class TestFirstSightingMarksSuspect:
    """Phase 1 — a new candidate orphan must be flagged but NOT cancelled."""

    def test_first_sighting_is_suspect_only(self):
        from strategy.orphan_detector import OrphanBracketDetector

        client = MagicMock()
        client.get_all_working_orders.return_value = [_msft_sell_order()]
        det = OrphanBracketDetector(grace_period_sec=60)
        open_con_ids = set()  # no matching open trade
        ib_positions = {}     # no position

        cancelled = det.scan(client, open_con_ids, ib_positions)

        assert cancelled == []
        assert 4383 in det.suspect_orders
        # Cancel was NOT called on the first sighting
        assert not client.cancel_order_by_id.called


class TestSecondSightingAfterGraceCancels:
    """Phase 2 — same orphan seen after grace period → cancel."""

    def test_aged_out_orphan_cancelled(self):
        from strategy.orphan_detector import OrphanBracketDetector

        client = MagicMock()
        client.get_all_working_orders.return_value = [_msft_sell_order()]
        det = OrphanBracketDetector(grace_period_sec=60)
        # Seed the suspect as if we saw it 90 seconds ago
        det.suspect_orders[4383] = -1_000_000  # far past, ensures age > grace

        cancelled = det.scan(client, set(), {})
        assert len(cancelled) == 1
        assert cancelled[0]["orderId"] == 4383
        assert cancelled[0].get("_outcome") == "cancel_sent"
        assert client.cancel_order_by_id.called
        # Suspect dict is pruned after action
        assert 4383 not in det.suspect_orders


class TestFiltersExcludeLegitimateOrders:
    def test_matching_db_trade_not_flagged(self):
        """SELL bracket child for a contract we DO have an open trade
        on → legitimate, skip."""
        from strategy.orphan_detector import OrphanBracketDetector

        client = MagicMock()
        client.get_all_working_orders.return_value = [_msft_sell_order()]
        det = OrphanBracketDetector()
        det.suspect_orders[4383] = -1_000_000  # would be confirmed if no match

        open_db = {874403104}                  # conId matches
        cancelled = det.scan(client, open_db, {874403104: 2})

        assert cancelled == []
        assert not client.cancel_order_by_id.called
        # Must have been cleared from suspect too
        assert 4383 not in det.suspect_orders

    def test_positive_position_not_flagged(self):
        """Even if no open DB trade, a positive IB position means the
        SELL would legitimately close it. Not an orphan."""
        from strategy.orphan_detector import OrphanBracketDetector

        client = MagicMock()
        client.get_all_working_orders.return_value = [_msft_sell_order()]
        det = OrphanBracketDetector()
        det.suspect_orders[4383] = -1_000_000

        cancelled = det.scan(client, set(), {874403104: 2})
        assert cancelled == []
        assert not client.cancel_order_by_id.called

    def test_standalone_order_not_flagged(self):
        """parentId=0 = not a bracket child. Likely user-placed. Skip."""
        from strategy.orphan_detector import OrphanBracketDetector

        client = MagicMock()
        client.get_all_working_orders.return_value = [
            _msft_sell_order(parentId=0),
        ]
        det = OrphanBracketDetector()
        cancelled = det.scan(client, set(), {})
        assert cancelled == []
        # Not even marked suspect
        assert 4383 not in det.suspect_orders

    def test_buy_order_not_flagged(self):
        """BUY orders can't flip us short; never an orphan."""
        from strategy.orphan_detector import OrphanBracketDetector

        client = MagicMock()
        client.get_all_working_orders.return_value = [
            _msft_sell_order(action="BUY"),
        ]
        det = OrphanBracketDetector()
        cancelled = det.scan(client, set(), {})
        assert cancelled == []
        assert 4383 not in det.suspect_orders

    def test_terminal_status_not_flagged(self):
        """Already-cancelled orders don't come back as orphans."""
        from strategy.orphan_detector import OrphanBracketDetector

        client = MagicMock()
        client.get_all_working_orders.return_value = [
            _msft_sell_order(status="Cancelled"),
        ]
        det = OrphanBracketDetector()
        cancelled = det.scan(client, set(), {})
        assert cancelled == []
        assert 4383 not in det.suspect_orders


class TestResolvedOrphanPruned:
    """If an order disappears between scans (cancelled externally or
    matched to a newly-opened DB trade), its entry in suspect_orders
    must be cleaned up."""

    def test_order_vanishes_and_suspect_is_pruned(self):
        from strategy.orphan_detector import OrphanBracketDetector

        client = MagicMock()
        det = OrphanBracketDetector()
        # Seed a suspect for an order that WON'T show up in this cycle
        det.suspect_orders[9999] = 0.0
        client.get_all_working_orders.return_value = []  # nothing visible

        det.scan(client, set(), {})
        assert 9999 not in det.suspect_orders


class TestIbError201FastPath:
    """When IB rejects a new bracket with 'Cannot have open orders on
    both sides of the same US Option contract' (code 201), orphaned
    brackets are demonstrated to exist. Skip the normal 60s grace —
    cancel immediately."""

    def test_fast_path_cancels_existing_sell_orders(self):
        from strategy.option_selector import _trigger_orphan_scan_fast_path

        client = MagicMock()
        client.refresh_all_open_orders.return_value = 3
        client.find_open_orders_for_contract.return_value = [
            {"orderId": 3913, "action": "SELL", "status": "Submitted",
             "orderType": "LMT", "lmtPrice": 7.50, "auxPrice": 0.0,
             "permId": 12345},
            {"orderId": 3914, "action": "SELL", "status": "PreSubmitted",
             "orderType": "STP", "lmtPrice": 0.0, "auxPrice": 1.50,
             "permId": 12346},
        ]
        order_result = {"con_id": 872076986, "ib_error": {"code": 201}}

        _trigger_orphan_scan_fast_path(
            client, "INTC", "INTC260424C00066000", order_result
        )

        # Both SELL orders cancelled
        cancelled_ids = {c.args[0] for c in client.cancel_order_by_id.call_args_list}
        assert 3913 in cancelled_ids
        assert 3914 in cancelled_ids

    def test_fast_path_noop_when_no_sell_orders(self):
        from strategy.option_selector import _trigger_orphan_scan_fast_path

        client = MagicMock()
        client.refresh_all_open_orders.return_value = 0
        client.find_open_orders_for_contract.return_value = []
        order_result = {"con_id": 999, "ib_error": {"code": 201}}

        # Should not raise, should not call cancel
        _trigger_orphan_scan_fast_path(
            client, "INTC", "INTC260424C00066000", order_result
        )
        assert not client.cancel_order_by_id.called

    def test_fast_path_noop_without_con_id(self):
        from strategy.option_selector import _trigger_orphan_scan_fast_path

        client = MagicMock()
        order_result = {"ib_error": {"code": 201}}  # no con_id

        _trigger_orphan_scan_fast_path(
            client, "INTC", "INTC260424C00066000", order_result
        )
        # Should bail out without querying
        assert not client.find_open_orders_for_contract.called


class TestDetectOnlyMode:
    """auto_cancel=False: log + audit but don't cancel."""

    def test_detect_only_does_not_cancel(self):
        from strategy.orphan_detector import OrphanBracketDetector

        client = MagicMock()
        client.get_all_working_orders.return_value = [_msft_sell_order()]
        det = OrphanBracketDetector(grace_period_sec=60, auto_cancel=False)
        det.suspect_orders[4383] = -1_000_000

        cancelled = det.scan(client, set(), {})
        assert len(cancelled) == 1
        assert cancelled[0].get("_outcome") == "detected_only"
        # Cancel must NOT have been called
        assert not client.cancel_order_by_id.called
