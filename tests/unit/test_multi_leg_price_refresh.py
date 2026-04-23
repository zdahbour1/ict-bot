"""Regression for 2026-04-23 multi-leg price-stale bug.

User-reported: on delta-neutral iron-condor trades, only leg 0's
``current_price`` was being updated by exit_manager's monitor loop.
Legs 1-3 stayed at entry_price forever, which broke per-leg P&L, the
leg-drill-down UI, and the delta-hedger's share-equivalent math.

Root cause: ``update_trade_price`` WHERE clause was
``leg_index = 0 AND leg_status = 'open'``. This module locks in the
fix — ``update_all_leg_prices(trade_id, {symbol: price})`` updates
every matching leg, and ``_refresh_multi_leg_prices`` in
exit_manager drives it for every multi-leg open trade.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestUpdateAllLegPrices:
    def _bypass_safe_db(self, writer):
        # The @_safe_db decorator returns None when the module-level
        # _db_checked global is False. Force it True so our function
        # actually runs under test.
        return patch.object(writer, "_db_checked", True)

    def test_updates_every_matching_leg(self):
        """Feeding a {symbol: price} dict must issue one UPDATE per
        symbol, each scoped by trade_id + leg_status='open'."""
        from db import writer

        session = MagicMock()
        session.execute.return_value.rowcount = 1
        with self._bypass_safe_db(writer), \
             patch.object(writer, "get_session", return_value=session):
            n = writer.update_all_leg_prices(
                trade_id=42,
                price_by_symbol={
                    "SPY260515C00500000": 2.10,
                    "SPY260515C00510000": 0.85,
                    "SPY260515P00500000": 1.75,
                    "SPY260515P00490000": 0.60,
                },
            )
        # 4 symbols → 4 execute calls
        assert session.execute.call_count == 4
        # Each call is scoped by trade_id + symbol + leg_status='open'
        for call in session.execute.call_args_list:
            sql = str(call.args[0])
            assert "leg_status='open'" in sql
            params = call.args[1] if len(call.args) > 1 else (call.kwargs.get("params") or {})
            assert params["id"] == 42
            assert "sym" in params
        assert n == 4

    def test_skips_none_prices(self):
        from db import writer
        session = MagicMock()
        session.execute.return_value.rowcount = 1
        with self._bypass_safe_db(writer), \
             patch.object(writer, "get_session", return_value=session):
            n = writer.update_all_leg_prices(
                trade_id=1, price_by_symbol={"A": 1.0, "B": None, "C": 2.0},
            )
        assert session.execute.call_count == 2

    def test_empty_map_is_noop(self):
        from db import writer
        with self._bypass_safe_db(writer), \
             patch.object(writer, "get_session", return_value=MagicMock()):
            n = writer.update_all_leg_prices(trade_id=1, price_by_symbol={})
        assert n == 0


class TestRefreshMultiLegPrices:
    def test_noop_when_no_multi_leg_trades(self):
        from strategy.exit_manager import _refresh_multi_leg_prices
        client = MagicMock()
        single_leg_trades = [
            {"db_id": 1, "n_legs": 1},
            {"db_id": 2, "n_legs": 1},
        ]
        # Should not hit DB at all
        with patch("db.connection.get_session") as gs:
            _refresh_multi_leg_prices(client, single_leg_trades, {})
            gs.assert_not_called()

    def test_queries_legs_for_multi_leg_trades(self):
        """When an iron condor is in the list, the helper should
        fetch each leg's symbol from trade_legs (leg_index > 0)."""
        from strategy import exit_manager as em
        client = MagicMock()
        client.get_option_prices_batch = MagicMock(return_value={})

        # Fake session whose trade_legs query returns 3 symbols (legs 1-3)
        session = MagicMock()
        session.execute.return_value.fetchall.return_value = [
            ("SPY260515C00510000",), ("SPY260515P00500000",),
            ("SPY260515P00490000",),
        ]
        with patch("db.connection.get_session", return_value=session), \
             patch("db.writer.update_all_leg_prices") as up:
            em._refresh_multi_leg_prices(
                client,
                [{"db_id": 77, "n_legs": 4}],
                {"SPY260515C00510000": 0.85},  # one already priced
            )
        # client.get_option_prices_batch was called for the missing symbols
        client.get_option_prices_batch.assert_called()
        # And update_all_leg_prices was called with trade_id=77
        assert up.called
        assert up.call_args.args[0] == 77
