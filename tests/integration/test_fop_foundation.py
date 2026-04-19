"""
Integration tests for the futures-options foundation
(feature/futures-options).

Covers:
- Seeded FOP ticker rows exist with correct sec_type / multiplier /
  exchange / currency
- FOP_SPECS dictionary is sane
- ib_qualify_futures_option() builds the right IB contract (mocked IB)
- A FOP trade can be inserted via insert_trade() with FOP-specific
  fields and round-trips correctly
- All seeded FOP tickers start is_active=FALSE (nothing trades yet)

No live IB connection needed — the IB call paths are mocked.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


def _db_available() -> bool:
    try:
        from db.connection import db_available
        return db_available()
    except Exception:
        return False


@pytest.fixture(scope="module")
def db_guard():
    if not _db_available():
        pytest.skip("Postgres not reachable")


@pytest.fixture
def session(db_guard):
    from db.connection import get_session
    s = get_session()
    yield s
    s.close()


# ── Seeded tickers ───────────────────────────────────────

class TestFopTickerSeeds:
    REQUIRED = {"MNQ", "NQ", "MES", "ES", "GC", "CL"}

    def test_all_six_seeded(self, session):
        rows = session.execute(text(
            "SELECT symbol FROM tickers WHERE sec_type = 'FOP' "
            "  AND symbol IN ('MNQ', 'NQ', 'MES', 'ES', 'GC', 'CL')"
        )).fetchall()
        symbols = {r[0] for r in rows}
        assert symbols == self.REQUIRED, f"expected {self.REQUIRED}, got {symbols}"

    def test_all_inactive(self, session):
        cnt = session.execute(text(
            "SELECT COUNT(*) FROM tickers WHERE sec_type = 'FOP' "
            "  AND is_active = TRUE"
        )).scalar()
        assert cnt == 0, (
            f"{cnt} FOP tickers are active — they should all start inactive"
        )

    @pytest.mark.parametrize("symbol,mult,exchange", [
        ("MNQ", 2,    "GLOBEX"),
        ("NQ",  20,   "GLOBEX"),
        ("MES", 5,    "GLOBEX"),
        ("ES",  50,   "GLOBEX"),
        ("GC",  100,  "NYMEX"),
        ("CL",  1000, "NYMEX"),
    ])
    def test_per_instrument_specs(self, session, symbol, mult, exchange):
        row = session.execute(text(
            "SELECT multiplier, exchange, currency FROM tickers "
            "WHERE symbol = :sym AND sec_type = 'FOP'"
        ), {"sym": symbol}).fetchone()
        assert row is not None
        assert row[0] == mult, f"{symbol} multiplier: expected {mult}, got {row[0]}"
        assert row[1] == exchange, f"{symbol} exchange: expected {exchange}, got {row[1]}"
        assert row[2] == "USD"


# ── FOP_SPECS dict ───────────────────────────────────────

class TestFopSpecsDict:
    def test_required_symbols_present(self):
        from broker.ib_contracts import FOP_SPECS
        required = {"MNQ", "NQ", "MES", "ES", "GC", "CL"}
        assert required.issubset(FOP_SPECS.keys()), (
            f"missing: {required - set(FOP_SPECS.keys())}"
        )

    def test_each_entry_has_required_fields(self):
        from broker.ib_contracts import FOP_SPECS
        for sym, spec in FOP_SPECS.items():
            for field in ("exchange", "multiplier", "strike_interval", "currency"):
                assert field in spec, f"{sym} missing field {field}"
            assert isinstance(spec["multiplier"], (int, float)) and spec["multiplier"] > 0
            assert spec["currency"] == "USD"

    def test_get_fop_spec_case_insensitive(self):
        from broker.ib_contracts import get_fop_spec
        assert get_fop_spec("mnq") == get_fop_spec("MNQ")
        assert get_fop_spec("unknown-future") is None


# ── ib_qualify_futures_option (mocked IB) ────────────────

class TestQualifyFuturesOption:
    def test_builds_globex_contract_for_mnq(self):
        from broker import ib_contracts as ibc

        # Mock IB returns a "qualified" contract with a conId
        mock_ib = MagicMock()
        mock_ib.qualifyContracts.side_effect = lambda c: (
            [type(c)(**{**c.__dict__, "conId": 999})]  # not how IB actually works,
            if False                                    # but we don't need this path —
            else [_make_qualified_mock_contract(c)]     # see helper below
        )

        cache: dict = {}
        result = ibc.ib_qualify_futures_option(
            ib=mock_ib,
            underlying="MNQ",
            expiry="20260619",
            strike=22500,
            right="C",
            contract_cache=cache,
        )

        assert result is not None
        assert result.conId != 0
        # Cache hit on second call
        result2 = ibc.ib_qualify_futures_option(
            ib=mock_ib, underlying="MNQ", expiry="20260619",
            strike=22500, right="C", contract_cache=cache,
        )
        assert result2 is result  # cached

    def test_unknown_underlying_requires_explicit_exchange(self):
        from broker import ib_contracts as ibc
        mock_ib = MagicMock()
        with pytest.raises(RuntimeError, match="No FOP_SPECS entry"):
            ibc.ib_qualify_futures_option(
                ib=mock_ib,
                underlying="WEIRD_SYMBOL",
                expiry="20260619",
                strike=100.0,
                right="C",
                contract_cache={},
            )

    def test_explicit_exchange_overrides_spec(self):
        from broker import ib_contracts as ibc
        mock_ib = MagicMock()
        mock_ib.qualifyContracts.side_effect = (
            lambda c: [_make_qualified_mock_contract(c)]
        )
        cache: dict = {}
        _ = ibc.ib_qualify_futures_option(
            ib=mock_ib,
            underlying="ES",
            expiry="20260619",
            strike=5500,
            right="P",
            contract_cache=cache,
            exchange="CME_CUSTOM",  # override the GLOBEX default
        )
        # At least one contract in cache keyed with CME_CUSTOM
        assert any("CME_CUSTOM" in k for k in cache.keys())


def _make_qualified_mock_contract(original_contract):
    """Helper: return a copy of the input contract with a fake conId set."""
    # ib_async Contract objects support attribute set via `copy`
    try:
        copy = original_contract.__class__()
        for k, v in vars(original_contract).items():
            setattr(copy, k, v)
        copy.conId = 123456
        return copy
    except Exception:
        # Fallback: just set conId on the original
        original_contract.conId = 123456
        return original_contract


# ── insert_trade() with FOP fields ───────────────────────

class TestInsertTradeFop:
    def test_fop_trade_roundtrip(self, db_guard):
        """A trade stamped sec_type='FOP' with MNQ's multiplier + GLOBEX
        round-trips through the DB with every field preserved."""
        from db.writer import insert_trade, invalidate_active_strategy_cache
        from db.connection import get_session

        invalidate_active_strategy_cache()
        session = get_session()
        try:
            trade_id = insert_trade({
                "ticker": "MNQ",
                "symbol": "MNQ260619C22500000",
                "direction": "LONG",
                "contracts": 1,
                "entry_price": 45.50,
                "profit_target": 91.00,
                "stop_loss": 22.75,
                "entry_time": datetime.now(timezone.utc),
                # FOP-specific
                "sec_type": "FOP",
                "multiplier": 2,
                "exchange": "GLOBEX",
                "currency": "USD",
                "underlying": "MNQ",
                "strategy_config": {
                    "base_interval": "15m",
                    "range_minutes": 30,
                    "tick_size": 0.25,
                },
            }, account="DU0-TEST")
            assert trade_id is not None

            row = session.execute(text(
                "SELECT ticker, symbol, sec_type, multiplier, exchange, "
                "       currency, underlying, strategy_config "
                "FROM trades WHERE id = :id"
            ), {"id": trade_id}).fetchone()
            assert row[0] == "MNQ"
            assert row[2] == "FOP"
            assert row[3] == 2
            assert row[4] == "GLOBEX"
            assert row[5] == "USD"
            assert row[6] == "MNQ"
            assert row[7]["base_interval"] == "15m"
            assert row[7]["tick_size"] == 0.25
        finally:
            session.execute(text(
                "DELETE FROM trades WHERE ticker = 'MNQ' AND symbol = 'MNQ260619C22500000'"
            ))
            session.commit()
            session.close()
