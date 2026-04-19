"""
Unit tests for strategy/exit_conditions.py — pure exit logic.

Covers:
- Trailing stop milestone updates
- TP → trail conversion
- Roll trigger at threshold
- evaluate_exit() decision tree: TP, SL, TRAIL_STOP, ROLL, TIME_EXIT, EOD_EXIT
- P&L classification: WIN / LOSS / SCRATCH
"""
import pytest
from datetime import datetime, timedelta
import pytz

import config
from strategy.exit_conditions import (
    update_trailing_stop,
    check_tp_to_trail,
    check_roll_condition,
    evaluate_exit,
)

PT = pytz.timezone("America/Los_Angeles")


@pytest.fixture
def base_trade():
    """A baseline open trade dict — entry 5 min before the test's 10:00 PT
    reference clock (see TestEvaluateExit._now). Uses an explicit time so
    bars_held is deterministic and not wall-clock dependent."""
    return {
        "ticker": "QQQ",
        "entry_price": 2.00,
        "entry_time": datetime.now(PT).replace(hour=9, minute=55, second=0,
                                               microsecond=0) - timedelta(0),
        "peak_pnl_pct": 0.0,
        "dynamic_sl_pct": -0.60,
        "contracts": 2,
    }


@pytest.fixture
def cfg(monkeypatch):
    """Pin config values so tests don't depend on DB/env."""
    monkeypatch.setattr(config, "PROFIT_TARGET", 1.00)
    monkeypatch.setattr(config, "STOP_LOSS", 0.60)
    monkeypatch.setattr(config, "TP_TO_TRAIL", False)
    monkeypatch.setattr(config, "ROLL_ENABLED", False)
    monkeypatch.setattr(config, "ROLL_THRESHOLD", 0.70)
    return config


# ── update_trailing_stop ─────────────────────────────────

class TestUpdateTrailingStop:
    def test_no_peak_returns_default_sl(self, base_trade, cfg):
        base_trade["peak_pnl_pct"] = 0.05
        assert update_trailing_stop(base_trade, 0.05) == -0.60

    def test_at_10pct_peak_trails(self, base_trade, cfg):
        base_trade["peak_pnl_pct"] = 0.10
        # steps=1, trail_base=0.10, SL=0.60 → -0.50
        assert update_trailing_stop(base_trade, 0.10) == pytest.approx(-0.50)

    def test_at_40pct_peak_trails(self, base_trade, cfg):
        base_trade["peak_pnl_pct"] = 0.40
        # steps=4, trail_base=0.40 → -0.20
        assert update_trailing_stop(base_trade, 0.40) == pytest.approx(-0.20)

    def test_monotonic_across_milestones(self, base_trade, cfg):
        """SL should move up (toward 0) as peak crosses each 10% milestone."""
        # Use well-separated peaks to avoid float rounding at boundaries
        prior = -1.0
        for peak in [0.11, 0.21, 0.41, 0.51]:
            base_trade["peak_pnl_pct"] = peak
            sl = update_trailing_stop(base_trade, peak)
            assert sl > prior
            prior = sl


# ── check_tp_to_trail ────────────────────────────────────

class TestCheckTpToTrail:
    def test_disabled_returns_false(self, base_trade, cfg, monkeypatch):
        monkeypatch.setattr(config, "TP_TO_TRAIL", False)
        assert check_tp_to_trail(base_trade, 1.50, 2.00) is False

    def test_below_tp_no_conversion(self, base_trade, cfg, monkeypatch):
        monkeypatch.setattr(config, "TP_TO_TRAIL", True)
        assert check_tp_to_trail(base_trade, 0.50, 2.00) is False
        assert not base_trade.get("_tp_trailed")

    def test_at_tp_converts(self, base_trade, cfg, monkeypatch):
        monkeypatch.setattr(config, "TP_TO_TRAIL", True)
        assert check_tp_to_trail(base_trade, 1.00, 2.00) is True
        assert base_trade["_tp_trailed"] is True
        # new SL = TP - STOP_LOSS = 1.00 - 0.60 = 0.40
        assert base_trade["dynamic_sl_pct"] == pytest.approx(0.40)

    def test_idempotent_once_converted(self, base_trade, cfg, monkeypatch):
        monkeypatch.setattr(config, "TP_TO_TRAIL", True)
        check_tp_to_trail(base_trade, 1.00, 2.00)
        # Second call should not re-trigger
        assert check_tp_to_trail(base_trade, 1.20, 2.00) is False


# ── check_roll_condition ─────────────────────────────────

