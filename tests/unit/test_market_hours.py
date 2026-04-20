"""Unit tests for strategy.market_hours.

The clock + its gates are pure functions of (now, settings). Tests
exercise every boundary condition so the behavior on a real market
day is predictable.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytz
import pytest

PT = pytz.timezone("America/Los_Angeles")


def _clock_at(hour: int, minute: int):
    """Build a MarketClock at the given PT time, with default settings."""
    from strategy.market_hours import get_market_clock
    now = PT.localize(datetime(2026, 4, 21, hour, minute))  # arbitrary weekday
    with patch("strategy.market_hours._load_int_setting") as mock:
        # Settings → defaults (hard cutoff 13:00, lead 5, start 06:30)
        def _lookup(key, default):
            return default
        mock.side_effect = _lookup
        return get_market_clock(now=now)


# ── Hard cutoff ──────────────────────────────────────────────────

class TestIsPastClose:
    def test_before_close_is_false(self):
        c = _clock_at(12, 59)
        assert c.is_past_close() is False

    def test_at_cutoff_is_true(self):
        """Exactly at 13:00:00 → past close. The market is in the
        matching-orders-only final-print state; our bot should stop."""
        c = _clock_at(13, 0)
        assert c.is_past_close() is True

    def test_after_close_is_true(self):
        c = _clock_at(13, 30)
        assert c.is_past_close() is True

    def test_early_morning_is_false(self):
        c = _clock_at(5, 0)
        assert c.is_past_close() is False


# ── EOD sweep window ─────────────────────────────────────────────

class TestEodSweepWindow:
    def test_before_sweep_start(self):
        c = _clock_at(12, 54)
        assert c.in_eod_sweep_window() is False

    def test_at_sweep_start(self):
        c = _clock_at(12, 55)
        assert c.in_eod_sweep_window() is True

    def test_in_sweep_window(self):
        c = _clock_at(12, 58)
        assert c.in_eod_sweep_window() is True

    def test_at_hard_cutoff_exits_window(self):
        """13:00 is past_close, NOT in sweep. Sweep window is
        half-open [12:55, 13:00)."""
        c = _clock_at(13, 0)
        assert c.in_eod_sweep_window() is False

    def test_after_close(self):
        c = _clock_at(13, 30)
        assert c.in_eod_sweep_window() is False


# ── Entry gates ──────────────────────────────────────────────────

class TestEntriesAllowed:
    def test_pre_market(self):
        c = _clock_at(5, 0)
        assert c.entries_allowed() is False

    def test_at_start_window(self):
        c = _clock_at(6, 30)
        assert c.entries_allowed() is True

    def test_mid_day(self):
        c = _clock_at(10, 0)
        assert c.entries_allowed() is True

    def test_just_before_eod_sweep(self):
        """12:54 — entries still allowed (sweep starts at 12:55)."""
        c = _clock_at(12, 54)
        assert c.entries_allowed() is True

    def test_in_eod_sweep_blocks_entries(self):
        """12:55 onwards — NO new entries. This is the bug fix for
        the 2026-04-20 afternoon cascade."""
        c = _clock_at(12, 55)
        assert c.entries_allowed() is False

    def test_past_close_blocks_entries(self):
        c = _clock_at(14, 0)
        assert c.entries_allowed() is False


# ── Minutes helpers ──────────────────────────────────────────────

class TestMinutesUntil:
    def test_minutes_until_sweep(self):
        c = _clock_at(12, 50)
        assert c.minutes_until_eod_sweep() == pytest.approx(5, abs=0.01)

    def test_minutes_until_close(self):
        c = _clock_at(12, 30)
        assert c.minutes_until_close() == pytest.approx(30, abs=0.01)

    def test_both_clamp_to_zero_after_close(self):
        c = _clock_at(14, 0)
        assert c.minutes_until_close() == 0.0
        assert c.minutes_until_eod_sweep() == 0.0


# ── TradeEntryManager.can_enter integration ──────────────────────

class TestCanEnterGatesOnClock:
    """The entry gate calls market_hours.get_market_clock() and refuses
    entries outside the window. Regression for the 'trade placed at
    12:58 then immediately torn down by EOD sweep' pattern."""

    def test_can_enter_blocked_at_eod(self):
        """At 12:57 (inside EOD sweep window), can_enter must return
        False with a clear reason mentioning EOD."""
        from strategy.market_hours import get_market_clock
        from unittest.mock import MagicMock
        # Build a minimal TradeEntryManager-shaped object; reach into
        # the can_enter method directly since the class imports heavily.
        from strategy.trade_entry_manager import TradeEntryManager

        mgr = TradeEntryManager.__new__(TradeEntryManager)
        mgr.ticker = "TEST"
        mgr.exit_manager = MagicMock()
        mgr.exit_manager.open_trades = []
        mgr._entry_pending = False
        mgr._trades_today = 0
        mgr._last_exit_time = None

        now = PT.localize(datetime(2026, 4, 21, 12, 57))
        with patch("strategy.market_hours._load_int_setting", side_effect=lambda k, d: d), \
             patch("strategy.market_hours.datetime") as mock_dt:
            # get_market_clock uses datetime.now(PT); intercept that
            mock_dt.now.return_value = now
            mock_dt.combine = datetime.combine
            allowed, reason = mgr.can_enter()

        assert allowed is False
        assert "EOD" in reason or "sweep" in reason.lower()
