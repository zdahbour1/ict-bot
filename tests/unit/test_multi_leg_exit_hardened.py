"""ENH-065 (Phase 1 of multi-leg completion, 2026-04-24)

Hardens _execute_multi_leg_exit so combo-close is AUTHORITATIVE:
  - Retries up to 3× with 2s backoff (quotes refresh between tries)
  - On all-retries-exhausted: escalates critical alert, leaves trade
    open for operator / next cycle. Does NOT silently fall through
    to per-leg SELLs (creates naked-short windows on iron condor).
  - STK hedge legs are excluded from the BAG (Phase 3 owns stock
    flatten separately).
  - Per-leg fallback only reachable when operator explicitly sets
    USE_COMBO_ORDERS_FOR_MULTI_LEG=false.

See docs/multi_leg_completion_plan.md § Phase 1.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _trade(**overrides):
    t = {
        "ticker": "SPY", "db_id": 42,
        "client_trade_id": "dn-SPY-260501-01",
        "n_legs": 4, "direction": "LONG",
        "ib_client_id": 1,
    }
    t.update(overrides)
    return t


def _four_leg_rows():
    """Shape returned by _fetch_open_legs — 4-leg iron butterfly."""
    return [
        {"leg_index": 0, "leg_role": "short_call", "sec_type": "OPT",
         "symbol": "SPY260501C00500000", "direction": "SHORT",
         "contracts_open": 2, "strike": 500.0, "right": "C",
         "expiry": "20260501", "multiplier": 100, "underlying": "SPY",
         "ib_tp_perm_id": 111, "ib_sl_perm_id": 112},
        {"leg_index": 1, "leg_role": "long_call", "sec_type": "OPT",
         "symbol": "SPY260501C00510000", "direction": "LONG",
         "contracts_open": 2, "strike": 510.0, "right": "C",
         "expiry": "20260501", "multiplier": 100, "underlying": "SPY"},
        {"leg_index": 2, "leg_role": "short_put", "sec_type": "OPT",
         "symbol": "SPY260501P00500000", "direction": "SHORT",
         "contracts_open": 2, "strike": 500.0, "right": "P",
         "expiry": "20260501", "multiplier": 100, "underlying": "SPY"},
        {"leg_index": 3, "leg_role": "long_put", "sec_type": "OPT",
         "symbol": "SPY260501P00490000", "direction": "LONG",
         "contracts_open": 2, "strike": 490.0, "right": "P",
         "expiry": "20260501", "multiplier": 100, "underlying": "SPY"},
    ]


class TestComboCloseHappyPath:
    def test_first_attempt_success_returns_none_and_issues_one_bag(self):
        """Combo close fills on first try → one BAG placed, no leg
        SELLs issued."""
        from strategy import exit_executor
        client = MagicMock()
        client.place_combo_close_order.return_value = {
            "all_filled": True,
            "combo_order_id": 9999,
            "net_fill_price": -2.50,   # credit close
            "legs": [{}, {}, {}, {}],
        }
        with patch.object(exit_executor, "_fetch_open_legs",
                          return_value=_four_leg_rows()), \
             patch("db.settings_cache.get_bool", return_value=True):
            ret = exit_executor._execute_multi_leg_exit(
                client, _trade(), reason="TEST")
        assert ret is None
        assert client.place_combo_close_order.call_count == 1
        # No per-leg SELLs
        client.sell_call.assert_not_called()
        client.sell_put.assert_not_called()


class TestComboCloseRetries:
    def test_retries_up_to_three_times_on_partial_fill(self):
        from strategy import exit_executor
        client = MagicMock()
        # Fail, fail, succeed
        client.place_combo_close_order.side_effect = [
            {"all_filled": False, "legs": [{"status": "Submitted"}]},
            {"all_filled": False, "legs": [{"status": "Submitted"}]},
            {"all_filled": True, "combo_order_id": 1, "net_fill_price": -2.0,
             "legs": [{}] * 4},
        ]
        with patch.object(exit_executor, "_fetch_open_legs",
                          return_value=_four_leg_rows()), \
             patch("db.settings_cache.get_bool", return_value=True), \
             patch("time.sleep"):  # skip the 2s backoff in test
            ret = exit_executor._execute_multi_leg_exit(
                client, _trade(), reason="TEST")
        assert ret is None
        assert client.place_combo_close_order.call_count == 3
        client.sell_call.assert_not_called()
        client.sell_put.assert_not_called()

    def test_retries_on_raise(self):
        """RuntimeError (e.g., quote failure) counts as a retryable
        attempt, not a hard failure."""
        from strategy import exit_executor
        client = MagicMock()
        client.place_combo_close_order.side_effect = [
            RuntimeError("combo limit acquisition failed"),
            {"all_filled": True, "combo_order_id": 1,
             "net_fill_price": -2.0, "legs": [{}] * 4},
        ]
        with patch.object(exit_executor, "_fetch_open_legs",
                          return_value=_four_leg_rows()), \
             patch("db.settings_cache.get_bool", return_value=True), \
             patch("time.sleep"):
            ret = exit_executor._execute_multi_leg_exit(
                client, _trade(), reason="TEST")
        assert ret is None
        assert client.place_combo_close_order.call_count == 2


class TestComboCloseExhaustedEscalates:
    def test_all_retries_fail_no_per_leg_fallback(self):
        """Critical: after max retries, must NOT fall through to
        per-leg SELLs. Defined-risk safety."""
        from strategy import exit_executor
        client = MagicMock()
        client.place_combo_close_order.return_value = {
            "all_filled": False,
            "legs": [{"status": "Submitted"}],
        }
        with patch.object(exit_executor, "_fetch_open_legs",
                          return_value=_four_leg_rows()), \
             patch("db.settings_cache.get_bool", return_value=True), \
             patch("time.sleep"), \
             patch("strategy.error_handler.handle_error") as mock_err:
            ret = exit_executor._execute_multi_leg_exit(
                client, _trade(), reason="TEST")

        assert ret is None
        # Tried MAX_ATTEMPTS times
        assert client.place_combo_close_order.call_count == 3
        # CRITICAL: no per-leg close fallback
        client.sell_call.assert_not_called()
        client.sell_put.assert_not_called()
        # Critical alert was fired
        assert mock_err.called
        _, kwargs = mock_err.call_args
        assert kwargs.get("critical") is True

    def test_all_retries_raise_escalates_with_last_error(self):
        from strategy import exit_executor
        client = MagicMock()
        err = RuntimeError("quote feed down")
        client.place_combo_close_order.side_effect = [err, err, err]
        with patch.object(exit_executor, "_fetch_open_legs",
                          return_value=_four_leg_rows()), \
             patch("db.settings_cache.get_bool", return_value=True), \
             patch("time.sleep"), \
             patch("strategy.error_handler.handle_error") as mock_err:
            ret = exit_executor._execute_multi_leg_exit(
                client, _trade(), reason="TEST")
        assert ret is None
        assert mock_err.called
        # Error passed through for telemetry
        args, _ = mock_err.call_args
        # args[2] is the exception — must be our RuntimeError
        assert isinstance(args[2], RuntimeError)


class TestStockLegsExcludedFromBag:
    def test_stk_hedge_leg_not_included_in_combo(self):
        """A delta-hedged trade may have a STK hedge stored alongside
        option legs (Phase B). Combo close must filter it out — IB
        doesn't accept a BAG that mixes OPT + STK."""
        from strategy import exit_executor
        client = MagicMock()
        captured = {}

        def _capture(legs, order_ref=None, limit_price=None):
            captured["legs"] = legs
            return {"all_filled": True, "combo_order_id": 1,
                    "net_fill_price": -2.0, "legs": [{}] * 4}

        client.place_combo_close_order.side_effect = _capture
        legs = _four_leg_rows() + [{
            "leg_index": 4, "leg_role": "delta_hedge", "sec_type": "STK",
            "symbol": "SPY", "direction": "LONG", "contracts_open": 10,
            "strike": None, "right": None, "expiry": None,
            "multiplier": 1, "underlying": "SPY",
        }]
        with patch.object(exit_executor, "_fetch_open_legs",
                          return_value=legs), \
             patch("db.settings_cache.get_bool", return_value=True):
            exit_executor._execute_multi_leg_exit(
                client, _trade(n_legs=5), reason="TEST")

        sec_types = {(l.get("sec_type") or "OPT").upper()
                      for l in captured["legs"]}
        assert sec_types == {"OPT"}, (
            f"STK leg must be excluded from the combo BAG, got "
            f"secTypes={sec_types}"
        )
        assert len(captured["legs"]) == 4


class TestNoLegsEdgeCase:
    def test_no_open_legs_returns_early(self):
        from strategy import exit_executor
        client = MagicMock()
        with patch.object(exit_executor, "_fetch_open_legs",
                          return_value=[]):
            ret = exit_executor._execute_multi_leg_exit(
                client, _trade(), reason="TEST")
        assert ret is None
        client.place_combo_close_order.assert_not_called()
