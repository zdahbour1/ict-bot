"""
Unit tests for backtest/metrics.py — summary stat computation (ENH-019).
"""
import pytest
from backtest.metrics import compute_summary, BacktestSummary


def _t(pnl, result=None, hold=None):
    d = {"pnl_usd": pnl}
    if result is not None:
        d["exit_result"] = result
    if hold is not None:
        d["hold_minutes"] = hold
    return d


class TestComputeSummary:
    def test_empty_returns_zero_summary(self):
        s = compute_summary([])
        assert s.total_trades == 0
        assert s.total_pnl == 0.0
        assert s.sharpe_ratio is None
        assert s.profit_factor is None

    def test_basic_aggregation(self):
        trades = [_t(100, "WIN", 30), _t(-50, "LOSS", 45), _t(200, "WIN", 60)]
        s = compute_summary(trades)
        assert s.total_trades == 3
        assert s.wins == 2
        assert s.losses == 1
        assert s.scratches == 0
        assert s.total_pnl == 250.0
        assert s.win_rate == pytest.approx(66.67, abs=0.01)
        assert s.avg_win == 150.0
        assert s.avg_loss == -50.0
        assert s.avg_hold_min == 45.0

    def test_scratches_excluded_from_win_rate(self):
        trades = [_t(100, "WIN"), _t(0, "SCRATCH"), _t(0, "SCRATCH")]
        s = compute_summary(trades)
        assert s.win_rate == 100.0  # 1 win / 1 decided trade
        assert s.scratches == 2

    def test_classify_from_pnl_when_result_missing(self):
        trades = [_t(50), _t(-30), _t(0)]
        s = compute_summary(trades)
        assert s.wins == 1
        assert s.losses == 1
        assert s.scratches == 1

    def test_profit_factor(self):
        # +100 + 200 = 300 gross profit, -50 - 50 = 100 gross loss → PF = 3
        trades = [_t(100), _t(200), _t(-50), _t(-50)]
        s = compute_summary(trades)
        assert s.profit_factor == pytest.approx(3.0)

    def test_profit_factor_none_when_no_losses(self):
        trades = [_t(100), _t(50)]
        s = compute_summary(trades)
        assert s.profit_factor is None

    def test_drawdown(self):
        # Cumulative: +500, +800, +300, +900 → peak=800 at trade 2,
        # trough=300 at trade 3 → drawdown = -500
        trades = [_t(500), _t(300), _t(-500), _t(600)]
        s = compute_summary(trades)
        assert s.max_drawdown == -500.0

    def test_drawdown_no_loss_ever(self):
        trades = [_t(100), _t(200), _t(300)]
        s = compute_summary(trades)
        assert s.max_drawdown == 0.0

    def test_streaks(self):
        trades = [_t(10, "WIN"), _t(10, "WIN"), _t(10, "WIN"),
                  _t(-10, "LOSS"), _t(-10, "LOSS"),
                  _t(10, "WIN")]
        s = compute_summary(trades)
        assert s.max_win_streak == 3
        assert s.max_loss_streak == 2

    def test_sharpe_needs_two_trades(self):
        assert compute_summary([_t(100)]).sharpe_ratio is None

    def test_sharpe_basic(self):
        # Symmetric small sample — just validate it's a finite number
        s = compute_summary([_t(10), _t(20), _t(30), _t(40)])
        assert s.sharpe_ratio is not None
        assert s.sharpe_ratio > 0

    def test_sharpe_zero_variance_returns_none(self):
        s = compute_summary([_t(50), _t(50), _t(50)])
        assert s.sharpe_ratio is None

    def test_to_dict_matches_db_columns(self):
        """Summary keys should align with backtest_runs columns."""
        s = BacktestSummary()
        d = s.to_dict()
        required = {
            "total_trades", "wins", "losses", "scratches", "total_pnl",
            "win_rate", "avg_win", "avg_loss", "max_drawdown",
            "sharpe_ratio", "profit_factor", "avg_hold_min",
            "max_win_streak", "max_loss_streak",
        }
        assert required.issubset(d.keys())
