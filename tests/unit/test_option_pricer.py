"""
Unit tests for backtest_engine/option_pricer.py — Black-Scholes pricing.

Numerical tolerances are loose (±1e-4 for prices, ±1e-3 for greeks)
to allow for different math implementations. The critical properties
tested are *directional* (put-call parity, monotonicity) which must
hold exactly regardless of numerical approach.
"""
from __future__ import annotations

import math
import pytest

from backtest_engine.option_pricer import (
    bs_price, bs_greeks, implied_vol, Greeks,
)


# ── bs_price: canonical textbook cases ──────────────────

class TestBSPriceCanonical:
    """Test against known textbook values (Hull, Options, Futures, and
    Other Derivatives — worked examples)."""

    def test_atm_call_standard(self):
        # S=100, K=100, T=1, r=5%, sigma=20% → call ≈ 10.45
        price = bs_price(100, 100, 1.0, 0.05, 0.20, "C")
        assert abs(price - 10.4506) < 0.001

    def test_atm_put_standard(self):
        # Put = Call - S + K*e^{-rT} (put-call parity)
        # Known: ATM put at S=100 K=100 T=1 r=5% sigma=20% ≈ 5.57
        price = bs_price(100, 100, 1.0, 0.05, 0.20, "P")
        assert abs(price - 5.5735) < 0.001

    def test_itm_call(self):
        # S=110, K=100, T=0.5, r=3%, sigma=25%
        price = bs_price(110, 100, 0.5, 0.03, 0.25, "C")
        assert 12.0 < price < 15.0  # known approx 13.x

    def test_otm_put(self):
        # S=110, K=100, T=0.5, r=3%, sigma=25% — far OTM put
        price = bs_price(110, 100, 0.5, 0.03, 0.25, "P")
        assert 1.0 < price < 5.0


class TestBSPriceEdgeCases:
    def test_expired_call_above_strike(self):
        assert bs_price(110, 100, 0.0, 0.05, 0.2, "C") == 10.0

    def test_expired_call_below_strike(self):
        assert bs_price(90, 100, 0.0, 0.05, 0.2, "C") == 0.0

    def test_expired_put_above_strike(self):
        assert bs_price(110, 100, 0.0, 0.05, 0.2, "P") == 0.0

    def test_expired_put_below_strike(self):
        assert bs_price(90, 100, 0.0, 0.05, 0.2, "P") == 10.0

    def test_zero_vol_call_discounted_intrinsic(self):
        # sigma=0: call = max(S - K*e^{-rT}, 0)
        price = bs_price(110, 100, 1.0, 0.05, 0.0, "C")
        expected = 110 - 100 * math.exp(-0.05)
        assert abs(price - expected) < 1e-6

    def test_invalid_right_rejected(self):
        with pytest.raises(ValueError):
            bs_price(100, 100, 1, 0.05, 0.2, "X")


class TestPutCallParity:
    """C - P = S - K*e^{-rT}  must hold exactly for any valid BS inputs."""

    @pytest.mark.parametrize("S,K,T,r,sigma", [
        (100, 100, 1.0, 0.05, 0.20),
        (100, 110, 0.5, 0.03, 0.30),
        (100, 90, 2.0, 0.02, 0.40),
        (50, 50, 0.1, 0.01, 0.50),
        (500, 450, 0.25, 0.045, 0.18),
    ])
    def test_parity_holds(self, S, K, T, r, sigma):
        call = bs_price(S, K, T, r, sigma, "C")
        put = bs_price(S, K, T, r, sigma, "P")
        rhs = S - K * math.exp(-r * T)
        assert abs((call - put) - rhs) < 1e-6


class TestMonotonicity:
    """Simple directional checks — must hold for any pricer."""

    def test_call_price_increases_with_spot(self):
        T, r, sigma = 0.5, 0.05, 0.2
        K = 100
        prices = [bs_price(S, K, T, r, sigma, "C") for S in (80, 90, 100, 110, 120)]
        assert all(prices[i] < prices[i + 1] for i in range(len(prices) - 1))

    def test_put_price_decreases_with_spot(self):
        T, r, sigma = 0.5, 0.05, 0.2
        K = 100
        prices = [bs_price(S, K, T, r, sigma, "P") for S in (80, 90, 100, 110, 120)]
        assert all(prices[i] > prices[i + 1] for i in range(len(prices) - 1))

    def test_call_price_increases_with_time(self):
        S, K, r, sigma = 100, 100, 0.05, 0.2
        prices = [bs_price(S, K, T, r, sigma, "C") for T in (0.01, 0.25, 0.5, 1.0, 2.0)]
        assert all(prices[i] < prices[i + 1] for i in range(len(prices) - 1))

    def test_call_price_increases_with_vol(self):
        S, K, T, r = 100, 100, 0.5, 0.05
        prices = [bs_price(S, K, T, r, sigma, "C") for sigma in (0.1, 0.2, 0.3, 0.5)]
        assert all(prices[i] < prices[i + 1] for i in range(len(prices) - 1))


