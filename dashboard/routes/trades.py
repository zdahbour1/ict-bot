"""Trades API — list, detail, close, close-all, notes, export."""
from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
from db.connection import get_session
from db.models import Trade, TradeCommand
import io

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
        "notes": t.notes,
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


@router.get("/trades/{trade_id}/audit")
def get_trade_audit(trade_id: int):
    """Return every system_log row that touched this trade, oldest first.

    Queries ``system_log.details->>'trade_id'`` which is populated by
    ``strategy.audit.log_trade_action``. Also includes any log line
    whose details contain ``from_trade_id`` or ``to_trade_id`` equal
    to the given id, so roll chains are visible from both sides.

    Use this to answer: "who opened this trade? who closed it? what
    reconciled it? no mystery actions."
    """
    session = get_session()
    if session is None:
        raise HTTPException(503, "Database not available")
    try:
        from sqlalchemy import text
        rows = session.execute(text(
            """
            SELECT id, component, level, message, details, created_at
            FROM system_log
            WHERE (details->>'trade_id')::int     = :tid
               OR (details->>'from_trade_id')::int = :tid
               OR (details->>'to_trade_id')::int   = :tid
            ORDER BY created_at ASC
            LIMIT 500
            """
        ), {"tid": trade_id}).fetchall()
        return {
            "trade_id": trade_id,
            "entries": [
                {
                    "id": r[0],
                    "component": r[1],
                    "level": r[2],
                    "message": r[3],
                    "details": r[4] or {},
                    "created_at": r[5].isoformat() if r[5] else None,
                }
                for r in rows
            ],
            "count": len(rows),
        }
    finally:
        session.close()


class NotesUpdate(BaseModel):
    notes: str = ""


@router.put("/trades/{trade_id}/notes")
def update_trade_notes(trade_id: int, body: NotesUpdate):
    """Update notes for a trade."""
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        trade = session.query(Trade).filter(Trade.id == trade_id).first()
        if not trade:
            raise HTTPException(404, f"Trade {trade_id} not found")
        trade.notes = body.notes
        session.commit()
        return {"status": "ok", "trade_id": trade_id, "notes": body.notes}
    finally:
        session.close()


@router.post("/trades/{trade_id}/close")
async def close_trade(trade_id: int, req: CloseRequest = CloseRequest()):
    """
    Smart close: checks IB position, decides what to do.
    1. If position closed on IB → mark closed in DB with IB exit price
    2. If position open on IB → cancel brackets, close on IB, update DB
    3. If can't reach IB (bot down) → queue command for when bot restarts
    """
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        trade = session.query(Trade).filter(Trade.id == trade_id).first()
        if not trade:
            raise HTTPException(404, "Trade not found")
        if trade.status != "open":
            raise HTTPException(400, f"Trade is already {trade.status}")

        # Try to close via sidecar (which talks to IB)
        try:
            import httpx
            import os
            sidecar_url = os.getenv("BOT_SIDECAR_URL", "http://host.docker.internal:9000")
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(f"{sidecar_url}/close-trade",
                    json={"trade_id": trade_id, "symbol": trade.symbol,
                          "ticker": trade.ticker, "contracts": req.contracts or trade.contracts_open,
                          "direction": trade.direction})
                if resp.status_code == 200:
                    result = resp.json()
                    # Update DB based on sidecar response
                    from datetime import datetime, timezone

                    # Use IB fill price, fall back to last known price
                    ib_exit_price = result.get("exit_price", 0)
                    exit_p = float(ib_exit_price) if ib_exit_price else float(trade.current_price or trade.entry_price or 0)

                    # Use IB execution time, fall back to now
                    ib_exit_time = result.get("exit_time")
                    if ib_exit_time:
                        try:
                            exit_time = datetime.fromisoformat(str(ib_exit_time).replace('Z', '+00:00'))
                        except Exception:
                            exit_time = datetime.now(timezone.utc)
                    else:
                        exit_time = datetime.now(timezone.utc)

                    if result.get("position_was_open") == False:
                        trade.exit_reason = "CLOSED (BRACKET/IB)"
                    else:
                        trade.exit_reason = "CLOSED (UI)"

                    trade.status = "closed"
                    trade.exit_time = exit_time
                    trade.exit_price = exit_p
                    trade.current_price = exit_p  # sync current with exit
                    entry = float(trade.entry_price) if trade.entry_price else 0
                    trade.pnl_pct = (exit_p - entry) / entry if entry > 0 else 0
                    trade.pnl_usd = (exit_p - entry) * 100 * trade.contracts_entered
                    trade.exit_result = "WIN" if trade.pnl_pct > 0 else "LOSS" if trade.pnl_pct < 0 else "SCRATCH"
                    trade.contracts_open = 0
                    trade.contracts_closed = trade.contracts_entered
                    session.commit()
                    session.close()
                    return {"status": "closed", "trade_id": trade_id, "detail": result}
        except Exception:
            pass  # Sidecar not available — fall back to command queue

        # Fallback: queue command for bot to process
        cmd = TradeCommand(
            trade_id=trade_id,
            command="close_partial" if req.contracts else "close",
            contracts=req.contracts,
        )
        session.add(cmd)
        session.commit()
        session.close()
        return {"status": "command_queued", "command_id": cmd.id,
                "note": "Bot will process when running"}
    finally:
        session.close()


