"""
Unit tests for backtest_engine/metrics.py and fill_model.py (ENH-019).
Pure-function tests — no DB.
"""
import pytest

from backtest_engine.metrics import compute_summary, BacktestSummary
from backtest_engine.fill_model import (
    FillConfig, simulate_entry_fill, simulate_exit_fill, compute_pnl,
)


def _t(pnl, result=None, hold=None):
    d = {"pnl_usd": pnl}
    if result is not None:
        d["exit_result"] = result
    if hold is not None:
        d["hold_minutes"] = hold
    return d


# ── Metrics ──────────────────────────────────────────────

class TestComputeSummary:
    def test_empty_returns_zero(self):
        s = compute_summary([])
        assert s.total_trades == 0
        assert s.total_pnl == 0.0
        assert s.sharpe_ratio is None
        assert s.profit_factor is None

    def test_basic(self):
        trades = [_t(100, "WIN", 30), _t(-50, "LOSS", 45), _t(200, "WIN", 60)]
        s = compute_summary(trades)
        assert s.total_trades == 3
        assert s.wins == 2 and s.losses == 1
        assert s.total_pnl == 250.0
        assert s.win_rate == pytest.approx(66.67, abs=0.01)
        assert s.avg_hold_min == 45.0

    def test_scratches_excluded_from_win_rate(self):
        s = compute_summary([_t(100, "WIN"), _t(0, "SCRATCH"), _t(0, "SCRATCH")])
        assert s.win_rate == 100.0
        assert s.scratches == 2

    def test_profit_factor(self):
        s = compute_summary([_t(100), _t(200), _t(-50), _t(-50)])
        assert s.profit_factor == pytest.approx(3.0)

    def test_profit_factor_none_when_no_losses(self):
        s = compute_summary([_t(100), _t(50)])
        assert s.profit_factor is None

    def test_drawdown(self):
        # Cum: +500, +800, +300, +900 → peak=800, trough=300 → dd=-500
        s = compute_summary([_t(500), _t(300), _t(-500), _t(600)])
        assert s.max_drawdown == -500.0

    def test_streaks(self):
        t = [_t(10, "WIN"), _t(10, "WIN"), _t(10, "WIN"),
             _t(-10, "LOSS"), _t(-10, "LOSS"),
             _t(10, "WIN")]
        s = compute_summary(t)
        assert s.max_win_streak == 3
        assert s.max_loss_streak == 2

    def test_sharpe_single_trade_is_none(self):
        assert compute_summary([_t(100)]).sharpe_ratio is None

    def test_sharpe_zero_variance(self):
        assert compute_summary([_t(50), _t(50)]).sharpe_ratio is None

    def test_to_dict_matches_db_columns(self):
        d = BacktestSummary().to_dict()
        required = {"total_trades", "wins", "losses", "scratches", "total_pnl",
                    "win_rate", "avg_win", "avg_loss", "max_drawdown",
                    "sharpe_ratio", "profit_factor", "avg_hold_min",
                    "max_win_streak", "max_loss_streak"}
        assert required.issubset(d.keys())


# ── Fill model ───────────────────────────────────────────

class TestFillModel:
    def test_long_entry_slippage_up(self):
        r = simulate_entry_fill(100.0, 2, "LONG", FillConfig(slippage_pct=0.01))
        assert r["fill_price"] == pytest.approx(101.0)
        assert r["commission"] == pytest.approx(1.30)

    def test_short_entry_slippage_down(self):
        r = simulate_entry_fill(100.0, 2, "SHORT", FillConfig(slippage_pct=0.01))
        assert r["fill_price"] == pytest.approx(99.0)

    def test_long_exit_receives_less(self):
        r = simulate_exit_fill(100.0, 2, "LONG", FillConfig(slippage_pct=0.01))
        assert r["fill_price"] == pytest.approx(99.0)

    def test_long_winner_pnl(self):
        r = compute_pnl(2.0, 4.0, 2, "LONG", total_commission=0)
        assert r["pnl_usd"] == 400.0
        assert r["pnl_pct"] == 1.0

    def test_short_winner_pnl(self):
        r = compute_pnl(2.0, 1.0, 2, "SHORT", total_commission=0)
        assert r["pnl_usd"] == 200.0
        assert r["pnl_pct"] == pytest.approx(0.5)

    def test_commission_deducted(self):
        r = compute_pnl(2.0, 4.0, 2, "LONG", total_commission=2.6)
        assert r["pnl_usd"] == pytest.approx(397.4)

    def test_zero_entry_safe(self):
        r = compute_pnl(0.0, 1.0, 1, "LONG", 0)
        assert r["pnl_pct"] == 0.0

    def test_round_trip(self):
        cfg = FillConfig(slippage_pct=0.005, commission_per_contract=0.65)
        entry = simulate_entry_fill(2.0, 2, "LONG", cfg)
        exit_ = simulate_exit_fill(4.0, 2, "LONG", cfg)
        tot = entry["commission"] + exit_["commission"]
        pnl = compute_pnl(entry["fill_price"], exit_["fill_price"], 2, "LONG", tot)
        assert 300 < pnl["pnl_usd"] < 400
