"""Unit tests for db.trade_ref.

The ref generator must be:
  - Deterministic shape: TICKER-YYMMDD-NN
  - Per-ticker, per-day unique within the DB
  - Never-block: if DB is unreachable, fall back to timestamp-seeded
    ordinal so entry flow continues (IntegrityError on unique index
    catches the rare collision).
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytz
import pytest


PT = pytz.timezone("America/Los_Angeles")


class TestGenerateTradeRef:
    def test_format_is_ticker_yymmdd_nn(self):
        from db.trade_ref import generate_trade_ref
        with patch("db.connection.get_session") as _mock:
            # Simulate empty DB → MAX returns NULL → ordinal starts at 1
            _mock.return_value = None
            ref = generate_trade_ref("INTC", now=PT.localize(datetime(2026, 4, 21, 9, 30)))
        # Fallback path triggered by None session → uses _fallback_ordinal
        assert ref.startswith("INTC-260421-")

    def test_first_trade_of_day_has_ordinal_01(self):
        from db.trade_ref import generate_trade_ref
        # DB returns MAX=0 → next = 1
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = (0,)
        with patch("db.connection.get_session", return_value=session):
            ref = generate_trade_ref("INTC", now=PT.localize(datetime(2026, 4, 21, 9, 30)))
        assert ref == "INTC-260421-01"

    def test_second_trade_same_day_gets_02(self):
        from db.trade_ref import generate_trade_ref
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = (1,)
        with patch("db.connection.get_session", return_value=session):
            ref = generate_trade_ref("INTC", now=PT.localize(datetime(2026, 4, 21, 10, 0)))
        assert ref == "INTC-260421-02"

    def test_n_widens_past_99(self):
        from db.trade_ref import generate_trade_ref
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = (99,)
        with patch("db.connection.get_session", return_value=session):
            ref = generate_trade_ref("SPY", now=PT.localize(datetime(2026, 4, 21, 11, 0)))
        # 100th trade — padding widens to 3 digits
        assert ref == "SPY-260421-100"

    def test_different_tickers_independent(self):
        """INTC and AAPL each start at 01 on their own days."""
        from db.trade_ref import generate_trade_ref
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = (0,)
        with patch("db.connection.get_session", return_value=session):
            intc = generate_trade_ref("INTC", now=PT.localize(datetime(2026, 4, 21, 9, 30)))
            aapl = generate_trade_ref("AAPL", now=PT.localize(datetime(2026, 4, 21, 9, 30)))
        assert intc == "INTC-260421-01"
        assert aapl == "AAPL-260421-01"

    def test_uppercase_ticker(self):
        from db.trade_ref import generate_trade_ref
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = (0,)
        with patch("db.connection.get_session", return_value=session):
            ref = generate_trade_ref("intc", now=PT.localize(datetime(2026, 4, 21, 9, 30)))
        assert ref.startswith("INTC-")

    def test_db_failure_uses_fallback(self):
        """If the SELECT raises, generate_trade_ref must still return
        a well-formed ref — fallback ordinal from the clock."""
        from db.trade_ref import generate_trade_ref
        session = MagicMock()
        session.execute.side_effect = RuntimeError("DB dead")
        with patch("db.connection.get_session", return_value=session):
            ref = generate_trade_ref("INTC", now=PT.localize(datetime(2026, 4, 21, 9, 30, 15)))
        # Fallback: 9*3600 + 30*60 + 15 = 34215
        assert ref == "INTC-260421-34215"

    def test_length_bounded_under_20(self):
        """DB column is VARCHAR(20). Even padded refs must fit."""
        from db.trade_ref import generate_trade_ref
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = (999,)
        with patch("db.connection.get_session", return_value=session):
            ref = generate_trade_ref("NVDA", now=PT.localize(datetime(2026, 4, 21, 9, 30)))
        assert len(ref) < 20, f"ref {ref!r} too long"


class TestStrategyPrefix:
    """New format: '<strategy>-TICKER-YYMMDD-NN' (e.g. 'ict-SPY-260421-01').

    Prefix is optional — when strategy isn't resolvable we fall back to
    the legacy shape so entry flow never blocks.
    """
    def test_explicit_strategy_name_prefixes_ref(self):
        from db.trade_ref import generate_trade_ref
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = (0,)
        with patch("db.connection.get_session", return_value=session):
            ref = generate_trade_ref(
                "SPY",
                now=PT.localize(datetime(2026, 4, 21, 9, 30)),
                strategy_name="ict",
            )
        assert ref == "ict-SPY-260421-01"

    def test_long_strategy_name_fits_and_parses(self):
        from db.trade_ref import generate_trade_ref, parse_trade_ref
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = (4,)
        with patch("db.connection.get_session", return_value=session):
            ref = generate_trade_ref(
                "NVDA",
                now=PT.localize(datetime(2026, 4, 21, 10, 0)),
                strategy_name="vwap_revert",
            )
        assert ref == "vwap_revert-NVDA-260421-05"
        # Must fit in the widened VARCHAR(40) column
        assert len(ref) <= 40
        info = parse_trade_ref(ref)
        assert info == {
            "strategy": "vwap_revert",
            "ticker": "NVDA",
            "date": "260421",
            "ordinal": 5,
        }

    def test_explicit_none_strategy_uses_legacy_format(self):
        """strategy_name=None explicitly + DB returns no active strategy
        → legacy ref without prefix (backwards compatible)."""
        from db.trade_ref import generate_trade_ref
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = (0,)
        with patch("db.connection.get_session", return_value=session), \
             patch("db.strategy_writer.get_active_strategy_name",
                   return_value=None):
            ref = generate_trade_ref(
                "SPY",
                now=PT.localize(datetime(2026, 4, 21, 9, 30)),
            )
        # Legacy shape when strategy can't be resolved
        assert ref == "SPY-260421-01"

    def test_active_strategy_resolved_from_db_when_not_passed(self):
        from db.trade_ref import generate_trade_ref
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = (0,)
        with patch("db.connection.get_session", return_value=session), \
             patch("db.strategy_writer.get_active_strategy_name",
                   return_value="orb"):
            ref = generate_trade_ref(
                "AAPL",
                now=PT.localize(datetime(2026, 4, 21, 9, 30)),
            )
        assert ref == "orb-AAPL-260421-01"

    def test_different_strategies_get_own_daily_counters(self):
        """ict and orb each start at 01 — MAX() is scoped to the
        full prefix, so 'ict-SPY-..' and 'orb-SPY-..' don't collide."""
        from db.trade_ref import generate_trade_ref
        # Both lookups return 0 because they query their own LIKE prefix
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = (0,)
        with patch("db.connection.get_session", return_value=session):
            ict = generate_trade_ref("SPY",
                                      now=PT.localize(datetime(2026, 4, 21, 9, 30)),
                                      strategy_name="ict")
            orb = generate_trade_ref("SPY",
                                      now=PT.localize(datetime(2026, 4, 21, 9, 31)),
                                      strategy_name="orb")
        assert ict == "ict-SPY-260421-01"
        assert orb == "orb-SPY-260421-01"


class TestParseTradeRef:
    def test_valid_ref(self):
        from db.trade_ref import parse_trade_ref
        # Legacy no-strategy format still parses (strategy=None)
        info = parse_trade_ref("INTC-260421-07")
        assert info == {
            "strategy": None, "ticker": "INTC", "date": "260421", "ordinal": 7,
        }

    def test_strategy_prefixed_ref(self):
        from db.trade_ref import parse_trade_ref
        info = parse_trade_ref("ict-SPY-260421-07")
        assert info == {
            "strategy": "ict", "ticker": "SPY", "date": "260421", "ordinal": 7,
        }

    def test_three_digit_ordinal(self):
        from db.trade_ref import parse_trade_ref
        info = parse_trade_ref("SPY-260421-142")
        assert info["ordinal"] == 142

    def test_malformed_returns_none(self):
        from db.trade_ref import parse_trade_ref
        assert parse_trade_ref("") is None
        assert parse_trade_ref("garbage") is None
        assert parse_trade_ref("INTC-260421") is None  # missing ordinal
        assert parse_trade_ref("lowercase-260421-01") is None

    def test_none_input(self):
        from db.trade_ref import parse_trade_ref
        assert parse_trade_ref(None) is None
