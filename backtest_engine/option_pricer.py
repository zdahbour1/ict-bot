"""
Black-Scholes-Merton option pricing + Greeks.

Replaces the crude "5x leverage on underlying move" proxy in
backtest_engine/engine.py. Pure-python, vectorizable, no dependencies
beyond the standard library + math.

API:
  bs_price(S, K, T, r, sigma, right) -> float
  bs_greeks(...)                     -> dict with delta/gamma/theta/vega/rho
  implied_vol(S, K, T, r, market, right) -> float   (solved numerically)

Parameters are the canonical BS inputs:
  S     : underlying price
  K     : strike
  T     : time to expiry in years
  r     : risk-free rate (e.g. 0.04 for 4 %/yr)
  sigma : volatility (e.g. 0.30 for 30 % annualized)
  right : 'C' (call) or 'P' (put)

Designed for OPT and FOP. Equity options use the standard BS formula;
futures options use the Black '76 model which is the same equation
with S replaced by F × e^(-rT). We accept a `model` flag to pick.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


SQRT_2PI = math.sqrt(2.0 * math.pi)


def _phi(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _Phi(x: float) -> float:
    """Standard normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        raise ValueError(f"invalid BS inputs: S={S} K={K} T={T} sigma={sigma}")
    vt = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / vt
    d2 = d1 - vt
    return d1, d2


def bs_price(
    S: float, K: float, T: float, r: float, sigma: float,
    right: str = "C", *, model: str = "bs",
) -> float:
    """Price a European option.

    model='bs'     — standard Black-Scholes for equity options
    model='black76' — Black '76 model for futures options (F-forward)
                     where S is the futures price and discounting is applied
                     to both legs.
    """
    if right not in ("C", "P"):
        raise ValueError(f"right must be 'C' or 'P', got {right!r}")

    # Handle degenerate cases: zero-time / expired options
    if T <= 0:
        intrinsic = max(S - K, 0) if right == "C" else max(K - S, 0)
        return intrinsic

    # Zero vol → just the discounted intrinsic
    if sigma <= 0:
        if right == "C":
            return max(S - K * math.exp(-r * T), 0)
        return max(K * math.exp(-r * T) - S, 0)

    if model == "black76":
        # Futures options: replace S with F e^{-rT} in BS formulas
        d1, d2 = _d1_d2(S * math.exp(-r * T) / math.exp(-r * T), K, T, r, sigma)  # equivalent
        # Actually the cleaner Black '76:
        vt = sigma * math.sqrt(T)
        d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / vt
        d2 = d1 - vt
        disc = math.exp(-r * T)
        if right == "C":
            return disc * (S * _Phi(d1) - K * _Phi(d2))
        return disc * (K * _Phi(-d2) - S * _Phi(-d1))

    # Standard Black-Scholes
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    if right == "C":
        return S * _Phi(d1) - K * math.exp(-r * T) * _Phi(d2)
    return K * math.exp(-r * T) * _Phi(-d2) - S * _Phi(-d1)


@dataclass
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float     # per year; divide by 365 for per-day
    vega: float      # per 1.00 change in sigma (decimal), so 1% change = vega/100
    rho: float       # per 1.00 change in r


def bs_greeks(
    S: float, K: float, T: float, r: float, sigma: float,
    right: str = "C", *, model: str = "bs",
) -> Greeks:
    """Price + 5 Greeks. Same model flags as bs_price."""
    if right not in ("C", "P"):
        raise ValueError(f"right must be 'C' or 'P', got {right!r}")

    if T <= 0:
        intrinsic = max(S - K, 0) if right == "C" else max(K - S, 0)
        return Greeks(price=intrinsic, delta=(1.0 if right == "C" and S > K
                                              else -1.0 if right == "P" and S < K
                                              else 0.0),
                      gamma=0.0, theta=0.0, vega=0.0, rho=0.0)

    if sigma <= 0:
        return Greeks(
            price=bs_price(S, K, T, r, 1e-12, right, model=model),
            delta=(1.0 if right == "C" and S > K * math.exp(-r * T)
                   else -1.0 if right == "P" and S < K * math.exp(-r * T)
                   else 0.0),
            gamma=0.0, theta=0.0, vega=0.0, rho=0.0,
        )

    if model == "black76":
        vt = sigma * math.sqrt(T)
        d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / vt
        d2 = d1 - vt
        disc = math.exp(-r * T)
        price = bs_price(S, K, T, r, sigma, right, model="black76")
        if right == "C":
            delta = disc * _Phi(d1)
            rho = -T * price
        else:
            delta = -disc * _Phi(-d1)
            rho = -T * price
        gamma = disc * _phi(d1) / (S * vt)
        theta = (-disc * S * _phi(d1) * sigma / (2 * math.sqrt(T))
                 + r * price)  # Black '76 theta
        vega = disc * S * _phi(d1) * math.sqrt(T)
        return Greeks(price, delta, gamma, theta, vega, rho)

    # Standard BS
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    disc = math.exp(-r * T)
    price = bs_price(S, K, T, r, sigma, right, model="bs")
    if right == "C":
        delta = _Phi(d1)
        theta = (-S * _phi(d1) * sigma / (2 * math.sqrt(T))
                 - r * K * disc * _Phi(d2))
        rho = K * T * disc * _Phi(d2)
    else:
        delta = _Phi(d1) - 1.0
        theta = (-S * _phi(d1) * sigma / (2 * math.sqrt(T))
                 + r * K * disc * _Phi(-d2))
        rho = -K * T * disc * _Phi(-d2)
    gamma = _phi(d1) / (S * sigma * math.sqrt(T))
    vega = S * _phi(d1) * math.sqrt(T)
    return Greeks(price, delta, gamma, theta, vega, rho)


def implied_vol(
    S: float, K: float, T: float, r: float, market_price: float,
    right: str = "C", *, model: str = "bs",
    tol: float = 1e-5, max_iter: int = 100,
) -> float:
    """Solve for sigma given an observed market price. Uses Newton-Raphson
    with a bisection fallback for poorly-conditioned cases.

    Returns the implied volatility. If the price is below intrinsic or
    the solver fails to converge, raises ValueError.
    """
    intrinsic = max(S - K, 0) if right == "C" else max(K - S, 0)
    if market_price < intrinsic - tol:
        raise ValueError(
            f"market price {market_price:.4f} < intrinsic {intrinsic:.4f} — "
            "arbitrage violation or bad input"
        )

    # Brenner-Subrahmanyam approximation as an initial guess
    # sigma ≈ sqrt(2π / T) * market / S
    try:
        sigma = math.sqrt(2 * math.pi / T) * market_price / S
    except (ValueError, ZeroDivisionError):
        sigma = 0.3
    sigma = max(0.01, min(sigma, 5.0))  # clamp to sensible range

    for _ in range(max_iter):
        g = bs_greeks(S, K, T, r, sigma, right, model=model)
        diff = g.price - market_price
        if abs(diff) < tol:
            return sigma
        if g.vega < 1e-8:
            break
        sigma = sigma - diff / g.vega
        sigma = max(0.001, min(sigma, 10.0))

    # Fallback: bisection on [0.01, 5.0]
    lo, hi = 0.001, 5.0
    for _ in range(200):
        mid = (lo + hi) / 2
        p = bs_price(S, K, T, r, mid, right, model=model)
        if abs(p - market_price) < tol:
            return mid
        if p < market_price:
            lo = mid
        else:
            hi = mid
    raise ValueError(
        f"implied_vol did not converge for S={S} K={K} T={T} r={r} "
        f"market={market_price} right={right}"
    )
