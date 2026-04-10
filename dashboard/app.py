"""
ICT Trading Bot — Dashboard API
FastAPI backend serving trade data, ticker management, settings, and real-time updates.
"""
import os
import sys
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import socketio

# Add parent dir to path so we can import db module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger(__name__)

# Socket.IO server
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")


async def trade_poller():
    """Background task: polls DB for trade changes and emits Socket.IO updates."""
    from db.connection import db_available
    while True:
        try:
            if db_available():
                from db.connection import get_session
                from db.models import Trade
                session = get_session()
                if session:
                    # Get all open trades
                    open_trades = session.query(Trade).filter(Trade.status == "open").all()
                    trades_data = [
                        {
                            "id": t.id, "ticker": t.ticker, "symbol": t.symbol,
                            "direction": t.direction,
                            "contracts_entered": t.contracts_entered,
                            "contracts_open": t.contracts_open,
                            "entry_price": float(t.entry_price) if t.entry_price else 0,
                            "current_price": float(t.current_price) if t.current_price else 0,
                            "pnl_pct": float(t.pnl_pct) if t.pnl_pct else 0,
                            "pnl_usd": float(t.pnl_usd) if t.pnl_usd else 0,
                            "peak_pnl_pct": float(t.peak_pnl_pct) if t.peak_pnl_pct else 0,
                            "dynamic_sl_pct": float(t.dynamic_sl_pct) if t.dynamic_sl_pct else 0,
                            "status": t.status,
                            "entry_time": t.entry_time.isoformat() if t.entry_time else None,
                        }
                        for t in open_trades
                    ]
                    session.close()
                    await sio.emit("trade_update", {"trades": trades_data})
        except Exception as e:
            log.debug(f"Trade poller error: {e}")
        await asyncio.sleep(5)


async def thread_poller():
    """Background task: polls thread_status table and emits updates."""
    from db.connection import db_available
    while True:
        try:
            if db_available():
                from db.connection import get_session
                from db.models import ThreadStatus
                session = get_session()
                if session:
                    threads = session.query(ThreadStatus).all()
                    threads_data = [
                        {
                            "thread_name": t.thread_name, "ticker": t.ticker,
                            "status": t.status, "last_message": t.last_message,
                            "scans_today": t.scans_today, "trades_today": t.trades_today,
                            "error_count": t.error_count,
                            "last_scan_time": t.last_scan_time.isoformat() if t.last_scan_time else None,
                        }
                        for t in threads
                    ]
                    session.close()
                    await sio.emit("thread_update", {"threads": threads_data})
        except Exception as e:
            log.debug(f"Thread poller error: {e}")
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background pollers on startup."""
    task1 = asyncio.create_task(trade_poller())
    task2 = asyncio.create_task(thread_poller())
    log.info("Dashboard API started — Socket.IO pollers running")
    yield
    task1.cancel()
    task2.cancel()


# FastAPI app
app = FastAPI(
    title="ICT Trading Bot Dashboard",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount Socket.IO
socket_app = socketio.ASGIApp(sio, app)

# Import and include route modules
from dashboard.routes import trades, tickers, settings, threads, bot, summary, analytics

app.include_router(trades.router, prefix="/api")
app.include_router(tickers.router, prefix="/api")
app.include_router(settings.router, prefix="/api")
app.include_router(threads.router, prefix="/api")
app.include_router(bot.router, prefix="/api")
app.include_router(summary.router, prefix="/api")
app.include_router(analytics.router, prefix="/api")


@app.get("/api/health")
def health():
    from db.connection import db_available
    return {"status": "ok", "db": db_available()}


# Socket.IO events
@sio.event
async def connect(sid, environ):
    log.info(f"Client connected: {sid}")


@sio.event
async def disconnect(sid):
    log.info(f"Client disconnected: {sid}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "dashboard.app:socket_app",
        host="0.0.0.0", port=8000, reload=True,
        log_level="info",
    )