class TestCheckRollCondition:
    def test_disabled_returns_false(self, base_trade, cfg):
        assert check_roll_condition(base_trade, 0.80) is False

    def test_below_threshold_no_roll(self, base_trade, cfg, monkeypatch):
        monkeypatch.setattr(config, "ROLL_ENABLED", True)
        # ROLL_THRESHOLD * PROFIT_TARGET = 0.70 * 1.00 = 0.70
        assert check_roll_condition(base_trade, 0.50) is False

    def test_at_threshold_rolls(self, base_trade, cfg, monkeypatch):
        monkeypatch.setattr(config, "ROLL_ENABLED", True)
        assert check_roll_condition(base_trade, 0.70) is True
        assert base_trade["_rolled"] is True
        assert base_trade["_should_roll"] is True

    def test_already_rolled_no_repeat(self, base_trade, cfg, monkeypatch):
        monkeypatch.setattr(config, "ROLL_ENABLED", True)
        base_trade["_rolled"] = True
        assert check_roll_condition(base_trade, 0.90) is False


# ── evaluate_exit ────────────────────────────────────────

class TestEvaluateExit:
    def _now(self, hour=10):
        """Market hours in PT (default 10 AM)."""
        return datetime.now(PT).replace(hour=hour, minute=0, second=0, microsecond=0)

    def test_no_exit_when_flat(self, base_trade, cfg):
        # current == entry, no peak, not EOD
        result = evaluate_exit(base_trade, 2.00, self._now(10))
        assert result is None

    def test_invalid_entry_returns_none(self, base_trade, cfg):
        base_trade["entry_price"] = 0
        assert evaluate_exit(base_trade, 2.00, self._now(10)) is None

    def test_hit_tp(self, base_trade, cfg):
        # price doubled → +100% = TP
        result = evaluate_exit(base_trade, 4.00, self._now(10))
        assert result is not None
        assert result["reason"] == "TP"
        assert result["result"] == "WIN"

    def test_hit_sl(self, base_trade, cfg):
        # -60% → SL
        result = evaluate_exit(base_trade, 0.80, self._now(10))
        assert result is not None
        assert result["reason"] == "SL"
        assert result["result"] == "LOSS"

    def test_trail_stop_after_runup(self, base_trade, cfg):
        # Previous peak at 40% → SL trails to -0.20
        base_trade["peak_pnl_pct"] = 0.40
        base_trade["dynamic_sl_pct"] = -0.20
        # Current P&L -25%: hit trailed SL but SL > -STOP_LOSS
        result = evaluate_exit(base_trade, 1.50, self._now(10))
        assert result is not None
        assert result["reason"] == "TRAIL_STOP"
        assert result["result"] == "LOSS"

    def test_roll_triggers_win(self, base_trade, cfg, monkeypatch):
        monkeypatch.setattr(config, "ROLL_ENABLED", True)
        # +70% = ROLL_THRESHOLD * PROFIT_TARGET
        result = evaluate_exit(base_trade, 3.40, self._now(10))
        assert result is not None
        assert result["reason"] == "ROLL"
        assert result["result"] == "WIN"
        assert result["should_roll"] is True

    def test_time_exit_after_90min(self, base_trade, cfg):
        # Entry at 08:00 PT, now is 10:00 PT → held 120 min, past the 90 cap
        base_trade["entry_time"] = self._now(10) - timedelta(minutes=120)
        result = evaluate_exit(base_trade, 2.10, self._now(10))
        assert result is not None
        assert result["reason"] == "TIME_EXIT"

    def test_eod_exit_at_13(self, base_trade, cfg):
        # Enter a few minutes before 13:00 so TIME_EXIT doesn't fire first
        base_trade["entry_time"] = self._now(13) - timedelta(minutes=10)
        result = evaluate_exit(base_trade, 2.20, self._now(13))
        assert result is not None
        assert result["reason"] == "EOD_EXIT"
        assert result["result"] == "WIN"

    def test_eod_loss_classification(self, base_trade, cfg):
        base_trade["entry_time"] = self._now(13) - timedelta(minutes=10)
        result = evaluate_exit(base_trade, 1.80, self._now(13))
        assert result is not None
        assert result["reason"] == "EOD_EXIT"
        assert result["result"] == "LOSS"

    def test_tp_wins_over_eod(self, base_trade, cfg):
        # Both TP and EOD true → TP takes priority (checked first)
        base_trade["entry_time"] = self._now(13) - timedelta(minutes=10)
        result = evaluate_exit(base_trade, 4.00, self._now(13))
        assert result["reason"] == "TP"

    def test_peak_pnl_updates(self, base_trade, cfg):
        evaluate_exit(base_trade, 2.40, self._now(10))  # +20%
        assert base_trade["peak_pnl_pct"] == pytest.approx(0.20)

    def test_peak_not_reduced_by_pullback(self, base_trade, cfg):
        base_trade["peak_pnl_pct"] = 0.30
        evaluate_exit(base_trade, 2.10, self._now(10))  # +5%
        assert base_trade["peak_pnl_pct"] == pytest.approx(0.30)

    def test_tp_to_trail_suppresses_tp_exit(self, base_trade, cfg, monkeypatch):
        monkeypatch.setattr(config, "TP_TO_TRAIL", True)
        # +100% — normally TP, but with TP_TO_TRAIL the TP is converted to trail
        result = evaluate_exit(base_trade, 4.00, self._now(10))
        # Should NOT exit on TP (converted instead)
        if result is not None:
            assert result["reason"] != "TP"
