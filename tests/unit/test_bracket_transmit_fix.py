"""Regression for the 2026-04-23 "brackets keep multiplying" bug.

``_ib_place_protection_brackets`` was setting ``tp_order.transmit =
False``. That flag is only meaningful for parent-child bracket orders
where a sibling order's transmission gates the child. In the
attach-to-existing-position case there IS no parent, so the TP stayed
stuck in IB "Transmit" status forever. Next reconcile PASS 4 saw
``tp_status=None / MISSING`` → declared the position unprotected →
called _restore_brackets_for again → another TP stuck in Transmit.
Over ~10 minutes this produced 4 TP+SL pairs per trade (8 orders).

Guarded here via a static check so nobody reintroduces the bug.
"""
from __future__ import annotations

import inspect


class TestProtectionBracketsBothTransmit:
    def test_tp_and_sl_both_transmit_true(self):
        from broker import ib_orders
        src = inspect.getsource(ib_orders._ib_place_protection_brackets_source_probe()
                                  if hasattr(ib_orders, "_ib_place_protection_brackets_source_probe")
                                  else ib_orders.IBOrdersMixin._ib_place_protection_brackets)
        # Both orders set transmit=True; the False pattern is reserved
        # for parent-child bracket flows (see broker.ib_orders.place_bracket_order).
        assert "tp_order.transmit = False" not in src, (
            "Regression guard: tp_order.transmit=False causes the TP "
            "to sit in IB 'Transmit' status forever, triggering infinite "
            "bracket re-restoration. See 2026-04-23 bracket-multiplication "
            "incident — 4 TP/SL pairs per ICT trade."
        )
        assert "tp_order.transmit = True" in src, (
            "tp_order must transmit=True so the LMT TP actually hits IB"
        )
        assert "sl_order.transmit = True" in src, (
            "sl_order must transmit=True so both siblings reach IB "
            "independently in the attach-to-existing-position case"
        )


class TestRestoreCooldown:
    """Reconciliation's bracket_restore path must not re-restore within
    a short cooldown window — gives the brackets it just placed time
    to register as live on IB before the next health check runs."""

    def test_cooldown_guard_is_wired_in_reconciliation(self):
        from strategy import reconciliation
        src = inspect.getsource(reconciliation)
        # A cooldown check against ib_brackets_checked_at must appear
        # before the call to _restore_brackets_for.
        assert "ib_brackets_checked_at" in src
        # We use < 120s but the exact value isn't load-bearing for the
        # test — what matters is that a numeric guard exists.
        assert "age_sec <" in src, (
            "Regression guard: a cooldown age check must gate "
            "_restore_brackets_for to prevent spam-restore loops."
        )
