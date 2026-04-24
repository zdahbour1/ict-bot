"""Regression: reconciliation PASS 2 must stamp a non-blank
client_trade_id on every adopted IB orphan, so the Trades-tab
Order Ref column never shows blank.

Before 2026-04-24, adopted trades went into the DB with NULL
client_trade_id + NULL signal_type. 29 of 32 ICT trades today
had no Order Ref because they were adopted orphans, making the
Trades tab uninformative.

Fix: `_resolve_adopted_order_ref` prefers IB's ``orderRef`` if
present; otherwise generates `adopted-TICKER-YYMMDD-NN` so the
origin is obvious.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch


class TestResolveAdoptedOrderRef:
    def test_uses_ib_orderRef_when_present(self):
        from strategy.reconciliation import _resolve_adopted_order_ref
        pos = {"orderRef": "ict-SPY-260423-05"}
        assert _resolve_adopted_order_ref(pos, "SPY") == "ict-SPY-260423-05"

    def test_generates_adopted_fallback_when_no_ib_ref(self):
        """When IB doesn't carry the original ref, generate a synthetic
        ``adopted-TICKER-YYMMDD-NN`` tag."""
        from strategy import reconciliation as rc
        # Stub get_session so our helper returns count=0
        fake_session = MagicMock()
        row = MagicMock(); row.__getitem__ = lambda s, i: 0
        fake_session.execute.return_value.fetchone.return_value = [0]
        with patch("db.connection.get_session",
                   return_value=fake_session):
            ref = rc._resolve_adopted_order_ref({}, "AAPL")
        ymd = date.today().strftime("%y%m%d")
        assert ref.startswith(f"adopted-AAPL-{ymd}-")
        assert ref.endswith("01")   # first of the day

    def test_increments_counter_per_day(self):
        """If we adopt multiple orphans for the same ticker on the same
        day, the NN suffix increments so refs stay unique."""
        from strategy import reconciliation as rc
        fake_session = MagicMock()
        # Simulate: already 2 adopted-COIN-260424-XX rows exist
        fake_session.execute.return_value.fetchone.return_value = [2]
        with patch("db.connection.get_session",
                   return_value=fake_session):
            ref = rc._resolve_adopted_order_ref({}, "COIN")
        ymd = date.today().strftime("%y%m%d")
        assert ref == f"adopted-COIN-{ymd}-03"

    def test_truncates_to_column_width(self):
        """trades.client_trade_id is VARCHAR(40). Very long orderRef
        values must be truncated, not rejected."""
        from strategy.reconciliation import _resolve_adopted_order_ref
        long_ref = "a" * 80
        ref = _resolve_adopted_order_ref({"orderRef": long_ref}, "X")
        assert len(ref) <= 40

    def test_empty_ib_orderRef_falls_through_to_fallback(self):
        """An orderRef of '' or '   ' must be treated as missing."""
        from strategy import reconciliation as rc
        fake_session = MagicMock()
        fake_session.execute.return_value.fetchone.return_value = [0]
        with patch("db.connection.get_session",
                   return_value=fake_session):
            ref = rc._resolve_adopted_order_ref({"orderRef": "   "}, "X")
        assert ref.startswith("adopted-X-")
