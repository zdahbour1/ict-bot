"""Regression: finalize_close must close EVERY leg of a multi-leg
trade, not just leg 0.

Before 2026-04-24 afternoon, finalize_close only ran
  UPDATE trade_legs SET ... WHERE trade_id=:id AND leg_index=0
so iron-condor trades that transitioned to status='closed' kept
legs 1-3 with contracts_open=1 and leg_status='open' forever.
Dashboards showed "ghost open" rows on closed trades, and any
analytics summing `contracts_open` across legs doubled/tripled
the true open position.

Fix verified here by inspecting the SQL issued by finalize_close.
"""
from __future__ import annotations

import inspect


class TestFinalizeCloseClosesAllLegs:
    def test_finalize_close_emits_update_for_legs_beyond_zero(self):
        """Static guard: the second UPDATE targeting leg_index > 0
        must be present. Without it, multi-leg trades regress."""
        from db import writer
        src = inspect.getsource(writer.finalize_close)
        # First UPDATE handles leg 0.
        assert "leg_index=0" in src
        # Second UPDATE handles every other open leg on the trade.
        assert "leg_index > 0" in src, (
            "finalize_close must also close legs beyond leg 0 for "
            "multi-leg trades (iron condor, spread, hedged)."
        )
        assert "contracts_open = 0" in src
        assert "leg_status = 'closed'" in src

    def test_sql_scopes_to_still_open_legs(self):
        """The fan-out UPDATE must filter leg_status='open' so we
        don't stomp on legs that legitimately closed earlier (partial
        close of individual legs should stay closed)."""
        from db import writer
        src = inspect.getsource(writer.finalize_close)
        # The WHERE clause on the second UPDATE must include
        # leg_status = 'open' to avoid resurrecting closed legs.
        assert "leg_status = 'open'" in src
