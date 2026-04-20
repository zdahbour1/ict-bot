"""Strategies API — list + activate + toggle enabled.

Powers the Strategies tab in the dashboard. Live strategy activation
requires a bot restart to take effect — the setting is a boot-time
decision per active_strategy_design.md.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Body

from db.connection import get_session
from db.models import Strategy
from sqlalchemy import text

router = APIRouter(tags=["strategies"])


def _strategy_row_to_dict(s: Strategy, *, active_name: str | None = None) -> dict:
    return {
        "strategy_id": s.strategy_id,
        "name": s.name,
        "display_name": s.display_name,
        "description": s.description,
        "class_path": s.class_path,
        "enabled": s.enabled,
        "is_default": s.is_default,
        "is_active": (active_name is not None and s.name == active_name),
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


@router.get("/strategies")
def list_all_strategies():
    """All strategies with `is_active` flag indicating which is the
    current ACTIVE_STRATEGY."""
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        rows = session.query(Strategy).order_by(Strategy.strategy_id).all()
        # Look up active name from the global setting
        active = session.execute(text(
            "SELECT value FROM settings "
            "WHERE key = 'ACTIVE_STRATEGY' AND strategy_id IS NULL"
        )).scalar()
        return {
            "strategies": [_strategy_row_to_dict(s, active_name=active) for s in rows],
            "active": active,
            "total": len(rows),
        }
    finally:
        session.close()


@router.get("/strategies/active")
def get_active_strategy():
    """Returns the currently active strategy's details (or 404)."""
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        row = session.execute(text(
            "SELECT s.strategy_id, s.name, s.display_name, s.description, "
            "       s.class_path, s.enabled, s.is_default "
            "FROM settings cfg "
            "JOIN strategies s ON s.name = cfg.value "
            "WHERE cfg.key = 'ACTIVE_STRATEGY' AND cfg.strategy_id IS NULL"
        )).fetchone()
        if row is None:
            raise HTTPException(404, "ACTIVE_STRATEGY not set or points at missing row")
        return {
            "strategy_id": row[0], "name": row[1], "display_name": row[2],
            "description": row[3], "class_path": row[4],
            "enabled": row[5], "is_default": row[6],
            "is_active": True,
        }
    finally:
        session.close()


@router.post("/strategies/{strategy_id}/activate")
def activate_strategy(strategy_id: int):
    """Make this strategy the ACTIVE_STRATEGY.

    The change takes effect on the next bot start. Backtests pick it up
    immediately via the dropdown default. Refuses to activate a disabled
    strategy.
    """
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        row = session.execute(text(
            "SELECT name, enabled FROM strategies WHERE strategy_id = :sid"
        ), {"sid": strategy_id}).fetchone()
        if row is None:
            raise HTTPException(404, f"strategy_id {strategy_id} not found")
        name, enabled = row[0], row[1]
        if not enabled:
            raise HTTPException(
                400,
                f"Cannot activate '{name}' — the strategy is disabled. "
                f"Enable it first via POST /api/strategies/{strategy_id}/enable"
            )

        # Use the strategy_writer helper which handles the NULL-strategy_id
        # uniqueness quirk correctly.
        from db.strategy_writer import set_active_strategy as _set_active
        ok = _set_active(name)
        if not ok:
            raise HTTPException(500, "Failed to update ACTIVE_STRATEGY")

        return {
            "activated": name,
            "strategy_id": strategy_id,
            "message": (
                f"ACTIVE_STRATEGY set to '{name}'. "
                "Backtests pick this up immediately. Live bot requires a "
                "restart to use the new strategy."
            ),
        }
    finally:
        session.close()


@router.post("/strategies/{strategy_id}/enable")
def enable_strategy(strategy_id: int):
    """Flip the `enabled` flag to TRUE. Required before a strategy can
    be activated or used in backtests."""
    return _toggle_enabled(strategy_id, True)


@router.post("/strategies/{strategy_id}/disable")
def disable_strategy(strategy_id: int):
    """Flip `enabled` to FALSE. Refuses to disable the currently
    active strategy (would leave the bot with nothing to run)."""
    return _toggle_enabled(strategy_id, False)


def _toggle_enabled(strategy_id: int, new_value: bool) -> dict:
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        row = session.execute(text(
            "SELECT name, enabled FROM strategies WHERE strategy_id = :sid"
        ), {"sid": strategy_id}).fetchone()
        if row is None:
            raise HTTPException(404, f"strategy_id {strategy_id} not found")
        name = row[0]

        if not new_value:
            # Refuse to disable the active strategy
            active = session.execute(text(
                "SELECT value FROM settings "
                "WHERE key = 'ACTIVE_STRATEGY' AND strategy_id IS NULL"
            )).scalar()
            if active == name:
                raise HTTPException(
                    400,
                    f"Cannot disable '{name}' — it is the active strategy. "
                    f"Activate another strategy first."
                )

        session.execute(text(
            "UPDATE strategies SET enabled = :val, updated_at = NOW() "
            "WHERE strategy_id = :sid"
        ), {"val": new_value, "sid": strategy_id})
        session.commit()

        return {
            "strategy_id": strategy_id,
            "name": name,
            "enabled": new_value,
        }
    finally:
        session.close()
