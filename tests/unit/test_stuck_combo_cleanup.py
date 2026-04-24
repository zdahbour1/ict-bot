"""Tests for stuck-combo entry cleanup (ENH-062, 2026-04-24).

Two layers:
  1. ``_cleanup_orphan_combo_by_ref`` — called immediately from the
     multi-leg entry exception handler to cancel the parent BAG order
     when place_combo raised (e.g. TimeoutError).
  2. ``_pass6_cancel_stuck_entry_combos`` — recurring reconciliation
     sweep that catches any surviving PendingSubmit BUY Bag whose
     orderRef has no matching open DB trade.

Without these, a timed-out multi-leg entry can leave a combo parent in
PendingSubmit forever, fill later, and create an orphan position with
no DB envelope — exactly the "brackets without trades" state observed
by the user on 2026-04-24.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestCleanupOrphanComboByRef:
    def test_cancels_pending_bag_matching_order_ref(self):
        from strategy.trade_entry_manager import _cleanup_orphan_combo_by_ref
        client = MagicMock()
        client.get_all_working_orders.return_value = [
            {"orderId": 999, "action": "BUY", "status": "PendingSubmit",
             "secType": "BAG", "orderRef": "zdn_0dte-AVGO-260424-01",
             "symbol": "AVGO"},
        ]
        n = _cleanup_orphan_combo_by_ref(
            client, "AVGO", "zdn_0dte-AVGO-260424-01")
        assert n == 1
        client.cancel_order_by_id.assert_called_once_with(999)

    def test_leaves_unrelated_refs_alone(self):
        from strategy.trade_entry_manager import _cleanup_orphan_combo_by_ref
        client = MagicMock()
        client.get_all_working_orders.return_value = [
            {"orderId": 1, "action": "BUY", "status": "PendingSubmit",
             "secType": "BAG", "orderRef": "someone-else-01",
             "symbol": "AVGO"},
        ]
        n = _cleanup_orphan_combo_by_ref(
            client, "AVGO", "zdn_0dte-AVGO-260424-01")
        assert n == 0
        client.cancel_order_by_id.assert_not_called()

    def test_ignores_already_terminal_orders(self):
        from strategy.trade_entry_manager import _cleanup_orphan_combo_by_ref
        client = MagicMock()
        client.get_all_working_orders.return_value = [
            {"orderId": 1, "action": "BUY", "status": "Filled",
             "secType": "BAG", "orderRef": "x-y-z", "symbol": "A"},
            {"orderId": 2, "action": "BUY", "status": "Cancelled",
             "secType": "BAG", "orderRef": "x-y-z", "symbol": "A"},
        ]
        n = _cleanup_orphan_combo_by_ref(client, "A", "x-y-z")
        assert n == 0
        client.cancel_order_by_id.assert_not_called()

    def test_empty_ref_is_noop(self):
        from strategy.trade_entry_manager import _cleanup_orphan_combo_by_ref
        client = MagicMock()
        assert _cleanup_orphan_combo_by_ref(client, "A", None) == 0
        assert _cleanup_orphan_combo_by_ref(client, "A", "") == 0
        client.get_all_working_orders.assert_not_called()

    def test_swallows_get_orders_failure(self):
        """Must not raise — the caller has already failed the entry;
        a cleanup failure shouldn't propagate on top of that."""
        from strategy.trade_entry_manager import _cleanup_orphan_combo_by_ref
        client = MagicMock()
        client.get_all_working_orders.side_effect = RuntimeError("conn lost")
        # Should return 0, never raise
        assert _cleanup_orphan_combo_by_ref(client, "A", "ref-1") == 0


class TestPass6StuckComboSweep:
    def _mk_client(self, working_orders):
        client = MagicMock()
        client.get_all_working_orders.return_value = working_orders
        return client

    def test_cancels_bag_with_no_matching_open_trade(self):
        from strategy.reconciliation import _pass6_cancel_stuck_entry_combos
        client = self._mk_client([
            {"orderId": 50, "action": "BUY", "status": "PendingSubmit",
             "secType": "BAG", "orderRef": "v1_baseline-AVGO-260424-01",
             "symbol": "AVGO"},
        ])
        # DB session returns NO matching open trade
        with patch("db.connection.get_session") as mk:
            sess = MagicMock()
            sess.execute.return_value.fetchall.return_value = [
                ("some-other-ref",),
            ]
            mk.return_value = sess
            n = _pass6_cancel_stuck_entry_combos(client, min_age_sec=0)
        assert n == 1
        client.cancel_order_by_id.assert_called_once_with(50)

    def test_skips_bag_with_matching_open_trade(self):
        """Live entry in flight — must NOT cancel."""
        from strategy.reconciliation import _pass6_cancel_stuck_entry_combos
        client = self._mk_client([
            {"orderId": 50, "action": "BUY", "status": "PendingSubmit",
             "secType": "BAG", "orderRef": "zdn_weekly-SPY-260424-01",
             "symbol": "SPY"},
        ])
        with patch("db.connection.get_session") as mk:
            sess = MagicMock()
            sess.execute.return_value.fetchall.return_value = [
                ("zdn_weekly-SPY-260424-01",),
            ]
            mk.return_value = sess
            n = _pass6_cancel_stuck_entry_combos(client, min_age_sec=0)
        assert n == 0
        client.cancel_order_by_id.assert_not_called()

    def test_skips_sell_brackets(self):
        """Only BUY BAGs are in scope; SELL (brackets) belong to PASS 5."""
        from strategy.reconciliation import _pass6_cancel_stuck_entry_combos
        client = self._mk_client([
            {"orderId": 60, "action": "SELL", "status": "PreSubmitted",
             "secType": "OPT", "orderRef": "orphan-SPY",
             "symbol": "SPY260501C00500000"},
        ])
        with patch("db.connection.get_session") as mk:
            sess = MagicMock()
            sess.execute.return_value.fetchall.return_value = []
            mk.return_value = sess
            n = _pass6_cancel_stuck_entry_combos(client, min_age_sec=0)
        assert n == 0
        client.cancel_order_by_id.assert_not_called()

    def test_skips_untagged_combos(self):
        """orderRef empty → can't link; PASS 5's generic sweep will
        handle these if they're single-leg brackets. PASS 6 stays
        conservative."""
        from strategy.reconciliation import _pass6_cancel_stuck_entry_combos
        client = self._mk_client([
            {"orderId": 70, "action": "BUY", "status": "PendingSubmit",
             "secType": "BAG", "orderRef": "", "symbol": "COIN"},
        ])
        with patch("db.connection.get_session") as mk:
            sess = MagicMock()
            sess.execute.return_value.fetchall.return_value = []
            mk.return_value = sess
            n = _pass6_cancel_stuck_entry_combos(client, min_age_sec=0)
        assert n == 0
