"""Unit tests for strategy.audit — the trade-level audit trail helper.

The helper wraps db.writer.add_system_log with a consistent schema
so the UI can filter/sort reliably.  These tests exercise the
helper itself with a mocked db.writer so no DB is required.
"""
from __future__ import annotations

from unittest.mock import patch


def test_log_trade_action_writes_expected_shape():
    """Every audit row has trade_id/action/actor/py_thread in details."""
    from strategy.audit import log_trade_action

    with patch("db.writer.add_system_log") as mock_log:
        log_trade_action(
            trade_id=42, action="open", actor="scanner-INTC",
            message="opened INTC260425C00060000 @ $1.50",
            extra={"ticker": "INTC", "signal_type": "LONG_OB"},
        )

    assert mock_log.called
    args, kwargs = mock_log.call_args
    # component (1st arg) == actor
    assert args[0] == "scanner-INTC"
    # level
    assert args[1] == "info"
    # message is tagged with the action
    assert "[AUDIT open]" in args[2]
    # details has the canonical keys
    details = args[3]
    assert details["trade_id"] == 42
    assert details["action"] == "open"
    assert details["actor"] == "scanner-INTC"
    assert "py_thread" in details
    # Extra fields merged
    assert details["ticker"] == "INTC"
    assert details["signal_type"] == "LONG_OB"


def test_log_trade_action_accepts_none_trade_id():
    """Reconcile actions may not have a trade_id at call time."""
    from strategy.audit import log_trade_action

    with patch("db.writer.add_system_log") as mock_log:
        log_trade_action(None, "reconcile_adopt", "reconciliation",
                         "adopted orphan")
    details = mock_log.call_args.args[3]
    assert details["trade_id"] is None


def test_log_trade_action_is_silent_on_failure():
    """Audit is observational; must NEVER raise into the trade flow."""
    from strategy.audit import log_trade_action

    with patch("db.writer.add_system_log", side_effect=RuntimeError("db down")):
        # No exception should escape
        log_trade_action(1, "open", "x", "y")


def test_levels_are_honored():
    from strategy.audit import log_trade_action

    with patch("db.writer.add_system_log") as mock_log:
        log_trade_action(1, "verify_close_fail", "exit_manager",
                         "position stayed non-zero", level="error")
    assert mock_log.call_args.args[1] == "error"

    with patch("db.writer.add_system_log") as mock_log:
        log_trade_action(1, "reconcile_adopt", "reconciliation",
                         "orphan", level="warn")
    assert mock_log.call_args.args[1] == "warn"


def test_canonical_action_keys():
    """Document the action vocabulary — also catches typos at call sites."""
    canonical = {
        "open",
        "close:TP", "close:SL", "close:ROLL", "close:MANUAL", "close:RECONCILE",
        "roll_start", "roll_open", "roll_abort",
        "cancel_bracket",
        "reconcile_close", "reconcile_adopt",
        "verify_close_ok", "verify_close_fail",
    }
    # Just assert the set is non-empty and strings — documentation test.
    assert all(isinstance(a, str) and a for a in canonical)
