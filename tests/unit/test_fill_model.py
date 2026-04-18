"""
Unit tests for backtest/fill_model.py — simulated fills + P&L (ENH-019).
"""
import pytest

from backtest.fill_model import (
    FillConfig, simulate_entry_fill, simulate_exit_fill, compute_pnl,
)


class TestEntryFill:
    def test_long_pays_slippage_up(self):
        r = simulate_entry_fill(100.0, 2, "LONG", FillConfig(slippage_pct=0.01))
        assert r["fill_price"] == pytest.approx(101.0)
        assert r["slippage_paid"] == pytest.approx(2.0)  # $1 slippage * 2 contracts
        assert r["commission"] == pytest.approx(1.30)    # 0.65 * 2

    def test_short_collects_less(self):
        r = simulate_entry_fill(100.0, 2, "SHORT", FillConfig(slippage_pct=0.01))
        assert r["fill_price"] == pytest.approx(99.0)

    def test_zero_slippage(self):
        r = simulate_entry_fill(100.0, 1, "LONG", FillConfig(slippage_pct=0.0))
        assert r["fill_price"] == 100.0
        assert r["slippage_paid"] == 0.0


class TestExitFill:
    def test_long_exit_receives_less(self):
        r = simulate_exit_fill(100.0, 2, "LONG", FillConfig(slippage_pct=0.01))
        assert r["fill_price"] == pytest.approx(99.0)

    def test_short_exit_pays_more_to_cover(self):
        r = simulate_exit_fill(100.0, 2, "SHORT", FillConfig(slippage_pct=0.01))
        assert r["fill_price"] == pytest.approx(101.0)


class TestComputePnL:
    def test_long_winner(self):
        # Entry 2.00, exit 4.00, 2 contracts, no commission
        r = compute_pnl(2.00, 4.00, 2, "LONG", total_commission=0)
        assert r["pnl_per_contract"] == 2.0
        # $2 per share * 2 contracts * 100 shares = $400
        assert r["pnl_usd"] == 400.0
        assert r["pnl_pct"] == 1.0  # +100%

    def test_long_loser(self):
        r = compute_pnl(2.00, 1.20, 2, "LONG", total_commission=0)
        assert r["pnl_usd"] == pytest.approx(-160.0)  # -0.80 * 2 * 100
        assert r["pnl_pct"] == pytest.approx(-0.40)

    def test_short_winner(self):
        # Short: entry high, exit low → profit
        r = compute_pnl(2.00, 1.00, 2, "SHORT", total_commission=0)
        assert r["pnl_per_contract"] == 1.0
        assert r["pnl_usd"] == 200.0
        assert r["pnl_pct"] == pytest.approx(0.5)  # +50% from shorter's POV

    def test_commission_deducted(self):
        r = compute_pnl(2.00, 4.00, 2, "LONG", total_commission=2.60)
        assert r["pnl_usd"] == pytest.approx(397.40)

    def test_zero_entry_handles_gracefully(self):
        r = compute_pnl(0.0, 1.0, 1, "LONG", 0)
        assert r["pnl_pct"] == 0.0  # no div-by-zero

    def test_round_trip_with_slippage_and_commissions(self):
        """Integration: full entry + exit + P&L chain."""
        cfg = FillConfig(slippage_pct=0.005, commission_per_contract=0.65)
        entry = simulate_entry_fill(2.00, 2, "LONG", cfg)
        exit_ = simulate_exit_fill(4.00, 2, "LONG", cfg)
        total_comm = entry["commission"] + exit_["commission"]
        pnl = compute_pnl(entry["fill_price"], exit_["fill_price"], 2, "LONG", total_comm)
        # Should still be a winner (~$380-ish after slippage/commission)
        assert pnl["pnl_usd"] > 300
        assert pnl["pnl_usd"] < 400
