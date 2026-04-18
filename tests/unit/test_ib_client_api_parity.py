"""
ARCH-003 Phase 2: API parity test for the ib_client mixin split.

The public IBClient API must be IDENTICAL before and after the split.
This test loads a golden manifest captured from the pre-split class and
verifies every method is still present with a compatible signature on
the new mixin-composed IBClient.

If this test fails, it means the refactor silently changed the API —
stop and reconcile before merging.

Run:
    python -m pytest tests/unit/test_ib_client_api_parity.py -v
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from broker.ib_client import IBClient
import broker.ib_client as ib_client_module


MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "ib_client_api_manifest.json"
)


@pytest.fixture(scope="module")
def manifest() -> dict:
    assert MANIFEST_PATH.exists(), f"missing manifest: {MANIFEST_PATH}"
    return json.loads(MANIFEST_PATH.read_text())


def _current_methods() -> dict[str, str]:
    out: dict[str, str] = {}
    for name, fn in inspect.getmembers(IBClient, predicate=inspect.isfunction):
        try:
            out[name] = str(inspect.signature(fn))
        except (TypeError, ValueError):
            out[name] = "(?)"
    return out


class TestApiParity:
    """Lock in the pre-split public API."""

    def test_no_public_method_removed(self, manifest):
        """Every public method in the pre-split class must still exist."""
        current = _current_methods()
        expected_public = {
            name for name in manifest["methods"]
            if not name.startswith("_") or name == "__init__"
        }
        missing = [m for m in expected_public if m not in current]
        assert not missing, f"public methods lost in split: {missing}"

    def test_no_private_helper_removed(self, manifest):
        """Private helpers (_ib_*, _place_order, etc.) should also survive
        — they're called from inside the class and renaming them silently
        would break the class cohesion."""
        current = _current_methods()
        expected_private = [
            name for name in manifest["methods"] if name.startswith("_")
        ]
        missing = [m for m in expected_private if m not in current]
        assert not missing, f"private helpers lost in split: {missing}"

    def test_signatures_match(self, manifest):
        """Every retained method must have the same signature as pre-split."""
        current = _current_methods()
        mismatches = []
        for name, expected_sig in manifest["methods"].items():
            if name not in current:
                continue  # covered by the "removed" tests above
            if current[name] != expected_sig:
                mismatches.append(f"{name}: pre={expected_sig}  post={current[name]}")
        assert not mismatches, "signature drift:\n  " + "\n  ".join(mismatches)

    def test_check_not_flex_still_exported(self, manifest):
        """broker.ib_client._check_not_flex is imported by callers — it
        must remain reachable from the top-level module."""
        assert manifest["module_level_symbols"]["_check_not_flex"] is True
        assert hasattr(ib_client_module, "_check_not_flex"), (
            "broker.ib_client._check_not_flex is no longer importable — "
            "re-export from ib_orders.py was dropped"
        )


class TestClassComposition:
    """Shape of the mixin hierarchy — catches MRO mistakes."""

    def test_mro_contains_all_four_components(self):
        """IBClient must inherit from all three mixins + the core."""
        mro_names = [c.__name__ for c in IBClient.__mro__]
        required = ["IBMarketDataMixin", "IBOrdersMixin",
                    "IBPositionsMixin", "IBClientCore"]
        for name in required:
            assert name in mro_names, f"{name} missing from MRO: {mro_names}"

    def test_mro_order_puts_core_last(self):
        """The core class is the common base — it must come AFTER the mixins
        so mixin methods aren't shadowed by anything on the core."""
        mro_names = [c.__name__ for c in IBClient.__mro__]
        core_idx = mro_names.index("IBClientCore")
        for mixin in ("IBMarketDataMixin", "IBOrdersMixin", "IBPositionsMixin"):
            assert mro_names.index(mixin) < core_idx, (
                f"{mixin} resolves after IBClientCore — MRO is wrong"
            )


