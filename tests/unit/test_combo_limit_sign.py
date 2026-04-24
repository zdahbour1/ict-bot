"""ENH-063 (2026-04-24) — combo BAG limit-price sign convention.

Regression: before the fix, _compute_combo_net_limit submitted
abs(net)+buf as the BUY limit price for a credit iron condor. IB's
price-cap protection clamped the order (TWS popup: "price capped to
-4.42 to avoid execution at a price not consistent with a fair and
orderly market"). The combo then sat unfilled.

The fix: preserve the sign — for a credit spread (net>0) the BUY
limit must be NEGATIVE (signed credit we'll accept), not +abs(net).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


class _FakeMixin:
    """Minimal stub — _compute_combo_net_limit only needs
    get_option_price on the mixin."""
    def __init__(self, mid_by_symbol: dict[str, float]):
        self._mids = mid_by_symbol

    def get_option_price(self, symbol: str, priority: bool = False) -> float:
        return self._mids.get(symbol, 0.0)


def _leg(symbol: str, direction: str, strike: float, right: str):
    return {
        "symbol": symbol, "direction": direction, "strike": strike,
        "right": right, "contracts": 1, "multiplier": 100,
    }


def _leg_contracts(legs):
    """Shape _compute_combo_net_limit expects: list of
    (i, leg_dict, contract_obj) tuples."""
    return [(i, leg, SimpleNamespace(conId=1000 + i))
             for i, leg in enumerate(legs)]


def _run(mixin, legs, action: str, slip_bps: float = 200.0):
    """Call the real _compute_combo_net_limit with settings patched."""
    from broker.ib_orders import _compute_combo_net_limit
    with patch("db.settings_cache.get_bool", return_value=True), \
         patch("db.settings_cache.get_float", return_value=slip_bps):
        return _compute_combo_net_limit(
            mixin, _leg_contracts(legs), action, legs)


class TestCreditIronCondorBuy:
    """Classic credit iron condor: short wings in, long wings out.
    Shorts receive premium > longs pay → net positive credit."""

    def test_buy_credit_condor_submits_negative_limit(self):
        # Short C500 $2.00, Long C510 $0.50 (call credit side: +$1.50)
        # Short P490 $2.00, Long P480 $0.50 (put credit side:  +$1.50)
        # Net credit = +$3.00. Fair BUY limit = -3.00, widened by 2%.
        legs = [
            _leg("SPY260501C00500000", "SHORT", 500.0, "C"),
            _leg("SPY260501C00510000", "LONG",  510.0, "C"),
            _leg("SPY260501P00490000", "SHORT", 490.0, "P"),
            _leg("SPY260501P00480000", "LONG",  480.0, "P"),
        ]
        mids = {
            "SPY260501C00500000": 2.00, "SPY260501C00510000": 0.50,
            "SPY260501P00490000": 2.00, "SPY260501P00480000": 0.50,
        }
        limit = _run(_FakeMixin(mids), legs, action="BUY")
        # BUY a credit spread → signed limit is negative.
        assert limit is not None
        assert limit < 0, (
            f"credit iron condor BUY must submit NEGATIVE limit "
            f"(IB BAG convention: negative = credit accepted). "
            f"Got {limit!r}, which IB would treat as a debit and "
            f"clamp via price-cap protection."
        )
        # Expected: -3.00 + 0.06 buf = -2.94
        assert abs(limit - -2.94) < 0.02

    def test_buy_credit_butterfly_submits_negative_limit(self):
        """Iron butterfly (shorts at same ATM strike) is also a credit
        spread — signed limit must be negative."""
        legs = [
            _leg("SPY260501C00500000", "SHORT", 500.0, "C"),
            _leg("SPY260501C00505000", "LONG",  505.0, "C"),
            _leg("SPY260501P00500000", "SHORT", 500.0, "P"),
            _leg("SPY260501P00495000", "LONG",  495.0, "P"),
        ]
        mids = {
            "SPY260501C00500000": 3.00, "SPY260501C00505000": 1.20,
            "SPY260501P00500000": 3.00, "SPY260501P00495000": 1.20,
        }
        limit = _run(_FakeMixin(mids), legs, action="BUY")
        assert limit is not None and limit < 0


class TestNoMktFallbackOnQuoteFail:
    """ENH-064 — when leg quotes fail, _ib_place_combo must raise
    rather than silently fall back to MarketOrder. MKT BAG combos
    on 4-leg spreads park in PendingSubmit and never fill, which is
    the 'stuck orders' state the user hit 2026-04-24."""

    def test_place_combo_raises_when_limit_acquisition_fails(self):
        """With auto-limit on but every quote returning 0, the combo
        placement must abort, not send MKT."""
        import broker.ib_orders as ibo

        # Stub the leg-quote path to force quote failures
        def _compute_that_fails(*a, **kw):
            return None

        legs = [
            _leg("SPY260501C00500000", "SHORT", 500.0, "C"),
            _leg("SPY260501C00510000", "LONG",  510.0, "C"),
            _leg("SPY260501P00490000", "SHORT", 490.0, "P"),
            _leg("SPY260501P00480000", "LONG",  480.0, "P"),
        ]

        # Minimal fake mixin with the IB placeOrder plumbing we don't
        # want to actually run.  The important thing is that the code
        # raises BEFORE ib.placeOrder is invoked.
        placed = []

        class _FakeIB:
            def __init__(self):
                self.client = SimpleNamespace(clientId=1,
                                                getReqId=lambda: 1)
            def placeOrder(self, c, o):
                placed.append((c, o))
                return SimpleNamespace(
                    orderStatus=SimpleNamespace(
                        status="Filled", avgFillPrice=1.0),
                    order=o, fills=[])
            def sleep(self, _s): return None
            def qualifyContracts(self, c):
                c.conId = 111; return [c]
            def executions(self): return []

        class _Fake(ibo.IBOrdersMixin):
            def __init__(self):
                self.ib = _FakeIB()
                self._pool = None
            def _submit_to_ib(self, fn, *a, **kw):
                kw.pop("timeout", None); kw.pop("priority", None)
                return fn(*a, **kw)
            def _build_leg_contract(self, leg):
                return SimpleNamespace(conId=222,
                                        secType="OPT",
                                        symbol=leg["symbol"][:3],
                                        tradingClass=leg["symbol"][:3])
            def get_option_price(self, sym, priority=False):
                return 0.0   # force quote failure

        client = _Fake()
        import pytest
        with patch("broker.ib_orders._compute_combo_net_limit",
                   new=_compute_that_fails):
            with pytest.raises(RuntimeError, match="combo limit"):
                client._ib_place_combo(legs, "zdn_w-SPY-1", "BUY", None)

        assert placed == [], "Must not call placeOrder when quotes fail"


class TestDebitVerticalBuy:
    """A debit call spread: long lower, short higher. Net is a debit
    (we pay). BUY limit must be POSITIVE."""

    def test_buy_debit_vertical_submits_positive_limit(self):
        legs = [
            _leg("SPY260501C00500000", "LONG",  500.0, "C"),   # pay 3.00
            _leg("SPY260501C00510000", "SHORT", 510.0, "C"),   # collect 1.00
        ]
        mids = {
            "SPY260501C00500000": 3.00,
            "SPY260501C00510000": 1.00,
        }
        limit = _run(_FakeMixin(mids), legs, action="BUY")
        # net = +1.00(SHORT) + -3.00(LONG) = -2.00. BUY limit = +2.00 + buf.
        assert limit is not None
        assert limit > 0, (
            f"debit vertical BUY must submit POSITIVE limit. Got {limit!r}"
        )
        assert abs(limit - 2.04) < 0.02
