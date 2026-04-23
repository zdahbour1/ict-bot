"""Regression tests for the OCC-symbol whitespace bug caught 2026-04-20.

Incident: reconciliation adopted IB positions using ``localSymbol``
with .strip(), which leaves internal whitespace intact ('QQQ   260420...').
Downstream ib_occ_to_contract regex rejects it, batch price fetch
returns None, exit_manager silently skips the trade, and
update_trade_price never fires. updated_at stays stuck at created_at
and the trade is effectively unmonitored.

Fix: _normalize_occ() (broker/ib_client.py) strips ALL whitespace,
including internal padding. Also applied defensively in
strategy/reconciliation.py.
"""
from __future__ import annotations

import re

import pytest


class TestNormalizeOcc:
    def test_padded_symbol_collapses_to_canonical(self):
        from broker.ib_client import _normalize_occ
        assert _normalize_occ("QQQ   260420C00645000") == "QQQ260420C00645000"
        assert _normalize_occ("AMZN  260420P00247500") == "AMZN260420P00247500"
        assert _normalize_occ("SPY   260424C00540000") == "SPY260424C00540000"

    def test_already_canonical_unchanged(self):
        from broker.ib_client import _normalize_occ
        assert _normalize_occ("AMD260424C00280000") == "AMD260424C00280000"

    def test_none_and_empty(self):
        from broker.ib_client import _normalize_occ
        assert _normalize_occ(None) == ""
        assert _normalize_occ("") == ""
        assert _normalize_occ("   ") == ""

    def test_trailing_whitespace(self):
        from broker.ib_client import _normalize_occ
        assert _normalize_occ(" QQQ260420C00645000 ") == "QQQ260420C00645000"

    def test_output_passes_ib_occ_regex(self):
        """The canonical output MUST match ib_occ_to_contract's regex —
        that's the whole point of normalization."""
        from broker.ib_client import _normalize_occ
        # Same regex used by broker/ib_contracts.py::ib_occ_to_contract
        occ_re = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")
        samples = [
            "QQQ   260420C00645000",
            "AMZN  260420P00247500",
            "IWM   260420C00275000",
            "TSLA  260420P00397500",
            "NFLX  260425P00690000",
        ]
        for raw in samples:
            clean = _normalize_occ(raw)
            assert occ_re.match(clean), (
                f"{raw!r} normalized to {clean!r} which fails the OCC regex"
            )


class TestReconciliationSymbolCleaning:
    """The reconcile adopt path must clean whitespace even if some
    upstream source forgot the normalizer. Belt-and-suspenders."""

    def test_adopt_path_strips_internal_whitespace(self):
        # This mirrors the inline expression in reconciliation.py:
        #   sym = "".join((pos.get("symbol") or "").split())
        raw = "QQQ   260420C00645000"
        clean = "".join((raw or "").split())
        assert clean == "QQQ260420C00645000"

    def test_adopt_path_handles_none(self):
        raw = None
        clean = "".join((raw or "").split())
        assert clean == ""


class TestReconciliationContractSignNormalization:
    """Regression tests for the 2026-04-23 ``-4x`` AMZN bug. IB reports
    naked-short positions with NEGATIVE qty; previous reconciliation
    code wrote that raw value to ``trade_legs.contracts_entered``,
    which broke every downstream P&L calc and UI column. Fix: always
    store positive contracts, derive direction from BOTH position
    sign and right.
    """

    @staticmethod
    def _adopt_fields(qty_raw: int, right: str):
        """Mirror the inline expression in reconciliation.py::_pass2_adopt."""
        qty = abs(qty_raw)
        if qty_raw >= 0:
            direction = "LONG" if right == "C" else "SHORT"
        else:
            direction = "SHORT" if right == "C" else "LONG"
        return qty, direction

    def test_long_call_stays_long(self):
        qty, direction = self._adopt_fields(4, "C")
        assert qty == 4 and direction == "LONG"

    def test_long_put_is_short_bias(self):
        # Bought a put → bearish trade
        qty, direction = self._adopt_fields(4, "P")
        assert qty == 4 and direction == "SHORT"

    def test_naked_short_put_is_long_bias(self):
        # Sold a put → bullish trade (AMZN regression case)
        qty, direction = self._adopt_fields(-4, "P")
        assert qty == 4            # MUST be positive, never -4
        assert direction == "LONG"

    def test_naked_short_call_is_short_bias(self):
        # Sold a call → bearish trade
        qty, direction = self._adopt_fields(-4, "C")
        assert qty == 4 and direction == "SHORT"

    def test_zero_qty_treated_as_long(self):
        # Degenerate — shouldn't happen in production but we pick a branch
        qty, direction = self._adopt_fields(0, "C")
        assert qty == 0 and direction == "LONG"
