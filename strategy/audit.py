"""Trade-level audit trail.

Every action that mutates a trade's state (open, close, roll, adopt,
reconcile-close, bracket cancel, verify close) writes a row to
``system_log`` via this helper so the Dashboard can show a full
"who did what, when, why" timeline per trade.

Key design choices
------------------
* **One helper, one schema.**  Every audit line has
  ``details = {"trade_id": int, "action": str, "actor": str, ...}``
  so the UI can filter/order reliably.
* **Thread-safe and silent on failure.**  Audit is observational; it
  must never raise into the trade flow.
* **Append-only.**  Existing rows are never updated — every transition
  is a fresh log entry with a fresh timestamp.

Actions (canonical set)
-----------------------
* ``open``              — trade placed + bracket attached
* ``close:<reason>``    — trade closed (reason: TP/SL/ROLL/MANUAL/...)
* ``roll_start``        — roll triggered, about to close old leg
* ``roll_open``         — new leg opened as part of a roll
* ``roll_abort``        — roll couldn't complete; trade stays open
* ``cancel_bracket``    — TP or SL order cancelled
* ``reconcile_close``   — reconciliation closed a DB orphan
* ``reconcile_adopt``   — reconciliation adopted an IB orphan
* ``verify_close_ok``   — post-close IB verification passed
* ``verify_close_fail`` — post-close IB verification failed → retry
"""
from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger(__name__)


def log_trade_action(
    trade_id: int | None,
    action: str,
    actor: str,
    message: str,
    *,
    level: str = "info",
    extra: dict[str, Any] | None = None,
) -> None:
    """Write an audit row. Never raises.

    Parameters
    ----------
    trade_id : int | None
        The trades.id this action is about. May be None for reconcile
        actions that create/close trades in-flight; use the resulting
        id on the next line.
    action : str
        Canonical action keyword (see module docstring).
    actor : str
        Thread/component doing the action. Usually ``scanner-TICKER``,
        ``exit_manager``, ``reconciliation``, ``entry-manager``. The
        Python thread name is appended automatically for debugging.
    message : str
        Human-readable sentence. Keep under ~200 chars; the UI shows
        it inline.
    level : str
        ``info`` / ``warn`` / ``error``. Default info.
    extra : dict | None
        Additional structured fields to merge into ``details`` (ticker,
        symbol, prices, permId, pnl_pct, etc.).
    """
    try:
        from db.writer import add_system_log
        details: dict[str, Any] = {
            "trade_id": int(trade_id) if trade_id is not None else None,
            "action": action,
            "actor": actor,
            "py_thread": threading.current_thread().name,
        }
        if extra:
            details.update(extra)
        add_system_log(actor, level, f"[AUDIT {action}] {message}", details)
    except Exception:
        # Audit is observational — never break the trade flow.
        pass
