"""
DB locking race tests — hammers the SELECT FOR UPDATE NOWAIT path
that gates all trade closes (ARCH-002 + ARCH-005).

These are the race conditions that caused the phantom-trade /
double-close bugs earlier this project. The invariant being tested:

    If two threads both try to close the same trade, EXACTLY ONE
    must acquire the lock. The other must get (None, None) from
    lock_trade_for_close() and gracefully skip.

Runs against a real Postgres. Set:
    DATABASE_URL=postgresql://ict_bot:ict_bot_dev@localhost:5432/ict_bot

Then run just this suite:
    PYTEST_DB_REPORT=1 pytest tests/integration/ -m integration

Or via the Tests tab → "Integration" in the dashboard.
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]


def _db_available() -> bool:
    """Skip cleanly if no Postgres is reachable."""
    try:
        from db.connection import db_available
        return db_available()
    except Exception:
        return False


@pytest.fixture(scope="module")
def db_guard():
    if not _db_available():
        pytest.skip("Postgres not reachable — skipping DB race tests")


@pytest.fixture
def open_trade(db_guard):
    """Insert a fresh open trade and yield its id. Cleans up after."""
    from db.connection import get_session
    from sqlalchemy import text

    session = get_session()
    result = session.execute(
        text("""
            INSERT INTO trades
              (account, ticker, symbol, direction, contracts_entered,
               contracts_open, entry_price, profit_target, stop_loss_level,
               entry_time, status)
            VALUES
              ('DU000000-TEST', 'TEST', 'TEST260415C00100000', 'LONG',
               2, 2, 2.00, 4.00, 0.80, NOW(), 'open')
            RETURNING id
        """)
    ).fetchone()
    session.commit()
    trade_id = int(result[0])
    session.close()

    yield trade_id

    # Cleanup — mark closed so no other test stumbles on it, then delete
    session = get_session()
    session.execute(text("DELETE FROM trade_closes WHERE trade_id = :id"), {"id": trade_id})
    session.execute(text("DELETE FROM trades WHERE id = :id"), {"id": trade_id})
    session.commit()
    session.close()


class TestLockForCloseRace:
    """Core invariant: exactly one winner per concurrent close attempt."""

    def test_exactly_one_thread_wins(self, open_trade):
        """16 threads race to lock the same trade. Exactly one should win."""
        from db.writer import lock_trade_for_close, release_trade_lock

        trade_id = open_trade
        winners: list[int] = []
        losers: list[int] = []
        lock = threading.Lock()
        start_barrier = threading.Barrier(16)

        def worker(n: int):
            start_barrier.wait()  # everyone starts at once
            session, data = lock_trade_for_close(trade_id)
            try:
                if session is not None and data is not None:
                    with lock:
                        winners.append(n)
                    # Hold the lock briefly to force losers to NOWAIT-fail
                    time.sleep(0.2)
                else:
                    with lock:
                        losers.append(n)
            finally:
                if session is not None:
                    release_trade_lock(session)

        with ThreadPoolExecutor(max_workers=16) as ex:
            list(as_completed([ex.submit(worker, i) for i in range(16)]))

        assert len(winners) == 1, (
            f"expected exactly 1 lock winner; got {len(winners)} "
            f"(winners={winners}, losers={losers})"
        )
        assert len(losers) == 15, (
            f"expected 15 NOWAIT-skipped losers; got {len(losers)}"
        )

    def test_closed_trade_cannot_be_relocked(self, open_trade):
        """Once a trade transitions to closed, every lock attempt fails."""
        from db.writer import lock_trade_for_close, finalize_close

        trade_id = open_trade
        session, data = lock_trade_for_close(trade_id)
        assert session is not None

        # Close it
        ok = finalize_close(session, trade_id, exit_price=2.45,
                            result="WIN", reason="TP")
        assert ok

        # Now try to re-lock from 8 threads — all must fail
        losers: list[int] = []
        lock = threading.Lock()

        def worker(n: int):
            s, d = lock_trade_for_close(trade_id)
            if s is None and d is None:
                with lock:
                    losers.append(n)

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(as_completed([ex.submit(worker, i) for i in range(8)]))

        assert len(losers) == 8, f"closed trade re-locked by {8 - len(losers)} threads!"


class TestAddTradeIdempotence:
    """ARCH-006: only one row per (ticker, conId, open)."""

    def test_concurrent_inserts_allow_all(self, db_guard):
        """insert_trade doesn't dedup on its own — the caller (add_trade
        in the scanner) does the one-open-per-ticker check. This test
        confirms that insert_trade is at least thread-safe: 8 concurrent
        inserts produce exactly 8 rows, no silent drops.

        Branch 3's multi-strategy refactor will tighten this to one
        open per (ticker, strategy, conId) — and we'll add that test here.
        """
        from db.writer import insert_trade
        from db.connection import get_session
        from sqlalchemy import text

        base_trade = {
            "ticker": "TESTX",
            "symbol": "TESTX260415C00100000",
            "direction": "LONG",
            "contracts": 2,
            "entry_price": 1.23,
            "profit_target": 2.46,
            "stop_loss": 0.49,
            "entry_time": datetime.now(timezone.utc),
            "ib_con_id": None,   # set unique below
        }

        ids: list[int] = []
        lock = threading.Lock()

        def worker(n: int):
            t = dict(base_trade)
            t["ib_con_id"] = 900_000 + n
            trade_id = insert_trade(t, account="DU000000-TEST")
            if trade_id is not None:
                with lock:
                    ids.append(trade_id)

        try:
            with ThreadPoolExecutor(max_workers=8) as ex:
                list(as_completed([ex.submit(worker, i) for i in range(8)]))

            assert len(ids) == 8, f"lost inserts: got {len(ids)}/8"
            assert len(set(ids)) == 8, f"duplicate IDs returned: {ids}"
        finally:
            # Cleanup
            session = get_session()
            session.execute(text("DELETE FROM trades WHERE ticker = 'TESTX'"))
            session.commit()
            session.close()
