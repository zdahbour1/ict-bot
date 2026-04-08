"""Settings API — CRUD for the settings table."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
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


class SettingUpdate(BaseModel):
    value: Optional[str] = None
    description: Optional[str] = None
    is_secret: Optional[bool] = None


def _setting_to_dict(s: Setting, mask_secrets: bool = True) -> dict:
    value = s.value
    if mask_secrets and s.is_secret and value:
        value = "********"
    return {
        "id": s.id, "category": s.category, "key": s.key,
        "value": value, "data_type": s.data_type,
        "description": s.description, "is_secret": s.is_secret,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


@router.get("/settings")
def list_settings(category: Optional[str] = None, reveal_secrets: bool = False):
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        q = session.query(Setting)
        if category:
            q = q.filter(Setting.category == category)
        settings = q.order_by(Setting.category, Setting.key).all()

        # Group by category
        grouped = {}
        for s in settings:
            cat = s.category
            if cat not in grouped:
                grouped[cat] = []
            grouped[cat].append(_setting_to_dict(s, mask_secrets=not reveal_secrets))

        session.close()
        return {"settings": grouped, "total": len(settings)}
    finally:
        session.close()


@router.get("/settings/{key}")
def get_setting(key: str, reveal: bool = False):
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        setting = session.query(Setting).filter(Setting.key == key).first()
        if not setting:
            raise HTTPException(404, f"Setting '{key}' not found")
        result = _setting_to_dict(setting, mask_secrets=not reveal)
        session.close()
        return result
    finally:
        session.close()


@router.put("/settings/{key}")
def update_setting(key: str, req: SettingUpdate):
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        setting = session.query(Setting).filter(Setting.key == key).first()
        if not setting:
            raise HTTPException(404, f"Setting '{key}' not found")
        if req.value is not None:
            setting.value = req.value
        if req.description is not None:
            setting.description = req.description
        if req.is_secret is not None:
            setting.is_secret = req.is_secret
        session.commit()
        result = _setting_to_dict(setting, mask_secrets=False)
        session.close()
        return result
    finally:
        session.close()


@router.post("/settings", status_code=201)
def create_setting(req: SettingCreate):
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        existing = session.query(Setting).filter(Setting.key == req.key).first()
        if existing:
            raise HTTPException(409, f"Setting '{req.key}' already exists")
        setting = Setting(
            category=req.category, key=req.key, value=req.value,
            data_type=req.data_type, description=req.description,
            is_secret=req.is_secret,
        )
        session.add(setting)
        session.commit()
        result = _setting_to_dict(setting, mask_secrets=False)
        session.close()
        return result
    finally:
        session.close()


@router.delete("/settings/{key}")
def delete_setting(key: str):
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        setting = session.query(Setting).filter(Setting.key == key).first()
        if not setting:
            raise HTTPException(404, f"Setting '{key}' not found")
        session.delete(setting)
        session.commit()
        session.close()
        return {"status": "deleted", "key": key}
    finally:
        session.close()
