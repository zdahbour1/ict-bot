"""
SQLAlchemy ORM models for all 8 database tables.
"""
from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Boolean, Text, Numeric, DateTime,
    ForeignKey, CheckConstraint, UniqueConstraint, Index
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account = Column(String(20), nullable=False, index=True)
    ticker = Column(String(10), nullable=False, index=True)
    symbol = Column(String(40), nullable=False)
    direction = Column(String(5), nullable=False, default="LONG")

    contracts_entered = Column(Integer, nullable=False)
    contracts_open = Column(Integer, nullable=False)
    contracts_closed = Column(Integer, nullable=False, default=0)

    entry_price = Column(Numeric(10, 4), nullable=False)
    exit_price = Column(Numeric(10, 4))
    current_price = Column(Numeric(10, 4))
    ib_fill_price = Column(Numeric(10, 4))
    ib_order_id = Column(Integer)

    pnl_pct = Column(Numeric(8, 4), default=0)
    pnl_usd = Column(Numeric(12, 4), default=0)
    peak_pnl_pct = Column(Numeric(8, 4), default=0)
    dynamic_sl_pct = Column(Numeric(8, 4), default=-0.60)

    profit_target = Column(Numeric(10, 4), nullable=False)
    stop_loss_level = Column(Numeric(10, 4), nullable=False)

    signal_type = Column(String(40))
    ict_entry = Column(Numeric(10, 4))
    ict_sl = Column(Numeric(10, 4))
    ict_tp = Column(Numeric(10, 4))

    entry_time = Column(DateTime(timezone=True), nullable=False)
    exit_time = Column(DateTime(timezone=True))

    status = Column(String(10), nullable=False, default="open", index=True)
    exit_reason = Column(String(40))
    exit_result = Column(String(10))
    error_message = Column(Text)

    entry_enrichment = Column(JSONB, default={})
    exit_enrichment = Column(JSONB, default={})

    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    closes = relationship("TradeClose", back_populates="trade", cascade="all, delete-orphan")
    commands = relationship("TradeCommand", back_populates="trade", cascade="all, delete-orphan")
    errors = relationship("Error", back_populates="trade")


class TradeClose(Base):
    __tablename__ = "trade_closes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(Integer, ForeignKey("trades.id", ondelete="CASCADE"), nullable=False, index=True)
    contracts = Column(Integer, nullable=False)
    close_price = Column(Numeric(10, 4), nullable=False)
    pnl_pct = Column(Numeric(8, 4), nullable=False)
    pnl_usd = Column(Numeric(12, 4), nullable=False)
    reason = Column(String(40), nullable=False)
    ib_order_id = Column(Integer)
    ib_fill_price = Column(Numeric(10, 4))
    closed_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    trade = relationship("Trade", back_populates="closes")


class TradeCommand(Base):
    __tablename__ = "trade_commands"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(Integer, ForeignKey("trades.id", ondelete="CASCADE"), nullable=False, index=True)
    command = Column(String(20), nullable=False)
    contracts = Column(Integer)
    status = Column(String(20), nullable=False, default="pending", index=True)
    error = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    executed_at = Column(DateTime(timezone=True))

    trade = relationship("Trade", back_populates="commands")


class ThreadStatus(Base):
    __tablename__ = "thread_status"

    id = Column(Integer, primary_key=True, autoincrement=True)
    thread_name = Column(String(30), nullable=False, unique=True)
    ticker = Column(String(10))
    status = Column(String(20), nullable=False, default="idle")
    pid = Column(Integer)
    thread_id = Column(Integer)  # Python threading.get_ident()
    last_scan_time = Column(DateTime(timezone=True))
    last_message = Column(Text)
    scans_today = Column(Integer, default=0)
    trades_today = Column(Integer, default=0)
    alerts_today = Column(Integer, default=0)
    error_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class BotState(Base):
    __tablename__ = "bot_state"

    id = Column(Integer, primary_key=True, default=1)
    status = Column(String(20), nullable=False, default="stopped")
    account = Column(String(20))
    pid = Column(Integer)
    total_tickers = Column(Integer, default=0)
    scans_active = Column(Boolean, default=False)
    stop_requested = Column(Boolean, default=False)
    ib_connected = Column(Boolean, default=False)
    last_error = Column(Text)
    started_at = Column(DateTime(timezone=True))
    stopped_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class SystemLog(Base):
    __tablename__ = "system_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    component = Column(String(30), nullable=False)
    level = Column(String(10), nullable=False, default="info")
    message = Column(Text, nullable=False)
    details = Column(JSONB, default={})
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class Error(Base):
    __tablename__ = "errors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    thread_name = Column(String(30))
    ticker = Column(String(10), index=True)
    trade_id = Column(Integer, ForeignKey("trades.id", ondelete="SET NULL"))
    error_type = Column(String(50), nullable=False)
    message = Column(Text, nullable=False)
    traceback = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)

    trade = relationship("Trade", back_populates="errors")


class Ticker(Base):
    __tablename__ = "tickers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(10), nullable=False, unique=True)
    name = Column(String(100))
    is_active = Column(Boolean, nullable=False, default=True)
    contracts = Column(Integer, nullable=False, default=2)
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    category = Column(String(30), nullable=False, index=True)
    key = Column(String(50), nullable=False, unique=True)
    value = Column(Text, nullable=False)
    data_type = Column(String(20), nullable=False, default="string")
    description = Column(Text)
    is_secret = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