@router.post("/trades/close-all")
async def close_all_trades():
    """Close all open trades. Tries sidecar first, falls back to command queue."""
    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        open_trades = session.query(Trade).filter(Trade.status == "open").all()
        if not open_trades:
            return {"status": "no_open_trades"}

        results = []
        for t in open_trades:
            try:
                import httpx
                import os
                sidecar_url = os.getenv("BOT_SIDECAR_URL", "http://host.docker.internal:9000")
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(f"{sidecar_url}/close-trade",
                        json={"trade_id": t.id, "symbol": t.symbol,
                              "ticker": t.ticker, "contracts": t.contracts_open,
                              "direction": t.direction})
                    if resp.status_code == 200:
                        from datetime import datetime, timezone
                        detail = resp.json()
                        ib_exit = detail.get("exit_price", 0)
                        exit_p = float(ib_exit) if ib_exit else float(t.current_price or t.entry_price or 0)
                        ib_time = detail.get("exit_time")
                        if ib_time:
                            try:
                                exit_time = datetime.fromisoformat(str(ib_time).replace('Z', '+00:00'))
                            except Exception:
                                exit_time = datetime.now(timezone.utc)
                        else:
                            exit_time = datetime.now(timezone.utc)
                        t.status = "closed"
                        t.exit_time = exit_time
                        t.exit_price = exit_p
                        t.current_price = exit_p
                        t.exit_reason = "CLOSED (UI CLOSE ALL)"
                        entry = float(t.entry_price) if t.entry_price else 0
                        t.pnl_pct = (exit_p - entry) / entry if entry > 0 else 0
                        t.pnl_usd = (exit_p - entry) * 100 * t.contracts_entered
                        t.exit_result = "WIN" if t.pnl_pct > 0 else "LOSS" if t.pnl_pct < 0 else "SCRATCH"
                        t.contracts_open = 0
                        t.contracts_closed = t.contracts_entered
                        results.append({"id": t.id, "status": "closed"})
                        continue
            except Exception:
                pass
            # Fallback: queue
            cmd = TradeCommand(trade_id=t.id, command="close")
            session.add(cmd)
            results.append({"id": t.id, "status": "queued"})

        session.commit()
        session.close()
        return {"status": "processed", "trades": results, "count": len(results)}
    finally:
        session.close()


@router.get("/trades/export")
def export_trades(
    status: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    """Export trades to Excel (.xlsx). Supports filtering by status and date range."""
    try:
        import openpyxl
        from openpyxl.styles import PatternFill, Font, Alignment
    except ImportError:
        raise HTTPException(500, "openpyxl not installed — run: pip install openpyxl")

    session = get_session()
    if not session:
        raise HTTPException(503, "Database not available")
    try:
        q = session.query(Trade)
        if status:
            q = q.filter(Trade.status == status)
        if start:
            q = q.filter(Trade.entry_time >= start)
        if end:
            q = q.filter(Trade.entry_time <= end + "T23:59:59")
        trades = q.order_by(Trade.entry_time.desc()).all()

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Trades"

        # Headers
        headers = ["ID", "Ticker", "Symbol", "Direction", "Contracts", "Entry Price",
                    "Exit Price", "P&L %", "P&L $", "Peak P&L %", "Status",
                    "Exit Reason", "Exit Result", "Signal Type", "Entry Time",
                    "Exit Time", "Notes"]
        header_fill = PatternFill("solid", fgColor="1F3864")
        header_font = Font(color="FFFFFF", bold=True, size=10)
        for col_idx, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=h)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        # Data rows
        win_fill = PatternFill("solid", fgColor="C6EFCE")
        loss_fill = PatternFill("solid", fgColor="FFC7CE")
        for row_idx, t in enumerate(trades, 2):
            pnl_pct = float(t.pnl_pct * 100) if t.pnl_pct else 0
            pnl_usd = float(t.pnl_usd) if t.pnl_usd else 0
            peak = float(t.peak_pnl_pct * 100) if t.peak_pnl_pct else 0
            row_data = [
                t.id, t.ticker, t.symbol, t.direction, t.contracts_entered,
                float(t.entry_price) if t.entry_price else None,
                float(t.exit_price) if t.exit_price else None,
                round(pnl_pct, 2), round(pnl_usd, 2), round(peak, 2),
                t.status, t.exit_reason, t.exit_result, t.signal_type,
                t.entry_time.strftime("%Y-%m-%d %H:%M") if t.entry_time else None,
                t.exit_time.strftime("%Y-%m-%d %H:%M") if t.exit_time else None,
                t.notes,
            ]
            fill = win_fill if t.exit_result == "WIN" else loss_fill if t.exit_result == "LOSS" else None
            for col_idx, val in enumerate(row_data, 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                if fill:
                    cell.fill = fill

        # Auto-width columns
        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 30)
        ws.freeze_panes = "A2"

        # Stream the file
        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        session.close()

        return StreamingResponse(
            buffer,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=trades_export.xlsx"}
        )
    finally:
        session.close()
