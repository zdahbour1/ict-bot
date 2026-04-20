"""Unit tests for backtest_engine/sweep.py — grid construction + result formatting.

The actual run_sweep() function is tested via integration because it
needs the engine + DB. Here we cover the pure helpers.
"""
from __future__ import annotations

from backtest_engine.sweep import (
    SweepCell, SweepResult, build_grid, format_results_table,
)


class TestBuildGrid:
    def test_empty_grid_yields_one_cell(self):
        cells = build_grid({})
        assert len(cells) == 1
        assert cells[0].overrides == {}

    def test_single_param_expansion(self):
        cells = build_grid({"profit_target": [0.5, 1.0, 1.5]})
        assert len(cells) == 3
        pts = [c.overrides["profit_target"] for c in cells]
        assert pts == [0.5, 1.0, 1.5]

    def test_two_params_cross_product(self):
        cells = build_grid({
            "profit_target": [0.5, 1.0],
            "stop_loss": [0.3, 0.6],
        })
        assert len(cells) == 4
        combos = [tuple(sorted(c.overrides.items())) for c in cells]
        # Cross product
        assert (("profit_target", 0.5), ("stop_loss", 0.3)) in combos
        assert (("profit_target", 1.0), ("stop_loss", 0.6)) in combos

    def test_three_params_cross_product(self):
        cells = build_grid({
            "a": [1, 2], "b": [10, 20], "c": [100, 200],
        })
        # 2 × 2 × 2 = 8
        assert len(cells) == 8

    def test_mixed_value_types(self):
        """Values can be int, float, str, list — grid doesn't care."""
        cells = build_grid({
            "interval": ["5m", "15m"],
            "dte": [1, 7],
        })
        assert len(cells) == 4
        assert {c.overrides["interval"] for c in cells} == {"5m", "15m"}
        assert {c.overrides["dte"] for c in cells} == {1, 7}


class TestSweepCellLabel:
    def test_deterministic_label(self):
        """Same overrides → same label regardless of dict order."""
        a = SweepCell(overrides={"profit_target": 1.0, "stop_loss": 0.6})
        b = SweepCell(overrides={"stop_loss": 0.6, "profit_target": 1.0})
        assert a.label() == b.label()

    def test_label_includes_all_keys(self):
        c = SweepCell(overrides={"profit_target": 1.0, "stop_loss": 0.6})
        assert "profit_target=1.0" in c.label()
        assert "stop_loss=0.6" in c.label()


class TestFormatResultsTable:
    def test_empty(self):
        assert "no results" in format_results_table([])

    def test_shape_with_mixed_outcomes(self):
        r1 = SweepResult(
            run_id=100, cell=SweepCell({"profit_target": 1.0}),
            total_pnl=500.0, profit_factor=1.5,
            win_rate=55.0, total_trades=50, max_drawdown=-200.0,
            sharpe_ratio=0.5, duration_sec=60,
        )
        r2 = SweepResult(
            run_id=101, cell=SweepCell({"profit_target": 2.0}),
            total_pnl=-300.0, profit_factor=None,
            win_rate=30.0, total_trades=40, max_drawdown=-400.0,
            sharpe_ratio=None, duration_sec=58,
        )
        out = format_results_table([r1, r2])
        assert "500.00" in out
        assert "-300.00" in out
        assert "profit_target=1.0" in out
        assert "PF" in out  # header

    def test_error_cell_shown(self):
        r = SweepResult(
            run_id=-1, cell=SweepCell({"bad": "config"}),
            total_pnl=0.0, profit_factor=None, win_rate=0.0,
            total_trades=0, max_drawdown=0.0, sharpe_ratio=None,
            duration_sec=None, error_message="boom",
        )
        out = format_results_table([r])
        assert "boom" in out
