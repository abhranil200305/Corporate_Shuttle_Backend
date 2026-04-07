# app/db/database.py

from __future__ import annotations

import asyncio
import os
from typing import AsyncGenerator

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.schema import Base

# ---------------------------
# Load environment variables
# ---------------------------
load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/corporate_shuttle_db",
).strip()


def _get_int_env(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc

    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}.")
    return value


def _get_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


DB_ECHO = _get_bool_env("DB_ECHO", False)
DB_POOL_SIZE = _get_int_env("DB_POOL_SIZE", 5, minimum=1)
DB_MAX_OVERFLOW = _get_int_env("DB_MAX_OVERFLOW", 2, minimum=0)
DB_POOL_TIMEOUT = _get_int_env("DB_POOL_TIMEOUT", 30, minimum=1)
DB_POOL_RECYCLE_SECONDS = _get_int_env("DB_POOL_RECYCLE_SECONDS", 1800, minimum=1)
DB_POOL_PRE_PING = _get_bool_env("DB_POOL_PRE_PING", True)
DB_POOL_USE_LIFO = _get_bool_env("DB_POOL_USE_LIFO", True)

# ---------------------------
# Shared async engine / shared pool (per process)
# ---------------------------
engine = create_async_engine(
    DATABASE_URL,
    echo=DB_ECHO,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_timeout=DB_POOL_TIMEOUT,
    pool_recycle=DB_POOL_RECYCLE_SECONDS,
    pool_pre_ping=DB_POOL_PRE_PING,
    pool_use_lifo=DB_POOL_USE_LIFO,
    pool_reset_on_return="rollback",
)

# ---------------------------
# Shared async session factory
# ---------------------------
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

# ---------------------------
# DB health / lifecycle helpers
# ---------------------------
async def ping_database(*, retries: int = 3, delay_seconds: float = 1.0) -> None:
    """
    Verifies the DB is reachable.
    The connection is automatically returned to the pool after the context exits.
    """
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return
        except OperationalError as exc:
            last_error = exc
            if attempt >= retries:
                break
            await asyncio.sleep(delay_seconds)

    assert last_error is not None
    raise last_error


async def dispose_database_engine() -> None:
    """
    Closes all checked-in pooled connections and resets the pool.
    """
    await engine.dispose()

# ---------------------------
# FastAPI dependency
# ---------------------------
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """
    One request/job/task -> one AsyncSession.
    Sessions are short-lived; the pool beneath them is shared.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

# ---------------------------
# DEV ONLY: Initialize DB
# ---------------------------
def init_db() -> None:
    """
    Create all tables for development.
    Alembic should be used for production migrations.
    """
    async def _init() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())