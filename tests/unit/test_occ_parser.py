"""
Unit tests for utils/occ_parser.py — OCC option symbol parsing.

Covers:
- Standard OCC format parsing
- IB-padded format (spaces between ticker and date)
- Invalid symbols (should return None)
- is_expired() based on date
- build_occ() round-trip
- normalize_symbol() whitespace handling
"""
import pytest
from datetime import date, timedelta
from utils.occ_parser import parse_occ, build_occ, is_expired, normalize_symbol, ParsedOption


class TestParseOCC:
    """Tests for parse_occ() function."""

    def test_parse_standard_call(self):
        result = parse_occ("QQQ260415C00634000")
        assert result is not None
        assert result.ticker == "QQQ"
        assert result.right == "C"
        assert result.strike == 634.0
        assert result.expiry == date(2026, 4, 15)
        assert result.is_call
        assert not result.is_put

    def test_parse_standard_put(self):
        result = parse_occ("SPY260416P00700000")
        assert result is not None
        assert result.ticker == "SPY"
        assert result.right == "P"
        assert result.strike == 700.0
        assert result.is_put
        assert not result.is_call

    def test_parse_ib_padded_format(self):
        """IB returns symbols with spaces — must still parse."""
        result = parse_occ("QQQ   260415C00634000")
        assert result is not None
        assert result.ticker == "QQQ"
        assert result.strike == 634.0

    def test_parse_longer_ticker(self):
        """Tickers can be up to 5-6 chars (e.g., GOOGL)."""
        result = parse_occ("GOOGL 260417P00337500")
        assert result is not None
        assert result.ticker == "GOOGL"
        assert result.strike == 337.5

    def test_parse_fractional_strike(self):
        """Options can have half-dollar strikes (e.g., $412.50)."""
        result = parse_occ("MSFT260415C00412500")
        assert result is not None
        assert result.strike == 412.5

    def test_parse_invalid_returns_none(self):
        assert parse_occ("") is None
        assert parse_occ("INVALID") is None
        assert parse_occ("QQQ") is None
        assert parse_occ("QQQ260415") is None  # missing right + strike
        assert parse_occ("QQQ260415X00634000") is None  # X not C/P

    def test_parse_none_returns_none(self):
        assert parse_occ(None) is None

    def test_parse_invalid_date_returns_none(self):
        """Date like 260230 (Feb 30) should fail to parse."""
        assert parse_occ("QQQ260230C00634000") is None

    def test_parse_display(self):
        """Human-friendly display format."""
        result = parse_occ("QQQ260415C00634000")
        assert result.expiry_display == "Apr 15"
        assert result.display == "Apr 15 $634.0 Call"

        result_put = parse_occ("QQQ260415P00634000")
        assert result_put.display == "Apr 15 $634.0 Put"


class TestIsExpired:
    """Tests for is_expired() function."""

    def test_future_date_not_expired(self):
        """An option expiring in the future is not expired."""
        future_date = date.today() + timedelta(days=7)
        yymmdd = future_date.strftime("%y%m%d")
        symbol = f"QQQ{yymmdd}C00634000"
        assert not is_expired(symbol)

    def test_past_date_expired(self):
        """An option with past expiration is expired."""
        past_date = date.today() - timedelta(days=7)
        yymmdd = past_date.strftime("%y%m%d")
        symbol = f"QQQ{yymmdd}C00634000"
        assert is_expired(symbol)

    def test_invalid_symbol_not_expired(self):
        """Unparseable symbols return False (safe default)."""
        assert not is_expired("INVALID")
        assert not is_expired("")
        assert not is_expired(None)


class TestBuildOCC:
    """Tests for build_occ() function."""

    def test_build_round_trip(self):
        """build_occ should produce a parseable symbol."""
        symbol = build_occ("QQQ", "260415", "C", 634.0)
        assert symbol == "QQQ260415C00634000"
        parsed = parse_occ(symbol)
        assert parsed is not None
        assert parsed.ticker == "QQQ"
        assert parsed.strike == 634.0

    def test_build_fractional_strike(self):
        symbol = build_occ("MSFT", "260415", "P", 412.5)
        assert symbol == "MSFT260415P00412500"


class TestNormalizeSymbol:
    """Tests for normalize_symbol() function."""

    def test_strips_spaces(self):
        assert normalize_symbol("QQQ   260415C00634000") == "QQQ260415C00634000"
        assert normalize_symbol("GOOGL 260417P00337500") == "GOOGL260417P00337500"

    def test_strips_leading_trailing_whitespace(self):
        assert normalize_symbol("  QQQ260415C00634000  ") == "QQQ260415C00634000"

    def test_empty_returns_empty(self):
        assert normalize_symbol("") == ""
        assert normalize_symbol(None) == ""


class TestParsedOptionRoundTrip:
    """Integration: parse → ParsedOption → to_occ → parse again."""

    def test_round_trip(self):
        original = "QQQ260415C00634000"
        parsed = parse_occ(original)
        reconstructed = parsed.to_occ()
        assert reconstructed == original