class TestInstantiationWithoutIB:
    """Create an IBClient in legacy mode and verify its initial state.
    Does NOT call .connect() — we're only testing the constructor path."""

    def test_legacy_mode_init(self):
        client = IBClient()
        # Legacy-mode flags
        assert client._pool_mode is False
        assert client._connected is False
        # Queues exist
        assert hasattr(client, "_order_queue")
        assert hasattr(client, "_priority_queue")
        assert hasattr(client, "_last_errors")
        # Cache exists
        assert isinstance(client._contract_cache, dict)

    def test_pool_mode_init_with_mock_connection(self):
        """Pool-mode init with an injected mock connection — this is how
        the connection pool wires real clients."""
        from unittest.mock import MagicMock
        mock_conn = MagicMock()
        mock_conn.ib = MagicMock()
        mock_conn.connected = True
        cache: dict = {}
        import threading
        lock = threading.Lock()

        client = IBClient(connection=mock_conn, contract_cache=cache,
                          cache_lock=lock)

        assert client._pool_mode is True
        assert client._connected is True
        assert client._conn is mock_conn
        assert client.ib is mock_conn.ib
        assert client._contract_cache is cache
        assert client._cache_lock is lock

    def test_methods_are_callable_attributes(self):
        """Smoke check — the handful of critical methods are bound to the
        instance (catch weird __getattr__ / descriptor issues)."""
        client = IBClient()
        required = [
            "buy_call", "buy_put", "sell_call", "sell_put",
            "place_bracket_order", "update_bracket_sl",
            "cancel_order_by_id", "cancel_bracket_children",
            "find_open_orders_for_contract", "check_bracket_orders_active",
            "cleanup_orphaned_orders",
            "get_realtime_equity_price", "get_atm_call_symbol",
            "get_atm_put_symbol", "get_option_price",
            "get_option_prices_batch", "get_option_greeks", "get_vix",
            "validate_contract",
            "get_ib_positions_raw", "get_position_quantity",
            "get_open_positions", "check_recent_fills",
            "check_fill_by_conid",
            "connect", "process_orders",
        ]
        for name in required:
            m = getattr(client, name, None)
            assert m is not None, f"{name} missing on instance"
            assert callable(m), f"{name} is not callable"


class TestMixinModuleImports:
    """Confirm the three new mixin modules are importable and well-formed."""

    def test_market_data_module(self):
        from broker.ib_market_data import IBMarketDataMixin
        assert hasattr(IBMarketDataMixin, "get_option_price")
        assert hasattr(IBMarketDataMixin, "get_vix")

    def test_orders_module(self):
        from broker.ib_orders import IBOrdersMixin, _check_not_flex
        assert hasattr(IBOrdersMixin, "buy_call")
        assert hasattr(IBOrdersMixin, "place_bracket_order")
        assert callable(_check_not_flex)

    def test_positions_module(self):
        from broker.ib_positions import IBPositionsMixin
        assert hasattr(IBPositionsMixin, "get_ib_positions_raw")
        assert hasattr(IBPositionsMixin, "get_position_quantity")

    def test_each_mixin_has_disjoint_methods(self):
        """Mixins shouldn't accidentally define the same method (would
        cause silent override via MRO)."""
        from broker.ib_market_data import IBMarketDataMixin
        from broker.ib_orders import IBOrdersMixin
        from broker.ib_positions import IBPositionsMixin

        def methods(cls):
            return {n for n, _ in inspect.getmembers(cls, inspect.isfunction)
                    if not n.startswith("__")}

        md = methods(IBMarketDataMixin)
        od = methods(IBOrdersMixin)
        pd = methods(IBPositionsMixin)

        assert md & od == set(), f"market_data + orders overlap: {md & od}"
        assert md & pd == set(), f"market_data + positions overlap: {md & pd}"
        assert od & pd == set(), f"orders + positions overlap: {od & pd}"
