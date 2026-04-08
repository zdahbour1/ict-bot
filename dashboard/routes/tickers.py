"""Tickers API — CRUD for the tickers table."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from db.connection import get_session
from db.models import Ticker

router = APIRouter(tags=["tickers"])


class TickerCreate(BaseModel):
    symbol: str
    name: Optional[str] = None
    contracts: int = 2
    notes: Optional[str] = None


class TickerUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None
    contracts: Optional[int] = None
    notes: Optional[str] = None


def _ticker_to_dict(t: Ticker) -> dict:
    return {
        "id": t.id, "symbol": t.symbol, "name": t.name,
        "is_active": t.is_active, "contracts": t.contracts, "notes": t.notes,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


@router.get("/tickers")
def list_tickers():
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        tickers = session.query(Ticker).order_by(Ticker.id).all()
        result = [_ticker_to_dict(t) for t in tickers]
        session.close()
        return {"tickers": result, "total": len(result),
                "active": sum(1 for t in result if t["is_active"])}
    finally:
        session.close()


@router.get("/tickers/{ticker_id}")
def get_ticker(ticker_id: int):
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        ticker = session.query(Ticker).filter(Ticker.id == ticker_id).first()
        if not ticker:
            raise HTTPException(404, "Ticker not found")
        result = _ticker_to_dict(ticker)
        session.close()
        return result
    finally:
        session.close()


@router.post("/tickers", status_code=201)
def create_ticker(req: TickerCreate):
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        existing = session.query(Ticker).filter(Ticker.symbol == req.symbol.upper()).first()
        if existing:
            raise HTTPException(409, f"Ticker {req.symbol.upper()} already exists")
        ticker = Ticker(
            symbol=req.symbol.upper(),
            name=req.name,
            contracts=req.contracts,
            notes=req.notes,
        )
        session.add(ticker)
        session.commit()
        result = _ticker_to_dict(ticker)
        session.close()
        return result
    finally:
        session.close()


@router.put("/tickers/{ticker_id}")
def update_ticker(ticker_id: int, req: TickerUpdate):
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        ticker = session.query(Ticker).filter(Ticker.id == ticker_id).first()
        if not ticker:
            raise HTTPException(404, "Ticker not found")
        if req.name is not None:
            ticker.name = req.name
        if req.is_active is not None:
            ticker.is_active = req.is_active
        if req.contracts is not None:
            ticker.contracts = req.contracts
        if req.notes is not None:
            ticker.notes = req.notes
        session.commit()
        result = _ticker_to_dict(ticker)
        session.close()
        return result
    finally:
        session.close()


@router.delete("/tickers/{ticker_id}")
def delete_ticker(ticker_id: int):
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        ticker = session.query(Ticker).filter(Ticker.id == ticker_id).first()
        if not ticker:
            raise HTTPException(404, "Ticker not found")
        session.delete(ticker)
        session.commit()
        session.close()
        return {"status": "deleted", "symbol": ticker.symbol}
    finally:
        session.close()
