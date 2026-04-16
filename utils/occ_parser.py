"""
OCC Option Symbol Parser — shared utility for parsing and formatting
option symbols in OCC format (e.g., QQQ260415C00634000).

Used by: ib_client, exit_manager, trade table frontend, reconciliation, emailer.

OCC format: {TICKER}{YYMMDD}{C|P}{SSSSSSSS}
  - TICKER: 1-6 uppercase letters
  - YYMMDD: expiration date
  - C/P: call or put
  - SSSSSSSS: strike price × 1000, zero-padded to 8 digits
"""
import re
from datetime import date
from dataclasses import dataclass


@dataclass
class ParsedOption:
    """Parsed OCC option symbol."""
    ticker: str
    expiry: date         # expiration date
    expiry_str: str      # YYMMDD format
    right: str           # 'C' or 'P'
    strike: float        # strike price (e.g., 634.0)
    raw_symbol: str      # original symbol (spaces stripped)

    @property
    def is_call(self) -> bool:
        return self.right == "C"

    @property
    def is_put(self) -> bool:
        return self.right == "P"

    @property
    def is_expired(self) -> bool:
        return self.expiry < date.today()

    @property
    def expiry_display(self) -> str:
        """Human-friendly: 'Apr 15'"""
        return self.expiry.strftime("%b %d")

    @property
    def display(self) -> str:
        """Human-friendly: 'Apr 15 $634.0 Call'"""
        cp = "Call" if self.is_call else "Put"
        return f"{self.expiry_display} ${self.strike} {cp}"

    def to_occ(self) -> str:
        """Convert back to OCC format: QQQ260415C00634000"""
        strike_str = str(int(self.strike * 1000)).zfill(8)
        return f"{self.ticker}{self.expiry_str}{self.right}{strike_str}"


# Regex for OCC format (with optional spaces — IB pads with spaces)
_OCC_PATTERN = re.compile(r'^([A-Z]+)\s*(\d{6})([CP])(\d{8})$')


def parse_occ(symbol: str) -> ParsedOption | None:
    """
    Parse an OCC option symbol string.
    Handles both compact (QQQ260415C00634000) and IB-padded (QQQ   260415C00634000) formats.
    Returns ParsedOption or None if not a valid OCC symbol.
    """
    if not symbol:
        return None
    cleaned = symbol.strip()
    match = _OCC_PATTERN.match(cleaned)
    if not match:
        return None

    ticker = match.group(1)
    exp_str = match.group(2)
    right = match.group(3)
    strike = int(match.group(4)) / 1000

    try:
        expiry = date(2000 + int(exp_str[:2]), int(exp_str[2:4]), int(exp_str[4:6]))
    except (ValueError, IndexError):
        return None

    return ParsedOption(
        ticker=ticker,
        expiry=expiry,
        expiry_str=exp_str,
        right=right,
        strike=strike,
        raw_symbol=cleaned.replace(" ", ""),
    )


def build_occ(ticker: str, expiry_yymmdd: str, right: str, strike: float) -> str:
    """Build an OCC symbol from components."""
    strike_str = str(int(strike * 1000)).zfill(8)
    return f"{ticker}{expiry_yymmdd}{right}{strike_str}"


def is_expired(symbol: str) -> bool:
    """Check if an option symbol is expired. Safe — returns False if unparseable."""
    parsed = parse_occ(symbol)
    return parsed.is_expired if parsed else False


def normalize_symbol(symbol: str) -> str:
    """Strip spaces from IB-padded symbol. 'QQQ   260415C00634000' → 'QQQ260415C00634000'"""
    return symbol.replace(" ", "").strip() if symbol else ""
