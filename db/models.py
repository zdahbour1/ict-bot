"""
SQLAlchemy ORM models for all 8 database tables.
"""
from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Boolean, Text, Numeric, Date, DateTime,
    TIMESTAMP,
    ForeignKey, CheckConstraint, UniqueConstraint, Index
)
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
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
    ib_perm_id = Column(Integer)       # IB permanent order ID (survives restarts)
    ib_tp_perm_id = Column(Integer)    # TP bracket leg permanent ID
    ib_sl_perm_id = Column(Integer)    # SL bracket leg permanent ID
    ib_con_id = Column(Integer)        # IB contract ID (unique per option)

    # Bracket health — refreshed by reconcile PASS 4 every cycle.
    # See strategy/reconciliation.py and docs/logging_and_audit.md.
    ib_tp_order_id = Column(Integer)    # current orderId for TP (may change)
    ib_sl_order_id = Column(Integer)    # current orderId for SL (may change)
    ib_tp_status = Column(String(20))   # Submitted / PreSubmitted / Cancelled / MISSING / etc.
    ib_sl_status = Column(String(20))
    ib_tp_price = Column(Numeric(10, 4))   # working TP limit price from IB
    ib_sl_price = Column(Numeric(10, 4))   # working SL stop price from IB
    ib_brackets_checked_at = Column(TIMESTAMP(timezone=True))

    # IB↔DB correlation ID — human-readable, tagged on every bracket
    # leg via IB Order.orderRef. Format: TICKER-YYMMDD-NN.
    # See docs/ib_db_correlation.md and db/trade_ref.py.
    client_trade_id = Column(String(20))

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
    notes = Column(Text)

    # ENH-024 rollout #1: strategy attribution (audit only — logic unchanged)
    strategy_id = Column(Integer, ForeignKey("strategies.strategy_id"),
                         nullable=False, index=True)

    # Roadmap schema extensions (forward-compatible; defaults = today's behavior)
    # Security typing for OPT / FOP / STK / FUT / BAG
    sec_type = Column(String(5), nullable=False, default="OPT")
    multiplier = Column(Integer, nullable=False, default=100)
    exchange = Column(String(20), nullable=False, default="SMART")
    currency = Column(String(5), nullable=False, default="USD")
    underlying = Column(String(20))
    # Snapshot of strategy tuning params at trade time (distinct from
    # strategy_id/signal_type — this captures the exact values like
    # PROFIT_TARGET/STOP_LOSS/ROLL_THRESHOLD that produced this trade).
    strategy_config = Column(JSONB, nullable=False, default=dict)

    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    closes = relationship("TradeClose", back_populates="trade", cascade="all, delete-orphan")
    commands = relationship("TradeCommand", back_populates="trade", cascade="all, delete-orphan")
    errors = relationship("Error", back_populates="trade")
    strategy = relationship("Strategy", back_populates="trades")


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
    symbol = Column(String(10), nullable=False)  # unique WITH strategy_id, see table_args
    name = Column(String(100))
    is_active = Column(Boolean, nullable=False, default=True)
    contracts = Column(Integer, nullable=False, default=2)
    notes = Column(Text)
    strategy_id = Column(Integer, ForeignKey("strategies.strategy_id"),
                         nullable=False, index=True)

    # Roadmap schema extensions (defaults = today's equity-options behavior)
    sec_type = Column(String(5), nullable=False, default="OPT")
    multiplier = Column(Integer, nullable=False, default=100)
    exchange = Column(String(20), nullable=False, default="SMART")
    currency = Column(String(5), nullable=False, default="USD")

    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    strategy = relationship("Strategy", back_populates="tickers")

    __table_args__ = (
        UniqueConstraint("symbol", "strategy_id", name="uniq_ticker_per_strategy"),
    )