# ── bs_greeks ───────────────────────────────────────────

class TestGreeks:
    def test_atm_call_delta_near_half(self):
        """ATM calls have delta ≈ 0.5 (slightly > for positive r)."""
        g = bs_greeks(100, 100, 1.0, 0.05, 0.2, "C")
        assert 0.55 < g.delta < 0.75

    def test_atm_put_delta_near_neg_half(self):
        g = bs_greeks(100, 100, 1.0, 0.05, 0.2, "P")
        assert -0.45 < g.delta < -0.25

    def test_call_delta_ranges_0_to_1(self):
        """Call delta is bounded in [0, 1]."""
        for S in (50, 75, 100, 125, 150):
            g = bs_greeks(S, 100, 0.5, 0.05, 0.25, "C")
            assert 0 <= g.delta <= 1

    def test_put_delta_ranges_neg1_to_0(self):
        for S in (50, 75, 100, 125, 150):
            g = bs_greeks(S, 100, 0.5, 0.05, 0.25, "P")
            assert -1 <= g.delta <= 0

    def test_gamma_always_positive(self):
        """Gamma is the same for calls and puts and always >=0."""
        for right in ("C", "P"):
            for S in (80, 100, 120):
                g = bs_greeks(S, 100, 0.5, 0.05, 0.25, right)
                assert g.gamma >= 0

    def test_vega_always_positive(self):
        for right in ("C", "P"):
            for S in (80, 100, 120):
                g = bs_greeks(S, 100, 0.5, 0.05, 0.25, right)
                assert g.vega >= 0

    def test_theta_negative_for_long_options(self):
        """Long calls and puts both lose value over time (theta < 0 typically)."""
        for right in ("C", "P"):
            g = bs_greeks(100, 100, 0.5, 0.05, 0.25, right)
            assert g.theta < 0

    def test_gamma_puts_calls_equal(self):
        """Gamma is identical for puts and calls at same strike."""
        gc = bs_greeks(100, 100, 0.5, 0.05, 0.25, "C")
        gp = bs_greeks(100, 100, 0.5, 0.05, 0.25, "P")
        assert abs(gc.gamma - gp.gamma) < 1e-8

    def test_price_matches_bs_price(self):
        """Greeks.price matches what bs_price returns directly."""
        for S in (90, 100, 110):
            for right in ("C", "P"):
                g = bs_greeks(S, 100, 0.5, 0.05, 0.25, right)
                p = bs_price(S, 100, 0.5, 0.05, 0.25, right)
                assert abs(g.price - p) < 1e-10


# ── Black '76 (futures options) ─────────────────────────

class TestBlack76:
    def test_atm_futures_call_positive_and_sensible(self):
        # For an ATM futures option at 20% vol, 1y: expect roughly $7-8
        b76_call = bs_price(100, 100, 1.0, 0.05, 0.20, "C", model="black76")
        assert 5 < b76_call < 10

    def test_black76_put_call_parity_with_futures(self):
        """Black '76 parity: C - P = e^{-rT}(F - K)"""
        S, K, T, r, sigma = 100, 100, 0.5, 0.04, 0.25
        call = bs_price(S, K, T, r, sigma, "C", model="black76")
        put = bs_price(S, K, T, r, sigma, "P", model="black76")
        expected = math.exp(-r * T) * (S - K)
        assert abs((call - put) - expected) < 1e-6


# ── implied_vol ─────────────────────────────────────────

class TestImpliedVol:
    @pytest.mark.parametrize("true_sigma", [0.15, 0.25, 0.40, 0.60])
    def test_recovers_original_vol(self, true_sigma):
        """Price an option at known vol, then solve implied_vol.
        Should recover the original sigma to within 1e-4."""
        S, K, T, r = 100, 100, 0.5, 0.03
        market = bs_price(S, K, T, r, true_sigma, "C")
        solved = implied_vol(S, K, T, r, market, "C")
        assert abs(solved - true_sigma) < 1e-4

    def test_recovers_put_vol(self):
        S, K, T, r, sigma = 100, 105, 0.25, 0.04, 0.35
        market = bs_price(S, K, T, r, sigma, "P")
        solved = implied_vol(S, K, T, r, market, "P")
        assert abs(solved - sigma) < 1e-4

    def test_rejects_below_intrinsic(self):
        # Market price below intrinsic = arbitrage violation
        with pytest.raises(ValueError, match="below intrinsic|arbitrage"):
            # Call with S=110, K=100: intrinsic = 10; market = 1 is impossible
            implied_vol(110, 100, 1.0, 0.05, 1.0, "C")
