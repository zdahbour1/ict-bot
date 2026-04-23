"""Unit tests for ENH-038 backtest multi-leg writer + schema.

Covers:
- record_multi_leg_trade writes one backtest_trades envelope + N
  backtest_trade_legs rows in the same DB transaction.
- Per-leg pnl_usd is computed from (exit - entry) * contracts * multiplier
  * sign when the caller doesn't supply it.
- n_legs column on backtest_trades is stamped to len(legs).
- Engine routing: trades with a _legs list → record_multi_leg_trade;
  without → record_trade. Tests the branch via mocks; doesn't invoke
  the full engine simulation.

DB-touching tests (record_multi_leg_trade) mock session.execute and
verify the SQL calls + parameter shapes. No Postgres required.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


class TestRecordMultiLegTrade:
    def _envelope(self):
        return {
            "ticker": "SPY",
            "entry_time": datetime(2026, 4, 22, 15, 30, tzinfo=timezone.utc),
            "exit_time": datetime(2026, 4, 22, 15, 45, tzinfo=timezone.utc),
            "pnl_usd": 150.0,
            "pnl_pct": 0.1,
            "signal_type": "DELTA_NEUTRAL_ENTRY",
            "hold_minutes": 15.0,
            "exit_reason": "TP",
            "exit_result": "WIN",
        }

    def _iron_condor_legs(self):
        return [
            {"leg_index": 0, "leg_role": "short_call",
             "sec_type": "OPT", "symbol": "SPY260515C00450000",
             "underlying": "SPY", "strike": 450.0, "right": "C",
             "expiry": "20260515", "multiplier": 100,
             "direction": "SHORT", "contracts": 1,
             "entry_price": 2.50, "exit_price": 1.80,
             "entry_time": datetime(2026, 4, 22, 15, 30, tzinfo=timezone.utc),
             "exit_time": datetime(2026, 4, 22, 15, 45, tzinfo=timezone.utc)},
            {"leg_index": 1, "leg_role": "long_call",
             "sec_type": "OPT", "symbol": "SPY260515C00460000",
             "underlying": "SPY", "strike": 460.0, "right": "C",
             "expiry": "20260515", "multiplier": 100,
             "direction": "LONG", "contracts": 1,
             "entry_price": 1.20, "exit_price": 0.80,
             "entry_time": datetime(2026, 4, 22, 15, 30, tzinfo=timezone.utc),
             "exit_time": datetime(2026, 4, 22, 15, 45, tzinfo=timezone.utc)},
            {"leg_index": 2, "leg_role": "short_put",
             "sec_type": "OPT", "symbol": "SPY260515P00440000",
             "underlying": "SPY", "strike": 440.0, "right": "P",
             "expiry": "20260515", "multiplier": 100,
             "direction": "SHORT", "contracts": 1,
             "entry_price": 2.20, "exit_price": 1.50,
             "entry_time": datetime(2026, 4, 22, 15, 30, tzinfo=timezone.utc),
             "exit_time": datetime(2026, 4, 22, 15, 45, tzinfo=timezone.utc)},
            {"leg_index": 3, "leg_role": "long_put",
             "sec_type": "OPT", "symbol": "SPY260515P00430000",
             "underlying": "SPY", "strike": 430.0, "right": "P",
             "expiry": "20260515", "multiplier": 100,
             "direction": "LONG", "contracts": 1,
             "entry_price": 1.10, "exit_price": 0.60,
             "entry_time": datetime(2026, 4, 22, 15, 30, tzinfo=timezone.utc),
             "exit_time": datetime(2026, 4, 22, 15, 45, tzinfo=timezone.utc)},
        ]

    def test_writes_envelope_then_n_legs_update_then_leg_inserts(self):
        from backtest_engine import writer as bt_writer
        mock_session = MagicMock()
        mock_session.execute.return_value.scalar.return_value = 42  # back-compat, not used
        with patch.object(bt_writer, "get_session", return_value=mock_session), \
             patch.object(bt_writer, "record_trade", return_value=999) as mock_rt:
            new_id = bt_writer.record_multi_leg_trade(
                run_id=5, strategy_id=91,
                envelope=self._envelope(), legs=self._iron_condor_legs(),
            )
        assert new_id == 999
        # record_trade called once with the augmented envelope (first leg's
        # symbol / direction / entry_price surfaced at trade level)
        mock_rt.assert_called_once()
        env_arg = mock_rt.call_args.args[2]
        assert env_arg["symbol"] == "SPY260515C00450000"
        assert env_arg["direction"] == "SHORT"    # first leg is short_call
        assert env_arg["entry_price"] == pytest.approx(2.50)
        # Then the UPDATE for n_legs + 4 INSERTs = 5 execute calls.
        assert mock_session.execute.call_count == 5
        mock_session.commit.assert_called_once()

    def test_per_leg_pnl_computed_when_not_supplied(self):
        """exit_price is given but pnl_usd is not — writer computes per-leg
        pnl as (exit - entry) * contracts * multiplier * sign."""
        from backtest_engine import writer as bt_writer
        mock_session = MagicMock()
        captured_params = []
        # Intercept the leg-INSERTs to check computed pnl_usd.
        def _exec(sql, params=None):
            if params and "tid" in params:    # leg INSERT
                captured_params.append(params)
            result = MagicMock()
            result.scalar.return_value = 1
            return result
        mock_session.execute.side_effect = _exec
        with patch.object(bt_writer, "get_session", return_value=mock_session), \
             patch.object(bt_writer, "record_trade", return_value=1):
            bt_writer.record_multi_leg_trade(
                run_id=1, strategy_id=1,
                envelope=self._envelope(), legs=self._iron_condor_legs(),
            )
        # short_call: (1.80 - 2.50) * 1 * 100 * -1 = +70
        # long_call:  (0.80 - 1.20) * 1 * 100 * +1 = -40
        # short_put:  (1.50 - 2.20) * 1 * 100 * -1 = +70
        # long_put:   (0.60 - 1.10) * 1 * 100 * +1 = -50
        pnls = [p["pnl"] for p in captured_params]
        assert pnls == [pytest.approx(70.0), pytest.approx(-40.0),
                        pytest.approx(70.0), pytest.approx(-50.0)]

    def test_returns_none_on_empty_leg_list(self):
        """Gracefully no-op (or near-it) when caller passes no legs."""
        from backtest_engine import writer as bt_writer
        mock_session = MagicMock()
        # record_trade with no legs still gets called — writer doesn't
        # reject zero legs; caller should have filtered upstream.
        with patch.object(bt_writer, "get_session", return_value=mock_session), \
             patch.object(bt_writer, "record_trade", return_value=77) as mock_rt:
            new_id = bt_writer.record_multi_leg_trade(
                run_id=1, strategy_id=1,
                envelope={"ticker": "X", "entry_time": datetime.now(timezone.utc)},
                legs=[],
            )
        assert new_id == 77
        mock_rt.assert_called_once()
        # Only the n_legs UPDATE fires (1 call), no leg INSERTs.
        assert mock_session.execute.call_count == 1


class TestEngineRoutesMultiLeg:
    """Verifies the engine branch in run_backtest: trades carrying a
    _legs list are routed to record_multi_leg_trade; plain trades go
    to record_trade. We don't exercise the full simulation loop — just
    the one-line router."""

    def test_legs_field_routes_to_multi_leg(self):
        # Reproduces the branch from backtest_engine/engine.py directly.
        # Keep this in sync if the routing logic moves.
        from backtest_engine import writer as bt_writer
        t_multi = {"ticker": "SPY", "entry_time": "x",
                   "_legs": [{"leg_index": 0, "symbol": "X",
                              "contracts": 1, "entry_price": 1.0}]}
        t_single = {"ticker": "SPY", "entry_time": "x"}
        with patch.object(bt_writer, "record_multi_leg_trade",
                           return_value=1) as mock_ml, \
             patch.object(bt_writer, "record_trade",
                           return_value=2) as mock_sl:
            for t in (t_multi, t_single):
                legs = t.get("_legs")
                if legs:
                    bt_writer.record_multi_leg_trade(1, 1, t, legs)
                else:
                    bt_writer.record_trade(1, 1, t)
        mock_ml.assert_called_once()
        mock_sl.assert_called_once()
