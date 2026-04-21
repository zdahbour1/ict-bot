"""
Unit tests for backtest_engine/data_provider_ib.py.

Mocks the IB connection so these run without TWS. The real-TWS
integration test lives in tests/integration/ and is marked so it
skips when TWS isn't listening.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from backtest_engine.data_provider_ib import (
    IBContractSpec, _build_ib_contract, _ib_bars_to_df,
    _interval_to_ib, _duration_string, _MAX_LOOKBACK_DAYS,
    spec_from_ticker_row, fetch_bars_ib,
)


# ── IBContractSpec ───────────────────────────────────────

class TestIBContractSpec:
    def test_fop_cache_key_stable(self):
        spec = IBContractSpec(
            sec_type="FOP", symbol="MNQ", exchange="GLOBEX",
            last_trade_date="20260619", strike=22500.0, right="C",
            multiplier=2,
        )
        a = spec.cache_key("5m", date(2026, 4, 20), 30)
        b = spec.cache_key("5m", date(2026, 4, 20), 30)
        assert a == b
        # No path-hostile characters
        assert "/" not in a and "\\" not in a

    def test_cache_key_differs_per_interval(self):
        spec = IBContractSpec(sec_type="FOP", symbol="ES", exchange="GLOBEX",
                              last_trade_date="20260619", strike=5500, right="P")
        a = spec.cache_key("5m", date(2026, 4, 20), 30)
        b = spec.cache_key("1h", date(2026, 4, 20), 30)
        assert a != b

    def test_cache_key_differs_per_strike(self):
        common = dict(sec_type="FOP", symbol="ES", exchange="GLOBEX",
                      last_trade_date="20260619", right="C")
        a = IBContractSpec(strike=5500, **common).cache_key("5m", date(2026, 4, 20), 30)
        b = IBContractSpec(strike=5600, **common).cache_key("5m", date(2026, 4, 20), 30)
        assert a != b


# ── Interval + duration helpers ──────────────────────────

class TestIntervalMapping:
    @pytest.mark.parametrize("interval,expected", [
        ("1m", "1 min"),
        ("5m", "5 mins"),
        ("1h", "1 hour"),
        ("1d", "1 day"),
    ])
    def test_mapping(self, interval, expected):
        assert _interval_to_ib(interval) == expected

    def test_unsupported_interval_raises(self):
        with pytest.raises(ValueError):
            _interval_to_ib("13s")

    def test_max_lookback_defined_for_all_intervals(self):
        supported = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}
        for i in supported:
            assert i in _MAX_LOOKBACK_DAYS


class TestDurationString:
    @pytest.mark.parametrize("days,expected", [
        (1, "1 D"),
        (30, "30 D"),
        (364, "364 D"),
        (365, "1 Y"),
        (730, "2 Y"),
        (0, "1 D"),        # guarded below
        (-5, "1 D"),
    ])
    def test_format(self, days, expected):
        assert _duration_string(days) == expected


# ── Contract builder ─────────────────────────────────────

class TestBuildIBContract:
    def test_fop_builds_futures_option(self):
        spec = IBContractSpec(
            sec_type="FOP", symbol="MNQ", exchange="GLOBEX",
            last_trade_date="20260619", strike=22500, right="C",
            multiplier=2,
        )
        c = _build_ib_contract(spec)
        assert c.__class__.__name__ == "FuturesOption"
        assert c.symbol == "MNQ"
        assert c.exchange == "GLOBEX"
        assert c.strike == 22500
        assert c.right == "C"
        assert c.multiplier == "2"

    def test_fop_missing_strike_rejected(self):
        spec = IBContractSpec(
            sec_type="FOP", symbol="MNQ", exchange="GLOBEX",
            last_trade_date="20260619", right="C",
        )
        with pytest.raises(ValueError, match="requires last_trade_date"):
            _build_ib_contract(spec)

    def test_stk_builds_stock(self):
        spec = IBContractSpec(sec_type="STK", symbol="QQQ", exchange="SMART")
        c = _build_ib_contract(spec)
        assert c.__class__.__name__ == "Stock"
        assert c.symbol == "QQQ"

    def test_fut_builds_future(self):
        spec = IBContractSpec(
            sec_type="FUT", symbol="MNQ", exchange="GLOBEX",
            contract_month="202606",
        )
        c = _build_ib_contract(spec)
        assert c.__class__.__name__ == "Future"
        assert c.lastTradeDateOrContractMonth == "202606"

    def test_unsupported_sec_type(self):
        spec = IBContractSpec(sec_type="ETF", symbol="X", exchange="Y")
        with pytest.raises(ValueError, match="Unsupported sec_type"):
            _build_ib_contract(spec)


# ── IB bars → DataFrame ──────────────────────────────────

class TestIBBarsToDF:
    def test_empty(self):
        df = _ib_bars_to_df([])
        assert df.empty
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]

    def test_canonical_shape(self):
        fake_bars = [
            SimpleNamespace(
                date=datetime(2026, 3, 1, 14, 30, tzinfo=timezone.utc),
                open=100.0, high=101.0, low=99.5, close=100.5, volume=1000,
            ),
            SimpleNamespace(
                date=datetime(2026, 3, 1, 14, 35, tzinfo=timezone.utc),
                open=100.5, high=100.8, low=100.2, close=100.3, volume=1200,
            ),
        ]
        df = _ib_bars_to_df(fake_bars)
        assert len(df) == 2
        assert df.index.tz is not None
        assert str(df.index.tz) == "UTC"
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
        assert df["close"].iloc[0] == 100.5

    def test_handles_naive_datetime(self):
        fake_bars = [
            SimpleNamespace(
                date=datetime(2026, 3, 1, 14, 30),   # NO tz
                open=100.0, high=101.0, low=99.5, close=100.5, volume=1000,
            ),
        ]
        df = _ib_bars_to_df(fake_bars)
        assert df.index.tz is not None
        assert str(df.index.tz) == "UTC"


# ── spec_from_ticker_row ─────────────────────────────────

class TestSpecFromTickerRow:
    def test_mnq_resolves_from_fop_specs(self):
        spec = spec_from_ticker_row("MNQ", "20260619", 22500, "C")
        assert spec.sec_type == "FOP"
        assert spec.symbol == "MNQ"
        assert spec.exchange == "CME"
        assert spec.multiplier == 2
        assert spec.strike == 22500
        assert spec.right == "C"

    def test_es_resolves(self):
        spec = spec_from_ticker_row("ES", "20260619", 5500, "P")
        assert spec.multiplier == 50
        assert spec.exchange == "CME"

    def test_gc_resolves(self):
        spec = spec_from_ticker_row("GC", "20260619", 2500, "C")
        assert spec.exchange == "NYMEX"
        assert spec.multiplier == 100

    def test_unknown_underlying_raises(self):
        with pytest.raises(ValueError, match="Unknown FOP"):
            spec_from_ticker_row("FAKESYM", "20260619", 100, "C")


# ── fetch_bars_ib (mocked IB) ────────────────────────────

class TestFetchBarsIBMocked:
    """Mock ib_async.IB so these pass without TWS."""

    def _make_fake_bars(self, n: int = 10):
        start = datetime(2026, 3, 1, 14, 30, tzinfo=timezone.utc)
        return [
            SimpleNamespace(
                date=start + pd.Timedelta(minutes=5 * i),
                open=100 + i * 0.1, high=100.5 + i * 0.1,
                low=99.8 + i * 0.1, close=100.2 + i * 0.1,
                volume=1000 + i * 50,
            )
            for i in range(n)
        ]

    def test_happy_path_no_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BACKTEST_CACHE_DIR", str(tmp_path))
        # Force re-import to pick up the env var? The _CACHE_DIR is read
        # at import — skip caching by passing use_cache=False.
        mock_ib = MagicMock()
        mock_ib.qualifyContracts.return_value = [
            SimpleNamespace(conId=12345),
        ]
        mock_ib.reqHistoricalData.return_value = self._make_fake_bars(20)

        with patch("backtest_engine.data_provider_ib.IB", return_value=mock_ib), \
             patch("backtest_engine.data_provider_ib._build_ib_contract",
                   return_value=SimpleNamespace(conId=0)):
            spec = IBContractSpec(
                sec_type="STK", symbol="QQQ", exchange="SMART",
            )
            df = fetch_bars_ib(
                spec, interval="5m", duration_days=5, use_cache=False,
            )

        assert len(df) == 20
        assert str(df.index.tz) == "UTC"
        mock_ib.connect.assert_called_once()
        mock_ib.disconnect.assert_called_once()

    def test_unqualified_contract_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BACKTEST_CACHE_DIR", str(tmp_path))
        mock_ib = MagicMock()
        # qualifyContracts returns empty → should raise
        mock_ib.qualifyContracts.return_value = []

        with patch("backtest_engine.data_provider_ib.IB", return_value=mock_ib), \
             patch("backtest_engine.data_provider_ib._build_ib_contract",
                   return_value=SimpleNamespace(conId=0)):
            spec = IBContractSpec(
                sec_type="FOP", symbol="MNQ", exchange="GLOBEX",
                last_trade_date="20260619", strike=22500, right="C",
            )
            with pytest.raises(RuntimeError, match="Could not qualify"):
                fetch_bars_ib(spec, interval="5m", use_cache=False)

        # Still disconnects even on error
        mock_ib.disconnect.assert_called_once()

    def test_duration_clamped_to_max(self, monkeypatch, tmp_path, caplog):
        """Requesting 1000 days of 1m bars should clamp to the IB limit."""
        monkeypatch.setenv("BACKTEST_CACHE_DIR", str(tmp_path))
        mock_ib = MagicMock()
        mock_ib.qualifyContracts.return_value = [SimpleNamespace(conId=1)]
        mock_ib.reqHistoricalData.return_value = []

        with patch("backtest_engine.data_provider_ib.IB", return_value=mock_ib), \
             patch("backtest_engine.data_provider_ib._build_ib_contract",
                   return_value=SimpleNamespace(conId=0)), \
             caplog.at_level("WARNING"):
            spec = IBContractSpec(sec_type="STK", symbol="QQQ", exchange="SMART")
            fetch_bars_ib(spec, interval="1m", duration_days=1000, use_cache=False)

        # Check the warning was logged
        assert any("Clamping duration" in rec.message for rec in caplog.records)
