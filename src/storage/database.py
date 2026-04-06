"""
database.py — Async SQLAlchemy engine setup and session factory.

Design:
  - One engine instance per process (module-level singleton)
  - `get_session()` yields an AsyncSession via async context manager
  - `init_db()` creates all tables if they don't exist (idempotent)
  - The DATABASE_URL environment variable overrides the default SQLite path

Switching to PostgreSQL (production):
  Set DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/qa_bot
  No code changes required — SQLAlchemy handles dialect differences.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.logger import get_logger
from src.storage.models import Base

log = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Engine singleton
# ─────────────────────────────────────────────────────────────────────────────

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker | None = None


def _default_db_url(output_dir: str = "outputs") -> str:
    """Build the default SQLite URL, creating the directory if needed."""
    db_dir = Path(output_dir)
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "qa_bot.db"
    return f"sqlite+aiosqlite:///{db_path.resolve()}"


def _get_db_url(output_dir: str = "outputs") -> str:
    """
    Resolve the database URL from environment or default SQLite path.
    Supports:
      sqlite+aiosqlite:///path/to/file.db
      postgresql+asyncpg://user:pass@host:5432/dbname
    """
    env_url = os.getenv("DATABASE_URL", "").strip()
    if env_url:
        log.info("database_url_from_env", url=env_url.split("@")[-1])  # Hide credentials
        return env_url
    url = _default_db_url(output_dir)
    log.info("database_url_default", url=url)
    return url


async def get_engine(output_dir: str = "outputs") -> AsyncEngine:
    """
    Return (or create) the module-level async engine.
    Thread-safe: asyncio event loop prevents concurrent initialisation.
    """
    global _engine
    if _engine is None:
        url = _get_db_url(output_dir)
        _engine = create_async_engine(
            url,
            echo=False,             # Set to True for SQL query logging
            pool_pre_ping=True,     # Verify connections before use
            # SQLite-specific: WAL mode allows concurrent reads + 1 write
            connect_args={"check_same_thread": False} if "sqlite" in url else {},
        )
        log.debug("engine_created", dialect=_engine.dialect.name)
    return _engine


async def init_db(output_dir: str = "outputs") -> None:
    """
    Create all tables defined in models.py if they don't exist.
    Safe to call multiple times (CREATE TABLE IF NOT EXISTS semantics).
    """
    engine = await get_engine(output_dir)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("database_initialised", tables=list(Base.metadata.tables.keys()))


async def close_db() -> None:
    """Dispose of the engine pool. Call at application shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        log.debug("database_closed")


# ─────────────────────────────────────────────────────────────────────────────
# Session context manager
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def get_session(output_dir: str = "outputs") -> AsyncIterator[AsyncSession]:
    """
    Yield an AsyncSession for one unit of work.

    Usage:
        async with get_session() as session:
            session.add(model_instance)
            await session.commit()

    Automatically rolls back on exception and closes the session.
    """
    global _session_factory
    engine = await get_engine(output_dir)

    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=engine,
            expire_on_commit=False,   # Keep attributes accessible after commit
            autoflush=False,          # We control when to flush
        )

    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
