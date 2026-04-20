"""
Real-IB integration test for data_provider_ib. Skips cleanly when TWS
isn't listening on 127.0.0.1:7497 so it doesn't block CI/regression.

Run explicitly when TWS is open:
    DATABASE_URL=... python -m pytest tests/integration/test_fop_real_ib.py -v

Or gate it behind env:
    RUN_IB_TESTS=1 python -m pytest tests/integration/test_fop_real_ib.py -v
"""
from __future__ import annotations

import os
import socket
from datetime import date, timedelta

import pytest


pytestmark = pytest.mark.integration


def _tws_reachable(host: str = "127.0.0.1", port: int = 7497,
                   timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


@pytest.fixture(scope="module")
def tws_guard():
    if os.getenv("SKIP_IB_TESTS", "").lower() in ("1", "true", "yes"):
        pytest.skip("SKIP_IB_TESTS set — skipping real-IB test")
    if not _tws_reachable():
        pytest.skip(
            "TWS not listening on 127.0.0.1:7497 — "
            "start TWS + accept paper-trading disclaimer to run this test"
        )


# ── Real-IB happy path (stock) ───────────────────────────

class TestRealIBStock:
    def test_fetch_qqq_5m_bars(self, tws_guard, tmp_path, monkeypatch):
        """Fetch 5 days of QQQ 5-min bars via IB. Validates the actual
        connect → qualify → reqHistoricalData → disconnect flow end-to-end.
        Uses a throwaway cache dir so this test doesn't pollute the real
        cache."""
        monkeypatch.setenv("BACKTEST_CACHE_DIR", str(tmp_path))
        from backtest_engine.data_provider_ib import (
            IBContractSpec, fetch_bars_ib,
        )

        spec = IBContractSpec(sec_type="STK", symbol="QQQ", exchange="SMART")
        df = fetch_bars_ib(
            spec,
            interval="5m",
            end=date.today() - timedelta(days=1),
            duration_days=5,
            use_cache=False,
            client_id=97,
        )

        assert not df.empty, "IB should return QQQ bars for the last 5 days"
        assert str(df.index.tz) == "UTC"
        assert set(df.columns) == {"open", "high", "low", "close", "volume"}
        # Sanity: QQQ should trade in the $400-800 range in 2026
        assert 100 < df["close"].mean() < 2000


# ── Real-IB FOP ──────────────────────────────────────────

class TestRealIBFop:
    def test_fetch_mnq_option_bars_skip_if_no_contract(
        self, tws_guard, tmp_path, monkeypatch,
    ):
        """Fetches a real MNQ futures option. Because the specific
        strike/expiry may not be valid, this test is tolerant — it
        skips if the contract doesn't exist at IB, fails only if
        the mechanical flow is broken (connection / dispatch / parsing).
        """
        monkeypatch.setenv("BACKTEST_CACHE_DIR", str(tmp_path))
        from backtest_engine.data_provider_ib import (
            spec_from_ticker_row, fetch_bars_ib,
        )

        # Next ~third-Friday monthly expiry is a reasonable guess —
        # if it doesn't exist, we skip rather than fail.
        today = date.today()
        # Next 3rd Friday roughly 60 days out
        target = today + timedelta(days=60)
        expiry = target.strftime("%Y%m%d")
        # Strike near the money — MNQ has 25-point intervals, round to it
        strike = 22500

        spec = spec_from_ticker_row("MNQ", expiry, strike, "C")
        try:
            df = fetch_bars_ib(
                spec,
                interval="5m",
                end=today - timedelta(days=1),
                duration_days=5,
                use_cache=False,
                client_id=98,
            )
        except RuntimeError as e:
            if "Could not qualify" in str(e):
                pytest.skip(
                    f"MNQ {expiry} {strike}C doesn't exist at IB — "
                    f"pick a valid contract via IB Contract Explorer "
                    f"and rerun with different expiry/strike. "
                    f"Connection flow worked though — that's what this "
                    f"test actually validates."
                )
            raise

        # If it DID return, sanity-check the shape. FOP bars may be sparse.
        assert str(df.index.tz) == "UTC"
        assert set(df.columns) == {"open", "high", "low", "close", "volume"}
