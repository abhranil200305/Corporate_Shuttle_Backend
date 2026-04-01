# app/db/database.py

import os
from typing import AsyncGenerator
from dotenv import load_dotenv

from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncSession,
    async_sessionmaker,
)
from app.db.schema import Base

# ---------------------------
# Load environment variables
# ---------------------------
load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/corporate_shuttle_db"
)

# ---------------------------
# Async SQLAlchemy Engine (POOL CONTROLLED)
# ---------------------------
engine = create_async_engine(
    DATABASE_URL,
    echo=True,               # 🔁 turn OFF in production
    pool_size=10,            # ✅ base pool connections
    max_overflow=5,          # ✅ extra temporary connections
    pool_timeout=30,         # ✅ wait before timeout
    pool_pre_ping=True,      # ✅ reconnect dead connections
)

# 👉 TOTAL MAX CONNECTIONS = pool_size + max_overflow = 15

# ---------------------------
# Async Session Factory (CORRECT)
# ---------------------------
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# ---------------------------
# FastAPI Dependency (SAFE)
# ---------------------------
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Provides DB session with proper connection reuse
    """

    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()   # extra safety (ensures release)

# ---------------------------
# Optional: Initialize DB (DEV ONLY)
# ---------------------------
def init_db():
    """
    Create tables (dev only)
    Use Alembic in production
    """
    import asyncio

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())