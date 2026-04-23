"""Unit tests for ENH-038 Part 2 — multi-leg simulation helpers +
engine integration.

Coverage:
- price_leg uses BS for equity options and Black-76 for FOP
- build_leg_state prices every leg at entry and attaches signed metadata
- price_legs_now reprices and computes net per-share P&L with correct
  directional sign (iron condor short wings net positive as IV decays)
- entry_basis returns a positive denominator even for net-zero spreads
- synth_price collapses multi-leg P&L into a scalar evaluate_exit can use
- build_legs_for_writer mirrors backtest_trade_legs columns exactly
- End-to-end: DeltaNeutralStrategy's 4-leg iron condor simulates cleanly
  through the engine and emits a trade carrying ``_legs``
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from backtest_engine.multi_leg_sim import (
    build_leg_state,
    build_legs_for_writer,
    entry_basis,
    price_leg,
    price_legs_now,
    synth_price,
)


def _iron_condor(underlying_price: float = 500.0,
                 expiry: str = "20260515") -> list[dict]:
    return [
        {"sec_type": "OPT", "symbol": "SPY260515C00500000",
         "underlying": "SPY", "strike": 500.0, "right": "C",
         "expiry": expiry, "multiplier": 100,
         "direction": "SHORT", "contracts": 1,
         "leg_role": "short_call"},
        {"sec_type": "OPT", "symbol": "SPY260515C00510000",
         "underlying": "SPY", "strike": 510.0, "right": "C",
         "expiry": expiry, "multiplier": 100,
         "direction": "LONG", "contracts": 1,
         "leg_role": "long_call"},
        {"sec_type": "OPT", "symbol": "SPY260515P00500000",
         "underlying": "SPY", "strike": 500.0, "right": "P",
         "expiry": expiry, "multiplier": 100,
         "direction": "SHORT", "contracts": 1,
         "leg_role": "short_put"},
        {"sec_type": "OPT", "symbol": "SPY260515P00490000",
         "underlying": "SPY", "strike": 490.0, "right": "P",
         "expiry": expiry, "multiplier": 100,
         "direction": "LONG", "contracts": 1,
         "leg_role": "long_put"},
    ]


class TestPriceLeg:
    def test_equity_call_atm_has_time_value(self):
        # ATM call with 30 DTE should have positive extrinsic value
        leg = {"sec_type": "OPT", "right": "C", "strike": 100.0,
               "expiry": "20260520"}
        now = datetime(2026, 4, 20, tzinfo=timezone.utc)
        px = price_leg(leg, underlying=100.0, now=now,
                       sigma=0.20, r=0.04)
        assert px > 0.5   # meaningful time premium
        assert px < 10.0  # sanity upper bound

    def test_fop_routes_to_black76(self):
        # Black-76 on equal F=K gives a small positive price; shouldn't crash
        leg = {"sec_type": "FOP", "right": "C", "strike": 5000.0,
               "expiry": "20260520"}
        now = datetime(2026, 4, 20, tzinfo=timezone.utc)
        px = price_leg(leg, underlying=5000.0, now=now,
                       sigma=0.25, r=0.04)
        assert px > 0

    def test_missing_expiry_uses_default_dte(self):
        leg = {"sec_type": "OPT", "right": "C", "strike": 100.0}
        now = datetime(2026, 4, 20, tzinfo=timezone.utc)
        # No expiry → default 7 DTE, should still price sensibly
        px = price_leg(leg, underlying=100.0, now=now)
        assert px > 0


class TestBuildLegState:
    def test_prices_all_four_legs(self):
        now = datetime(2026, 4, 20, tzinfo=timezone.utc)
        state = build_leg_state(_iron_condor(), 500.0, now,
                                sigma=0.20, r=0.04)
        assert len(state) == 4
        # All legs receive a positive BS price
        assert all(s["entry_price"] > 0 for s in state)
        # leg_index increments
        assert [s["leg_index"] for s in state] == [0, 1, 2, 3]

    def test_direction_sign_recorded(self):
        now = datetime(2026, 4, 20, tzinfo=timezone.utc)
        state = build_leg_state(_iron_condor(), 500.0, now)
        signs = [s["_sign"] for s in state]
        assert signs == [-1, +1, -1, +1]   # short_call, long_call, short_put, long_put

    def test_accepts_dataclass_legs(self):
        from strategy.base_strategy import LegSpec
        legs = [LegSpec(sec_type="OPT", symbol="SPY_C", direction="SHORT",
                        contracts=1, strike=500.0, right="C",
                        expiry="20260515")]
        now = datetime(2026, 4, 20, tzinfo=timezone.utc)
        state = build_leg_state(legs, 500.0, now)
        assert len(state) == 1
        assert state[0]["direction"] == "SHORT"
        assert state[0]["entry_price"] > 0


class TestPriceLegsNow:
    def test_time_decay_favors_short_spread(self):
        """Underlying unchanged + time passes → short premiums shrink →
        iron condor should show a positive net P&L per share."""
        entry = datetime(2026, 4, 20, tzinfo=timezone.utc)
        state = build_leg_state(_iron_condor(), 500.0, entry,
                                sigma=0.20, r=0.04)
        # 14 days later, same underlying
        later = datetime(2026, 5, 4, tzinfo=timezone.utc)
        prices, net_pnl = price_legs_now(state, 500.0, later,
                                         sigma=0.20, r=0.04)
        assert len(prices) == 4
        # Net P&L per share is POSITIVE because we collected more
        # premium than we paid (short ATM vs long OTM).
        assert net_pnl > 0

    def test_big_underlying_move_hurts_short_spread(self):
        """Underlying rips through the short call strike — iron condor
        takes a loss. net_pnl should be negative."""
        entry = datetime(2026, 4, 20, tzinfo=timezone.utc)
        state = build_leg_state(_iron_condor(500.0), 500.0, entry)
        later = datetime(2026, 4, 25, tzinfo=timezone.utc)
        # Underlying jumps from 500 to 520 — beyond the 510 long call
        _, net_pnl = price_legs_now(state, 520.0, later)
        assert net_pnl < 0


class TestEntryBasisAndSynthPrice:
    def test_entry_basis_positive(self):
        entry = datetime(2026, 4, 20, tzinfo=timezone.utc)
        state = build_leg_state(_iron_condor(), 500.0, entry)
        assert entry_basis(state) > 0

    def test_entry_basis_nonzero_for_empty(self):
        # Defensive: never return 0 (would divide-by-zero in synth_price)
        assert entry_basis([]) == 1.0

    def test_synth_price_profit(self):
        # $2 proxy, $0.10 net profit per share on a $0.50 basis → +20%
        px = synth_price(entry_proxy=2.0, net_pnl_per_share=0.10, basis=0.50)
        assert px == pytest.approx(2.40)

    def test_synth_price_loss(self):
        px = synth_price(entry_proxy=2.0, net_pnl_per_share=-0.30, basis=0.50)
        assert px == pytest.approx(0.80)


class TestBuildLegsForWriter:
    def test_mirrors_schema(self):
        entry = datetime(2026, 4, 20, tzinfo=timezone.utc)
        state = build_leg_state(_iron_condor(), 500.0, entry)
        exit_prices = [1.0, 0.5, 1.0, 0.5]
        exit_time = datetime(2026, 4, 22, tzinfo=timezone.utc)
        out = build_legs_for_writer(state, exit_prices, exit_time)

        assert len(out) == 4
        first = out[0]
        # All backtest_trade_legs columns present
        for col in ("leg_index", "leg_role", "sec_type", "symbol",
                    "underlying", "strike", "right", "expiry",
                    "multiplier", "direction", "contracts",
                    "entry_price", "exit_price",
                    "entry_time", "exit_time"):
            assert col in first
        assert first["exit_price"] == 1.0
        assert first["exit_time"] == exit_time

    def test_length_mismatch_raises(self):
        entry = datetime(2026, 4, 20, tzinfo=timezone.utc)
        state = build_leg_state(_iron_condor(), 500.0, entry)
        with pytest.raises(ValueError):
            build_legs_for_writer(state, [1.0, 2.0], entry)   # only 2 prices


class TestDeltaNeutralEngineIntegration:
    """Smoke test: a DeltaNeutralStrategy signal passes through the
    engine's multi-leg branch and the resulting trade carries ``_legs``.
    We stub detect() to fire once, feed a synthetic OHLC frame, and
    inspect the trade dict the engine emits. No DB, no IB, no yfinance.
    """

    def _synth_bars(self, n: int = 80):
        import pandas as pd
        idx = pd.date_range("2026-04-20 09:30", periods=n, freq="5min",
                            tz="America/New_York")
        close = [500.0 + (i * 0.1) for i in range(n)]
        return pd.DataFrame({
            "open": close, "high": close, "low": close, "close": close,
            "volume": [1000] * n,
        }, index=idx)

    def test_place_legs_path_emits_multi_leg_trade(self, monkeypatch):
        from backtest_engine import engine as bt_engine
        from strategy.delta_neutral_strategy import DeltaNeutralStrategy
        from strategy.base_strategy import Signal

        strategy = DeltaNeutralStrategy(
            default_expiry="20260515", contracts=1,
        )

        # Force detect to fire one signal exactly on the first scanned bar.
        call_counter = {"n": 0}
        def fake_detect(self, b1, b1h, b4h, levels, ticker):
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                return [Signal(
                    signal_type="DELTA_NEUTRAL_CONDOR",
                    direction="LONG",
                    entry_price=float(b1["close"].iloc[-1]),
                    sl=0.0, tp=0.0,
                    setup_id="test-setup",
                    ticker=ticker,
                    strategy_name="delta_neutral",
                    details={
                        "iv_proxy": 0.30,
                        "current_price": float(b1["close"].iloc[-1]),
                        "strike_interval": 5.0,
                        "wing_width": 10.0,
                        "expiry": "20260515",
                    },
                )]
            return []
        monkeypatch.setattr(DeltaNeutralStrategy, "detect", fake_detect)

        # Stub evaluate_exit to fire on the next bar (forces an exit).
        exit_counter = {"n": 0}
        def fake_evaluate_exit(trade, price, now_pt):
            exit_counter["n"] += 1
            if exit_counter["n"] >= 2:
                return {"reason": "TP", "result": "WIN"}
            return None
        monkeypatch.setattr(bt_engine, "evaluate_exit", fake_evaluate_exit)

        # Stub fetch_multi_timeframe so no yfinance network call happens.
        bars = self._synth_bars()
        monkeypatch.setattr(bt_engine, "fetch_multi_timeframe",
                            lambda *a, **kw: {"base": bars,
                                              "1h": bars, "4h": bars})
        # get_all_levels returns [] → avoid dependency on indicator libs
        monkeypatch.setattr(bt_engine, "get_all_levels",
                            lambda *a, **kw: [])

        from backtest_engine.fill_model import FillConfig
        trades = bt_engine._simulate_ticker(
            ticker="SPY",
            strategy=strategy,
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 21),
            pnl_target=1.0, sl_target=0.6, contracts=1,
            fill_cfg=FillConfig(),
            cfg={"sec_type": "OPT", "cooldown_minutes": 0,
                 "max_trades_per_day": 8},
            progress=lambda m: None,
        )

        assert len(trades) == 1
        trade = trades[0]
        assert "_legs" in trade, "multi-leg branch must emit _legs"
        assert len(trade["_legs"]) == 4
        # Each leg has the writer-schema keys populated
        for lg in trade["_legs"]:
            assert lg["leg_index"] in (0, 1, 2, 3)
            assert lg["entry_price"] > 0
            assert lg["exit_price"] >= 0
            assert lg["multiplier"] == 100
        # pnl_usd is summed across legs (could be + or -, just must exist)
        assert "pnl_usd" in trade
        assert trade["exit_reason"] == "TP"
