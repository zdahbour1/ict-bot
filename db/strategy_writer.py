"""Strategy CRUD helpers for the `strategies` table (ENH-024 rollout #1).

Every operation runs in a single transaction so clone-from-source
is atomic: a partial failure never leaves a half-populated new
strategy (broken settings but copied tickers, etc.).
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text

from db.connection import get_session

log = logging.getLogger(__name__)


def get_active_strategy_id() -> Optional[int]:
    """Look up the strategy_id for the value stored in ACTIVE_STRATEGY
    (global setting). Returns None if the setting or the referenced
    strategy is missing / disabled."""
    session = get_session()
    if session is None:
        return None
    try:
        row = session.execute(text(
            "SELECT s.strategy_id "
            "FROM settings cfg "
            "JOIN strategies s ON s.name = cfg.value "
            "WHERE cfg.key = 'ACTIVE_STRATEGY' AND cfg.strategy_id IS NULL "
            "  AND s.enabled = TRUE "
            "LIMIT 1"
        )).fetchone()
        return int(row[0]) if row else None
    finally:
        session.close()


def get_default_strategy_id() -> Optional[int]:
    """Return the strategy_id of the strategy flagged is_default=TRUE,
    or None if no strategy has that flag set."""
    session = get_session()
    if session is None:
        return None
    try:
        row = session.execute(text(
            "SELECT strategy_id FROM strategies WHERE is_default = TRUE LIMIT 1"
        )).fetchone()
        return int(row[0]) if row else None
    finally:
        session.close()


def list_strategies(enabled_only: bool = False) -> list[dict]:
    """Return all strategies (or just enabled ones)."""
    session = get_session()
    if session is None:
        return []
    try:
        q = ("SELECT strategy_id, name, display_name, description, "
             "class_path, enabled, is_default "
             "FROM strategies ")
        if enabled_only:
            q += "WHERE enabled = TRUE "
        q += "ORDER BY strategy_id"
        rows = session.execute(text(q)).fetchall()
        return [
            {
                "strategy_id": int(r[0]),
                "name": r[1],
                "display_name": r[2],
                "description": r[3],
                "class_path": r[4],
                "enabled": bool(r[5]),
                "is_default": bool(r[6]),
            }
            for r in rows
        ]
    finally:
        session.close()


def create_strategy_from_source(
    new_name: str,
    display_name: str,
    class_path: str,
    *,
    source_strategy_id: int,
    description: Optional[str] = None,
) -> Optional[int]:
    """Create a new strategy by cloning settings + tickers from an
    existing one.

    Returns the new strategy_id on success, None on any error. The
    whole operation (strategies insert + settings copy + tickers copy)
    runs in one transaction — partial failures leave no artifacts.
    """
    session = get_session()
    if session is None:
        log.error("strategy_writer: DB session unavailable")
        return None
    try:
        # Verify source exists
        src = session.execute(
            text("SELECT strategy_id FROM strategies WHERE strategy_id = :sid"),
            {"sid": source_strategy_id},
        ).fetchone()
        if src is None:
            log.warning(f"strategy_writer: source strategy_id={source_strategy_id} not found")
            return None

        # Insert new strategy
        result = session.execute(
            text(
                "INSERT INTO strategies "
                "  (name, display_name, description, class_path) "
                "VALUES (:name, :display, :desc, :cp) "
                "RETURNING strategy_id"
            ),
            {
                "name": new_name,
                "display": display_name,
                "desc": description,
                "cp": class_path,
            },
        )
        new_id = int(result.scalar())

        # Clone strategy-scoped settings
        session.execute(
            text(
                "INSERT INTO settings "
                "  (category, key, value, data_type, description, is_secret, strategy_id) "
                "SELECT category, key, value, data_type, description, is_secret, :new "
                "FROM settings WHERE strategy_id = :src"
            ),
            {"new": new_id, "src": source_strategy_id},
        )

        # Clone tickers
        session.execute(
            text(
                "INSERT INTO tickers "
                "  (symbol, name, is_active, contracts, notes, strategy_id) "
                "SELECT symbol, name, is_active, contracts, notes, :new "
                "FROM tickers WHERE strategy_id = :src"
            ),
            {"new": new_id, "src": source_strategy_id},
        )

        session.commit()
        log.info(
            f"strategy_writer: created strategy '{new_name}' (id={new_id}) "
            f"cloned from id={source_strategy_id}"
        )
        return new_id
    except Exception as e:
        session.rollback()
        log.error(f"strategy_writer: clone failed — {e}", exc_info=True)
        return None
    finally:
        session.close()


def set_active_strategy(name: str) -> bool:
    """Update the global ACTIVE_STRATEGY setting. Does NOT restart the
    bot — that's a UI/sidecar concern. Returns True if the strategy
    exists + is enabled, False otherwise.

    Uses explicit SELECT → UPDATE/INSERT rather than ON CONFLICT because
    Postgres treats NULL as distinct in unique constraints, and the
    global ACTIVE_STRATEGY row has strategy_id IS NULL.
    """
    session = get_session()
    if session is None:
        return False
    try:
        row = session.execute(
            text("SELECT 1 FROM strategies WHERE name = :n AND enabled = TRUE"),
            {"n": name},
        ).fetchone()
        if row is None:
            log.warning(f"strategy_writer: set_active refused — '{name}' not enabled")
            return False

        existing = session.execute(
            text(
                "SELECT id FROM settings "
                "WHERE key = 'ACTIVE_STRATEGY' AND strategy_id IS NULL "
                "LIMIT 1"
            )
        ).fetchone()

        if existing is not None:
            session.execute(
                text(
                    "UPDATE settings SET value = :v, updated_at = NOW() "
                    "WHERE id = :id"
                ),
                {"v": name, "id": int(existing[0])},
            )
        else:
            session.execute(
                text(
                    "INSERT INTO settings "
                    "  (category, key, value, data_type, description, is_secret, strategy_id) "
                    "VALUES ('strategy', 'ACTIVE_STRATEGY', :v, 'string', "
                    "        'Which strategy the bot runs. Change requires bot restart.', "
                    "        FALSE, NULL)"
                ),
                {"v": name},
            )
        session.commit()
        return True
    except Exception as e:
        session.rollback()
        log.error(f"strategy_writer: set_active failed — {e}", exc_info=True)
        return False
    finally:
        session.close()