class Setting(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    category = Column(String(30), nullable=False, index=True)
    key = Column(String(50), nullable=False)  # unique WITH strategy_id, see table_args
    value = Column(Text, nullable=False)
    data_type = Column(String(20), nullable=False, default="string")
    description = Column(Text)
    is_secret = Column(Boolean, nullable=False, default=False)
    # Nullable strategy_id: NULL = global (infra/account), non-NULL = per-strategy override
    strategy_id = Column(Integer, ForeignKey("strategies.strategy_id"),
                         nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    strategy = relationship("Strategy", back_populates="settings")

    __table_args__ = (
        UniqueConstraint("key", "strategy_id", name="uniq_setting_per_scope"),
    )


class Strategy(Base):
    """ENH-024 rollout #1: strategies table.

    Each trading strategy registered in the system (ICT, ORB, VWAP, ...).
    The `strategy_id` is the user-facing stable ID and the FK target on
    child tables (trades, tickers, settings).

    Only one strategy may have `is_default=True` at a time (enforced by
    partial unique index in the SQL schema).
    """
    __tablename__ = "strategies"

    strategy_id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(30), nullable=False, unique=True)
    display_name = Column(String(80), nullable=False)
    description = Column(Text)
    class_path = Column(String(200), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    is_default = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    trades = relationship("Trade", back_populates="strategy")
    tickers = relationship("Ticker", back_populates="strategy")
    settings = relationship("Setting", back_populates="strategy")


# ── Test Run History (ARCH-004) ─────────────────────────────
# One row per `pytest` invocation — captures the summary so we can
# chart pass/fail trend over time in the dashboard.

class TestRun(Base):
    __tablename__ = "test_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    git_branch = Column(String(80), index=True)
    git_sha = Column(String(40), index=True)
    suite = Column(String(40), nullable=False, default="unit", index=True)
    # Counts
    total = Column(Integer, nullable=False, default=0)
    passed = Column(Integer, nullable=False, default=0)
    failed = Column(Integer, nullable=False, default=0)
    skipped = Column(Integer, nullable=False, default=0)
    errors = Column(Integer, nullable=False, default=0)
    # Timing
    duration_sec = Column(Numeric(10, 3), nullable=False, default=0)
    started_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    finished_at = Column(DateTime(timezone=True))
    # Metadata
    triggered_by = Column(String(30), nullable=False, default="manual")  # manual | ci | pre-commit
    python_version = Column(String(20))
    platform = Column(String(40))
    exit_status = Column(String(20), nullable=False, default="pending")  # pending | passed | failed | error
    summary = Column(Text)  # short human-readable summary line

    results = relationship("TestResult", back_populates="run", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_test_runs_started", "started_at"),
    )


class TestResult(Base):
    """One row per test that ran inside a TestRun."""
    __tablename__ = "test_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("test_runs.id", ondelete="CASCADE"),
                    nullable=False, index=True)
    nodeid = Column(Text, nullable=False)     # e.g. tests/unit/test_foo.py::TestBar::test_baz
    module = Column(String(200), index=True)  # tests/unit/test_foo.py
    test_class = Column(String(100))
    test_name = Column(String(200))
    outcome = Column(String(10), nullable=False, index=True)  # passed | failed | skipped | error
    duration_sec = Column(Numeric(10, 4))
    error_message = Column(Text)  # only populated on failure
    traceback = Column(Text)      # only populated on failure
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    run = relationship("TestRun", back_populates="results")


# ── Backtest Framework (ENH-019) ────────────────────────────

class BacktestRun(Base):
    """One row per backtest execution."""
    __tablename__ = "backtest_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100))
    status = Column(String(20), nullable=False, default="pending")  # pending | running | completed | failed

    strategy_id = Column(Integer, ForeignKey("strategies.strategy_id"),
                         nullable=False, index=True)

    tickers = Column(ARRAY(Text), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    config = Column(JSONB, nullable=False, default=dict)

    total_trades = Column(Integer, default=0)
    wins = Column(Integer, default=0)
    losses = Column(Integer, default=0)
    scratches = Column(Integer, default=0)
    total_pnl = Column(Numeric(12, 2), default=0)
    win_rate = Column(Numeric(5, 2), default=0)
    avg_win = Column(Numeric(12, 2), default=0)
    avg_loss = Column(Numeric(12, 2), default=0)
    max_drawdown = Column(Numeric(12, 2), default=0)
    sharpe_ratio = Column(Numeric(8, 4))
    profit_factor = Column(Numeric(8, 4))
    avg_hold_min = Column(Numeric(8, 1))
    max_win_streak = Column(Integer, default=0)
    max_loss_streak = Column(Integer, default=0)

    error_message = Column(Text)
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    duration_sec = Column(Numeric(10, 2))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    notes = Column(Text)

    strategy = relationship("Strategy")
    trades = relationship("BacktestTrade", back_populates="run",
                          cascade="all, delete-orphan")


class BacktestTrade(Base):
    """One row per simulated trade inside a BacktestRun."""
    __tablename__ = "backtest_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("backtest_runs.id", ondelete="CASCADE"),
                    nullable=False, index=True)
    strategy_id = Column(Integer, ForeignKey("strategies.strategy_id"),
                         nullable=False, index=True)

    ticker = Column(String(10), nullable=False, index=True)
    symbol = Column(String(40))
    direction = Column(String(5), nullable=False)  # LONG | SHORT
    contracts = Column(Integer, nullable=False, default=2)

    entry_price = Column(Numeric(10, 4), nullable=False)
    exit_price = Column(Numeric(10, 4))
    pnl_pct = Column(Numeric(8, 4), default=0)
    pnl_usd = Column(Numeric(12, 4), default=0)
    peak_pnl_pct = Column(Numeric(8, 4), default=0)
    slippage_paid = Column(Numeric(10, 4), default=0)
    commission = Column(Numeric(10, 4), default=0)

    entry_time = Column(DateTime(timezone=True), nullable=False)
    exit_time = Column(DateTime(timezone=True))
    hold_minutes = Column(Numeric(8, 1))

    signal_type = Column(String(40), index=True)
    entry_bar_idx = Column(Integer)

    exit_reason = Column(String(20))  # TP | SL | TRAIL_STOP | ROLL | TIME_EXIT | EOD_EXIT
    exit_result = Column(String(10), index=True)  # WIN | LOSS | SCRATCH

    tp_level = Column(Numeric(10, 4))
    sl_level = Column(Numeric(10, 4))
    dynamic_sl_pct = Column(Numeric(8, 4))
    tp_trailed = Column(Boolean, default=False)
    rolled = Column(Boolean, default=False)

    entry_indicators = Column(JSONB, default=dict)
    exit_indicators = Column(JSONB, default=dict)
    entry_context = Column(JSONB, default=dict)
    signal_details = Column(JSONB, default=dict)

    # Roadmap schema extensions (defaults = today's equity-options behavior)
    sec_type = Column(String(5), nullable=False, default="OPT")
    multiplier = Column(Integer, nullable=False, default=100)
    exchange = Column(String(20), nullable=False, default="SMART")
    currency = Column(String(5), nullable=False, default="USD")
    underlying = Column(String(20))
    strategy_config = Column(JSONB, nullable=False, default=dict)

    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    run = relationship("BacktestRun", back_populates="trades")
    strategy = relationship("Strategy")
