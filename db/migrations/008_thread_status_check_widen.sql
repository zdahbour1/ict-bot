-- 008_thread_status_check_widen.sql
--
-- Widen `thread_status_status_check` to include the entry-manager
-- lifecycle statuses that trade_entry_manager.py::_update_entry_thread
-- emits: 'preflight', 'placing', 'blocked', 'failed', 'filled'.
--
-- Caught in bot.log 2026-04-22 10:58 — these writes silently warned
-- and rolled back, so the thread_status row for 'entry-manager' never
-- reflected preflight/blocked/etc states. Non-fatal, but noisy and
-- means the Threads UI was showing stale data for that row.
--
-- Migration is idempotent (DROP CONSTRAINT IF EXISTS).

ALTER TABLE thread_status DROP CONSTRAINT IF EXISTS thread_status_status_check;

ALTER TABLE thread_status
  ADD CONSTRAINT thread_status_status_check
  CHECK (status IN ('starting', 'running', 'scanning', 'idle',
                    'error', 'stopped',
                    -- entry-manager lifecycle (added 2026-04-22):
                    'preflight', 'placing', 'blocked', 'failed', 'filled'));
