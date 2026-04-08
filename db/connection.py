"""
Database connection — synchronous SQLAlchemy engine for PostgreSQL.
Reads DATABASE_URL from environment. Returns None if not configured.
"""
import os
import logging

log = logging.getLogger(__name__)

_engine = None
_SessionLocal = None


def get_engine():
    """Get or create the SQLAlchemy engine. Returns None if DATABASE_URL not set."""
    global _engine
    if _engine is not None:
        return _engine

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        log.info("DATABASE_URL not set — database features disabled")
        return None

    try:
        from sqlalchemy import create_engine
        _engine = create_engine(
            db_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            echo=False,
        )
        log.info(f"Database engine created: {db_url.split('@')[-1] if '@' in db_url else db_url}")
        return _engine
    except Exception as e:
        log.error(f"Failed to create database engine: {e}")
        return None


def get_session():
    """Get a new database session. Returns None if no engine available."""
    global _SessionLocal
    engine = get_engine()
    if engine is None:
        return None

    if _SessionLocal is None:
        from sqlalchemy.orm import sessionmaker
        _SessionLocal = sessionmaker(bind=engine)

    return _SessionLocal()


def db_available() -> bool:
    """Check if the database is configured and reachable."""
    engine = get_engine()
    if engine is None:
        return False
    try:
        with engine.connect() as conn:
            conn.execute(__import__('sqlalchemy').text("SELECT 1"))
        return True
    except Exception:
        return False
