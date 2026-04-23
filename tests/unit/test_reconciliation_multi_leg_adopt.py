"""Regression tests for the 2026-04-23 multi-leg adoption bug.

Incident: ``_get_db_open_con_ids`` joined trade_legs with the filter
``AND l.leg_index = 0`` — only leg 0 of each trade was tracked.
Reconciliation PASS 2 then saw legs 1-3 of a delta-neutral iron
condor as orphan IB positions and adopted each one as a fresh ICT
single-leg trade. Result: two DB rows for the same contract, two
conflicting SL brackets sitting on the same short-call, and the
"too many bracket orders" the user noticed.

Fix: the query returns the conId of EVERY open leg with
contracts_open > 0, not just leg 0.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestGetDBOpenConIds:
    def _mock_session(self, rows):
        session = MagicMock()
        session.execute.return_value.fetchall.return_value = [
            (r,) for r in rows
        ]
        return session

    def test_collects_all_legs_across_multi_leg_trade(self):
        """An iron condor adopted via insert_multi_leg_trade has 4 legs,
        4 different conIds — the function must return ALL of them."""
        from strategy import reconciliation as rc

        condor_con_ids = [861519648, 861519761, 861516966, 861516543]
        with patch.object(rc, "get_session", create=True,
                          return_value=self._mock_session(condor_con_ids)):
            # _get_db_open_con_ids imports get_session lazily — patch
            # via the module it uses.
            import db.connection as db_conn
            with patch.object(db_conn, "get_session",
                              return_value=self._mock_session(condor_con_ids)):
                result = rc._get_db_open_con_ids()
        assert result == set(condor_con_ids), (
            f"Multi-leg adoption bug: only got {result} — must return "
            f"all 4 leg conIds so reconciliation doesn't re-adopt "
            f"legs 1-3 as phantom ICT trades."
        )

    def test_sql_query_no_longer_filters_leg_index_zero(self):
        """Static guard: the regression was driven by 'AND l.leg_index = 0'
        narrowing the query. Fail loudly if anyone reintroduces it in
        the SQL (as opposed to the docstring that explains the fix)."""
        import inspect
        from strategy import reconciliation as rc
        src = inspect.getsource(rc._get_db_open_con_ids)
        # Only guard against the SQL form — the docstring may legitimately
        # reference it while explaining the history.
        assert "AND l.leg_index = 0" not in src, (
            "Regression guard: the SQL query must NOT filter on "
            "l.leg_index = 0 — that was the multi-leg adopt bug."
        )
        # Should filter on the leg-being-open criteria instead.
        assert "leg_status" in src or "contracts_open" in src, (
            "Query should filter on leg-status fields so closed legs "
            "don't register as open conIds."
        )

    def test_empty_result_when_no_open_trades(self):
        import db.connection as db_conn
        with patch.object(db_conn, "get_session",
                          return_value=self._mock_session([])):
            from strategy import reconciliation as rc
            result = rc._get_db_open_con_ids()
        assert result == set()

    def test_handles_null_con_ids(self):
        """Defensive: ``(None,)`` rows must not explode the set comp."""
        import db.connection as db_conn
        with patch.object(db_conn, "get_session",
                          return_value=self._mock_session([861519648, None,
                                                            861519761])):
            from strategy import reconciliation as rc
            result = rc._get_db_open_con_ids()
        assert result == {861519648, 861519761}
