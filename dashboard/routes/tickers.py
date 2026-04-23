"""Tickers API — CRUD for the tickers table.

Multi-strategy v2 (Phase 3 extension): tickers are scoped by
``strategy_id`` (NOT NULL FK). Reads filter by ``strategy_id`` query
param; writes require ``strategy_id`` in the body. Symbol uniqueness is
enforced per-(symbol, strategy_id) so e.g. SPY can exist under both ICT
and ORB independently.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from db.connection import get_session
from db.models import Ticker

router = APIRouter(tags=["tickers"])


class TickerCreate(BaseModel):
    symbol: str
    strategy_id: int
    sec_type: str = "OPT"
    name: Optional[str] = None
    contracts: int = 2
    notes: Optional[str] = None


class TickerUpdate(BaseModel):
    # strategy_id is intentionally NOT editable — to move a ticker
    # between strategies, delete and re-add.
    name: Optional[str] = None
    is_active: Optional[bool] = None
    contracts: Optional[int] = None
    notes: Optional[str] = None


def _ticker_to_dict(t: Ticker) -> dict:
    return {
        "id": t.id, "symbol": t.symbol, "name": t.name,
        "is_active": t.is_active, "contracts": t.contracts, "notes": t.notes,
        "strategy_id": t.strategy_id,
        "sec_type": getattr(t, "sec_type", "OPT"),
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


@router.get("/strategies/{strategy_id}/supported-ticker-types")
def list_supported_ticker_types(strategy_id: int):
    """Return the list of instrument types this strategy can trade,
    pulled from strategy_supported_ticker_types. The Tickers tab uses
    this to populate the sec_type dropdown when adding a ticker."""
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        from sqlalchemy import text
        rows = session.execute(text(
            "SELECT sec_type, notes FROM strategy_supported_ticker_types "
            "WHERE strategy_id = :sid ORDER BY sec_type"
        ), {"sid": strategy_id}).fetchall()
        session.close()
        return {
            "strategy_id": strategy_id,
            "sec_types": [{"sec_type": r[0], "notes": r[1]} for r in rows],
        }
    finally:
        session.close()


def _validate_ticker_type_for_strategy(session, strategy_id: int, sec_type: str):
    """Raise HTTPException(400) if the strategy doesn't support this sec_type.
    Pure DB lookup — no ORM coupling, called from create_ticker."""
    from sqlalchemy import text
    row = session.execute(text(
        "SELECT 1 FROM strategy_supported_ticker_types "
        "WHERE strategy_id = :sid AND sec_type = :st LIMIT 1"
    ), {"sid": strategy_id, "st": sec_type}).fetchone()
    if row is None:
        # Also look up supported types for a helpful error message.
        supported = session.execute(text(
            "SELECT sec_type FROM strategy_supported_ticker_types "
            "WHERE strategy_id = :sid ORDER BY sec_type"
        ), {"sid": strategy_id}).fetchall()
        allowed = ", ".join(r[0] for r in supported) or "(none configured)"
        raise HTTPException(
            400,
            f"Strategy {strategy_id} does not support sec_type='{sec_type}'. "
            f"Supported: {allowed}. See strategy_supported_ticker_types table."
        )


@router.get("/tickers")
def list_tickers(strategy_id: Optional[int] = None):
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        q = session.query(Ticker)
        if strategy_id is not None:
            q = q.filter(Ticker.strategy_id == strategy_id)
        tickers = q.order_by(Ticker.id).all()
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
        # ENH-042: Validate sec_type is allowed for this strategy.
        sec_type = (req.sec_type or "OPT").upper()
        _validate_ticker_type_for_strategy(session, req.strategy_id, sec_type)

        existing = session.query(Ticker).filter(
            Ticker.symbol == req.symbol.upper(),
            Ticker.strategy_id == req.strategy_id,
        ).first()
        if existing:
            raise HTTPException(
                409,
                f"Ticker {req.symbol.upper()} already exists for strategy_id={req.strategy_id}",
            )
        ticker = Ticker(
            symbol=req.symbol.upper(),
            strategy_id=req.strategy_id,
            sec_type=sec_type,
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
