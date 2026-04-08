"""Trades API — list, detail, close, close-all."""
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel
from typing import Optional
from db.connection import get_session
from db.models import Trade, TradeCommand

router = APIRouter(tags=["trades"])


class CloseRequest(BaseModel):
    contracts: Optional[int] = None  # None = close all


def _trade_to_dict(t: Trade) -> dict:
    return {
        "id": t.id, "account": t.account, "ticker": t.ticker, "symbol": t.symbol,
        "direction": t.direction,
        "contracts_entered": t.contracts_entered, "contracts_open": t.contracts_open,
        "contracts_closed": t.contracts_closed,
        "entry_price": float(t.entry_price) if t.entry_price else None,
        "exit_price": float(t.exit_price) if t.exit_price else None,
        "current_price": float(t.current_price) if t.current_price else None,
        "ib_fill_price": float(t.ib_fill_price) if t.ib_fill_price else None,
        "pnl_pct": float(t.pnl_pct) if t.pnl_pct else 0,
        "pnl_usd": float(t.pnl_usd) if t.pnl_usd else 0,
        "peak_pnl_pct": float(t.peak_pnl_pct) if t.peak_pnl_pct else 0,
        "dynamic_sl_pct": float(t.dynamic_sl_pct) if t.dynamic_sl_pct else 0,
        "profit_target": float(t.profit_target) if t.profit_target else None,
        "stop_loss_level": float(t.stop_loss_level) if t.stop_loss_level else None,
        "signal_type": t.signal_type,
        "entry_time": t.entry_time.isoformat() if t.entry_time else None,
        "exit_time": t.exit_time.isoformat() if t.exit_time else None,
        "status": t.status, "exit_reason": t.exit_reason, "exit_result": t.exit_result,
        "error_message": t.error_message,
        "entry_enrichment": t.entry_enrichment or {},
        "exit_enrichment": t.exit_enrichment or {},
    }


@router.get("/trades")
def list_trades(
    status: Optional[str] = None,
    ticker: Optional[str] = None,
    sort: str = "entry_time",
    order: str = "desc",
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
):
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        q = session.query(Trade)
        if status:
            q = q.filter(Trade.status == status)
        if ticker:
            q = q.filter(Trade.ticker == ticker.upper())

        # Sorting
        col = getattr(Trade, sort, Trade.entry_time)
        q = q.order_by(col.desc() if order == "desc" else col.asc())

        total = q.count()
        trades = q.offset((page - 1) * limit).limit(limit).all()
        result = [_trade_to_dict(t) for t in trades]
        session.close()
        return {"trades": result, "total": total, "page": page, "limit": limit}
    finally:
        session.close()


@router.get("/trades/{trade_id}")
def get_trade(trade_id: int):
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        trade = session.query(Trade).filter(Trade.id == trade_id).first()
        if not trade:
            raise HTTPException(404, "Trade not found")
        result = _trade_to_dict(trade)
        session.close()
        return result
    finally:
        session.close()


@router.post("/trades/{trade_id}/close")
def close_trade(trade_id: int, req: CloseRequest = CloseRequest()):
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        trade = session.query(Trade).filter(Trade.id == trade_id).first()
        if not trade:
            raise HTTPException(404, "Trade not found")
        if trade.status != "open":
            raise HTTPException(400, f"Trade is already {trade.status}")

        cmd = TradeCommand(
            trade_id=trade_id,
            command="close_partial" if req.contracts else "close",
            contracts=req.contracts,
        )
        session.add(cmd)
        session.commit()
        session.close()
        return {"status": "command_queued", "command_id": cmd.id}
    finally:
        session.close()


@router.post("/trades/close-all")
def close_all_trades():
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        open_trades = session.query(Trade).filter(Trade.status == "open").all()
        if not open_trades:
            return {"status": "no_open_trades"}
        commands = []
        for t in open_trades:
            cmd = TradeCommand(trade_id=t.id, command="close")
            session.add(cmd)
            commands.append(t.id)
        session.commit()
        session.close()
        return {"status": "commands_queued", "trade_ids": commands, "count": len(commands)}
    finally:
        session.close()
