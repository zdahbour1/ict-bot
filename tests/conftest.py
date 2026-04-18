"""
Pytest configuration and shared fixtures for ICT Trading Bot tests.

Fixtures defined here are automatically available to all test files.

Fixture scopes:
- function (default): new instance per test
- class: new instance per test class
- module: new instance per test file
- session: one instance for entire test run
"""
import os
import sys
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest

# Add project root to sys.path so tests can import bot modules
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ── Pytest → DB reporter ─────────────────────────────────────
# Opt-in: set PYTEST_DB_REPORT=1 to persist runs to Postgres.
# Without it, pytest runs normally with no DB side effects.
pytest_plugins = ["tests.pytest_db_reporter"]


# ── Mock IB Contract ─────────────────────────────────────────

class MockContract:
    """Mock IB Option/Stock contract for testing."""
    def __init__(self, symbol="QQQ", localSymbol="QQQ   260415C00634000",
                 conId=123456789, secType="OPT", exchange="SMART",
                 strike=634.0, right="C", tradingClass=None):
        self.symbol = symbol
        self.localSymbol = localSymbol
        self.conId = conId
        self.secType = secType
        self.exchange = exchange
        self.currency = "USD"
        self.strike = strike
        self.right = right
        self.lastTradeDateOrContractMonth = "20260415"
        self.multiplier = "100"
        self.tradingClass = tradingClass or symbol


@pytest.fixture
def mock_contract():
    """Standard mock option contract for a QQQ call."""
    return MockContract()


@pytest.fixture
def mock_put_contract():
    """Mock put contract for tests requiring shorts."""
    return MockContract(
        symbol="QQQ", localSymbol="QQQ   260415P00634000",
        conId=987654321, right="P"
    )


# ── Mock IB Position ─────────────────────────────────────────

class MockPosition:
    """Mock IB position for testing reconciliation and position checks."""
    def __init__(self, contract=None, position=2.0, avgCost=245.0, marketPrice=1.5):
        self.contract = contract or MockContract()
        self.position = position
        self.avgCost = avgCost
        self.marketPrice = marketPrice


@pytest.fixture
def mock_position():
    """Standard mock position: 2 long QQQ calls."""
    return MockPosition()


@pytest.fixture
def mock_negative_position():
    """Mock naked short position (should never exist but needs testing)."""
    return MockPosition(position=-2.0)


# ── Mock IB Order ────────────────────────────────────────────

class MockOrder:
    """Mock IB order for testing order placement/cancellation."""
    def __init__(self, orderId=1001, permId=9001001, action="BUY",
                 totalQuantity=2, orderType="MKT"):
        self.orderId = orderId
        self.permId = permId
        self.action = action
        self.totalQuantity = totalQuantity
        self.orderType = orderType


class MockOrderStatus:
    """Mock IB order status."""
    def __init__(self, status="Filled", avgFillPrice=2.45):
        self.status = status
        self.avgFillPrice = avgFillPrice


class MockTrade:
    """Mock IB trade object (order + contract + status)."""
    def __init__(self, contract=None, order=None, status="Filled", fill_price=2.45):
        self.contract = contract or MockContract()
        self.order = order or MockOrder()
        self.orderStatus = MockOrderStatus(status=status, avgFillPrice=fill_price)
        self.fills = []


# ── Mock IB Client (facade) ──────────────────────────────────

@pytest.fixture
def mock_ib_client():
    """Fully mocked IB client with common method stubs."""
    client = MagicMock()

    # Default behaviors
    client.get_ib_positions_raw.return_value = []
    client.get_position_quantity.return_value = 0
    client.find_open_orders_for_contract.return_value = []
    client.check_recent_fills.return_value = None
    client.check_fill_by_conid.return_value = None
    client.cancel_bracket_children.return_value = None
    client.cancel_order_by_id.return_value = None

    # Order placement returns a filled trade dict
    client.place_bracket_order.return_value = {
        "symbol": "QQQ260415C00634000",
        "contracts": 2,
        "order_id": 1001,
        "perm_id": 9001001,
        "con_id": 123456789,
        "tp_order_id": 1002,
        "tp_perm_id": 9001002,
        "sl_order_id": 1003,
        "sl_perm_id": 9001003,
        "status": "Filled",
        "fill_price": 2.45,
    }

    return client


# ── Mock Trade Dict ──────────────────────────────────────────

@pytest.fixture
def sample_trade_dict():
    """Standard trade dict as used by exit_manager and DB writer."""
    return {
        "db_id": 1,
        "ticker": "QQQ",
        "symbol": "QQQ260415C00634000",
        "contracts": 2,
        "direction": "LONG",
        "entry_price": 2.45,
        "entry_time": datetime(2026, 4, 15, 7, 30, tzinfo=timezone.utc),
        "current_price": 2.45,
        "profit_target": 4.90,
        "stop_loss": 0.98,
        "ib_con_id": 123456789,
        "ib_order_id": 1001,
        "ib_perm_id": 9001001,
        "ib_tp_order_id": 1002,
        "ib_sl_order_id": 1003,
        "peak_pnl_pct": 0.0,
        "dynamic_sl_pct": -0.60,
        "signal_type": "LONG_iFVG",
    }


@pytest.fixture
def sample_short_trade_dict(sample_trade_dict):
    """Short (PUT) trade dict."""
    trade = sample_trade_dict.copy()
    trade.update({
        "symbol": "QQQ260415P00634000",
        "direction": "SHORT",
        "signal_type": "SHORT_OB",
    })
    return trade


# ── Mock Price Bars ──────────────────────────────────────────

@pytest.fixture
def sample_bars_1m():
    """Generate 120 1-minute bars for signal detection tests."""
    import pandas as pd
    import numpy as np
    dates = pd.date_range("2026-04-15 13:30", periods=120, freq="1min", tz="UTC")
    np.random.seed(42)
    prices = 634.0 + np.cumsum(np.random.randn(120) * 0.1)
    return pd.DataFrame({
        "open": prices,
        "high": prices + np.random.uniform(0.05, 0.15, 120),
        "low": prices - np.random.uniform(0.05, 0.15, 120),
        "close": prices + np.random.uniform(-0.05, 0.05, 120),
        "volume": np.random.randint(1000, 10000, 120),
    }, index=dates)


# ── Frozen Time ──────────────────────────────────────────────

@pytest.fixture
def market_hours_time():
    """Time during market hours PT (7:30 AM)."""
    return datetime(2026, 4, 15, 14, 30, tzinfo=timezone.utc)  # 7:30 AM PT


@pytest.fixture
def after_hours_time():
    """Time after market close PT (2:00 PM)."""
    return datetime(2026, 4, 15, 21, 0, tzinfo=timezone.utc)  # 2:00 PM PT
