"""Settings API — CRUD for the settings table.

Multi-strategy v2 (Phase 3): settings are now scoped by `strategy_id`.
  - NULL strategy_id  = global / inherited default
  - non-NULL          = per-strategy override

Reads with ?strategy_id=N return (strategy-scoped ∪ globals) — the
strategy-scoped row wins where a key exists at both scopes (the UI
layers the display accordingly, marking NULL-scoped rows as
"inherited"). Writes are scoped via the optional `strategy_id` body
field; editing an inherited setting creates a new scoped row rather
than overwriting the global.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import or_
from db.connection import get_session
from db.models import Setting

router = APIRouter(tags=["settings"])


class SettingCreate(BaseModel):
    category: str
    key: str
    value: str
    data_type: str = "string"
    description: Optional[str] = None
    is_secret: bool = False
    strategy_id: Optional[int] = None


class SettingUpdate(BaseModel):
    value: Optional[str] = None
    description: Optional[str] = None
    is_secret: Optional[bool] = None
    # When set, PUT by key will upsert into the (key, strategy_id) scope
    # rather than mutating whichever row `key` happens to match first.
    strategy_id: Optional[int] = None


def _setting_to_dict(s: Setting, mask_secrets: bool = True) -> dict:
    value = s.value
    if mask_secrets and s.is_secret and value:
        value = "********"
    return {
        "id": s.id, "category": s.category, "key": s.key,
        "value": value, "data_type": s.data_type,
        "description": s.description, "is_secret": s.is_secret,
        "strategy_id": s.strategy_id,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


@router.get("/settings")
def list_settings(
    category: Optional[str] = None,
    reveal_secrets: bool = False,
    strategy_id: Optional[int] = None,
):
    """List settings.

    - No `strategy_id`: returns globals only (strategy_id IS NULL).
    - With `strategy_id`: returns globals ∪ rows scoped to that strategy.
      The caller (UI) decides which wins per-key when both are present.
    """
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        q = session.query(Setting)
        if strategy_id is None:
            q = q.filter(Setting.strategy_id.is_(None))
        else:
            q = q.filter(or_(
                Setting.strategy_id == strategy_id,
                Setting.strategy_id.is_(None),
            ))
        if category:
            q = q.filter(Setting.category == category)
        settings = q.order_by(Setting.category, Setting.key).all()

        # Group by category. When a key exists at both scopes, the
        # strategy-scoped row shadows the global one, and the global is
        # dropped from the payload (the UI shows the override; the user
        # can "Reset to global" which deletes the scoped row).
        grouped: dict = {}
        if strategy_id is not None:
            scoped_keys = {s.key for s in settings if s.strategy_id == strategy_id}
            settings = [s for s in settings
                        if not (s.strategy_id is None and s.key in scoped_keys)]

        for s in settings:
            cat = s.category
            if cat not in grouped:
                grouped[cat] = []
            grouped[cat].append(_setting_to_dict(s, mask_secrets=not reveal_secrets))

        return {"settings": grouped, "total": len(settings)}
    finally:
        session.close()


@router.get("/settings/{key}")
def get_setting(key: str, reveal: bool = False, strategy_id: Optional[int] = None):
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        q = session.query(Setting).filter(Setting.key == key)
        if strategy_id is None:
            q = q.filter(Setting.strategy_id.is_(None))
        else:
            # Prefer scoped row, fall back to global
            scoped = q.filter(Setting.strategy_id == strategy_id).first()
            if scoped:
                return _setting_to_dict(scoped, mask_secrets=not reveal)
            q = session.query(Setting).filter(
                Setting.key == key, Setting.strategy_id.is_(None))
        setting = q.first()
        if not setting:
            raise HTTPException(404, f"Setting '{key}' not found")
        return _setting_to_dict(setting, mask_secrets=not reveal)
    finally:
        session.close()


@router.put("/settings/{key}")
def update_setting(key: str, req: SettingUpdate):
    """Update a setting by (key, strategy_id).

    If `strategy_id` is provided and no row exists at that scope, a new
    scoped row is created by cloning the global row's category/data_type/
    is_secret. This is how "editing an inherited setting" creates an
    override without touching the global.
    """
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        # Exact-scope lookup
        q = session.query(Setting).filter(Setting.key == key)
        if req.strategy_id is None:
            q = q.filter(Setting.strategy_id.is_(None))
        else:
            q = q.filter(Setting.strategy_id == req.strategy_id)
        setting = q.first()

        if not setting:
            if req.strategy_id is None:
                raise HTTPException(404, f"Setting '{key}' not found")
            # No scoped row yet — clone from the global if present and
            # apply the override.
            global_row = session.query(Setting).filter(
                Setting.key == key, Setting.strategy_id.is_(None)
            ).first()
            if not global_row:
                raise HTTPException(
                    404,
                    f"Setting '{key}' not found at global or strategy scope; "
                    "POST /api/settings to create it first."
                )
            setting = Setting(
                category=global_row.category,
                key=key,
                value=req.value if req.value is not None else global_row.value,
                data_type=global_row.data_type,
                description=(req.description if req.description is not None
                             else global_row.description),
                is_secret=(req.is_secret if req.is_secret is not None
                           else global_row.is_secret),
                strategy_id=req.strategy_id,
            )
            session.add(setting)
            session.commit()
            return _setting_to_dict(setting, mask_secrets=False)

        if req.value is not None:
            setting.value = req.value
        if req.description is not None:
            setting.description = req.description
        if req.is_secret is not None:
            setting.is_secret = req.is_secret
        session.commit()
        return _setting_to_dict(setting, mask_secrets=False)
    finally:
        session.close()


@router.post("/settings", status_code=201)
def create_setting(req: SettingCreate):
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        q = session.query(Setting).filter(Setting.key == req.key)
        if req.strategy_id is None:
            q = q.filter(Setting.strategy_id.is_(None))
        else:
            q = q.filter(Setting.strategy_id == req.strategy_id)
        existing = q.first()
        if existing:
            scope = "global" if req.strategy_id is None else f"strategy {req.strategy_id}"
            raise HTTPException(409, f"Setting '{req.key}' already exists at {scope} scope")
        setting = Setting(
            category=req.category, key=req.key, value=req.value,
            data_type=req.data_type, description=req.description,
            is_secret=req.is_secret, strategy_id=req.strategy_id,
        )
        session.add(setting)
        session.commit()
        return _setting_to_dict(setting, mask_secrets=False)
    finally:
        session.close()


@router.delete("/settings/{key}")
def delete_setting(key: str, strategy_id: Optional[int] = None):
    """Delete a setting at a specific scope.

    - No strategy_id → deletes the global row (rare; usually the user
      wants the scoped variant).
    - With strategy_id → deletes just the override, causing the global
      to take effect again (the "Reset to global" UI action).
    """
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        q = session.query(Setting).filter(Setting.key == key)
        if strategy_id is None:
            q = q.filter(Setting.strategy_id.is_(None))
        else:
            q = q.filter(Setting.strategy_id == strategy_id)
        setting = q.first()
        if not setting:
            raise HTTPException(404, f"Setting '{key}' not found at requested scope")
        session.delete(setting)
        session.commit()
        return {"status": "deleted", "key": key, "strategy_id": strategy_id}
    finally:
        session.close()
