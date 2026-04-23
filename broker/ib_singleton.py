"""Process-level IB client singleton.

The trading strategies are normally pure functions (no side effects,
no I/O). But a couple of entries — notably ENH-035 production IV
detection for DeltaNeutralStrategy — need a live quote. Rather than
plumb the client through every call site, main.py registers it here
at bot startup and any strategy that wants to opt into a live lookup
can read via ``get_client()``.

Backtest and unit tests leave this registry empty. Strategies that
try to fetch a quote see ``None`` and silently fall back to
historical-data-only calculations. No hard dependency.
"""
from __future__ import annotations
from typing import Optional

_CLIENT = None


def set_client(client) -> None:
    """Called once by main.py after IBClient connects."""
    global _CLIENT
    _CLIENT = client


def get_client():
    """Returns the registered client or ``None`` if not set."""
    return _CLIENT


def clear() -> None:
    """Test helper — drop the registered client."""
    global _CLIENT
    _CLIENT = None
