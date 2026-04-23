"""Unit tests for ENH-047 /api/trades/{id}/legs endpoint + _leg_to_dict.

Scope: the per-leg drill-down endpoint + the leg→dict serializer.
Covers shape, P&L math, status pass-through, and 404 on unknown trades.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _leg(**kwargs) -> SimpleNamespace:
    """Build a fake TradeLeg-ish object with sensible defaults."""
    defaults = dict(
        leg_id=1, trade_id=100, leg_index=0, leg_role="short_call",
        sec_type="OPT", symbol="SPY260515C00500000", underlying="SPY",
        strike=500.0, right="C", expiry="20260515",
        multiplier=100, exchange="SMART", currency="USD",
        direction="SHORT", contracts_entered=1, contracts_open=1,
        contracts_closed=0,
        entry_price=2.50, exit_price=None, current_price=2.00,
        ib_fill_price=2.50,
        profit_target=None, stop_loss_level=None,
        ib_order_id=1001, ib_perm_id=None, ib_con_id=12345,
        ib_tp_order_id=None, ib_tp_perm_id=None,
        ib_tp_status=None, ib_tp_price=None,
        ib_sl_order_id=None, ib_sl_perm_id=None,
        ib_sl_status=None, ib_sl_price=None,
        entry_time=datetime(2026, 4, 23, 15, 0, tzinfo=timezone.utc),
        exit_time=None,
        leg_status="open",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestLegToDict:
    def test_short_leg_pnl_positive_when_price_drops(self):
        """Short premium collected — current price lower = profit."""
        from dashboard.routes.trades import _leg_to_dict
        l = _leg(direction="SHORT", entry_price=2.50, current_price=2.00,
                 contracts_entered=1, multiplier=100)
        d = _leg_to_dict(l)
        # sign=-1, (2.00 - 2.50) * 1 * 100 * -1 = +50
        assert d["pnl_usd"] == pytest.approx(50.0)
        assert d["direction"] == "SHORT"
        assert d["entry_price"] == pytest.approx(2.50)
        assert d["current_price"] == pytest.approx(2.00)

    def test_long_leg_pnl_positive_when_price_rises(self):
        from dashboard.routes.trades import _leg_to_dict
        l = _leg(direction="LONG", entry_price=1.00, current_price=1.75,
                 contracts_entered=2, multiplier=100)
        d = _leg_to_dict(l)
        # (1.75 - 1.00) * 2 * 100 * +1 = +150
        assert d["pnl_usd"] == pytest.approx(150.0)

    def test_uses_exit_price_over_current_price_when_present(self):
        from dashboard.routes.trades import _leg_to_dict
        l = _leg(direction="LONG", entry_price=1.00,
                 current_price=2.00, exit_price=1.50,
                 contracts_entered=1, multiplier=100)
        d = _leg_to_dict(l)
        # Should use exit_price (1.50), not current_price (2.00)
        assert d["pnl_usd"] == pytest.approx(50.0)
        assert d["exit_price"] == pytest.approx(1.50)

    def test_no_close_price_returns_none_pnl(self):
        from dashboard.routes.trades import _leg_to_dict
        l = _leg(entry_price=2.50, current_price=None, exit_price=None)
        d = _leg_to_dict(l)
        assert d["pnl_usd"] is None

    def test_shape_contains_every_writer_schema_column(self):
        """UI relies on these keys — lock them in."""
        from dashboard.routes.trades import _leg_to_dict
        d = _leg_to_dict(_leg())
        required = {
            "leg_id", "trade_id", "leg_index", "leg_role",
            "sec_type", "symbol", "underlying", "strike", "right", "expiry",
            "multiplier", "exchange", "currency",
            "direction", "contracts_entered", "contracts_open", "contracts_closed",
            "entry_price", "exit_price", "current_price", "ib_fill_price",
            "profit_target", "stop_loss_level",
            "ib_order_id", "ib_perm_id", "ib_con_id",
            "ib_tp_order_id", "ib_tp_perm_id", "ib_tp_status", "ib_tp_price",
            "ib_sl_order_id", "ib_sl_perm_id", "ib_sl_status", "ib_sl_price",
            "entry_time", "exit_time", "leg_status", "pnl_usd",
        }
        missing = required - set(d.keys())
        assert not missing, f"Leg dict missing keys: {missing}"


class TestGetTradeLegsEndpoint:
    """The /api/trades/{id}/legs FastAPI route."""

    def _fake_trade(self, legs):
        trade = MagicMock()
        trade.ticker = "SPY"
        trade.legs = legs
        trade.n_legs = len(legs)
        return trade

    def test_returns_legs_ordered_by_leg_index(self):
        from dashboard.routes.trades import get_trade_legs

        # Out-of-order legs to prove sort happens server-side
        legs_unsorted = [
            _leg(leg_id=3, leg_index=2, leg_role="short_put"),
            _leg(leg_id=1, leg_index=0, leg_role="short_call"),
            _leg(leg_id=4, leg_index=3, leg_role="long_put"),
            _leg(leg_id=2, leg_index=1, leg_role="long_call"),
        ]
        trade = self._fake_trade(legs_unsorted)

        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = trade
        with patch("dashboard.routes.trades.get_session", return_value=session):
            resp = get_trade_legs(42)

        assert resp["trade_id"] == 42
        assert resp["ticker"] == "SPY"
        assert resp["n_legs"] == 4
        roles = [l["leg_role"] for l in resp["legs"]]
        assert roles == ["short_call", "long_call", "short_put", "long_put"]
        indexes = [l["leg_index"] for l in resp["legs"]]
        assert indexes == [0, 1, 2, 3]

    def test_404_when_trade_not_found(self):
        from fastapi import HTTPException
        from dashboard.routes.trades import get_trade_legs

        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = None
        with patch("dashboard.routes.trades.get_session", return_value=session):
            with pytest.raises(HTTPException) as exc:
                get_trade_legs(999)
        assert exc.value.status_code == 404

    def test_503_when_db_unavailable(self):
        from fastapi import HTTPException
        from dashboard.routes.trades import get_trade_legs

        with patch("dashboard.routes.trades.get_session", return_value=None):
            with pytest.raises(HTTPException) as exc:
                get_trade_legs(1)
        assert exc.value.status_code == 503
