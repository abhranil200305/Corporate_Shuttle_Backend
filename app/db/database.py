# app/db/database.py

import os
import asyncio
from typing import AsyncGenerator
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy.exc import OperationalError

# ---------------------------
# Load environment variables
# ---------------------------
load_dotenv()
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/corporate_shuttle_db"
)

# ---------------------------
# Base for models
# ---------------------------
Base = declarative_base()

# ---------------------------
# Async Engine with Controlled Connection Pool
# ---------------------------
# pool_size + max_overflow should stay small to avoid exhausting DB connections
engine = create_async_engine(
    DATABASE_URL,
    echo=True,           # turn off in production
    pool_size=5,         # persistent connections
    max_overflow=2,      # temporary connections
    pool_timeout=30,     # wait before failing
    pool_pre_ping=True,  # automatically reconnect dead connections
)

# ---------------------------
# Async Session Factory
# ---------------------------
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# ---------------------------
# FastAPI Dependency with Retry
# ---------------------------
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Provides DB session with proper connection reuse.
    Includes retry for transient OperationalError.
    """
    retries = 3
    delay = 1  # seconds
    for attempt in range(retries):
        try:
            async with AsyncSessionLocal() as session:
                yield session
            break  # success
        except OperationalError as e:
            if attempt < retries - 1:
                await asyncio.sleep(delay)
            else:
                raise e

# ---------------------------
# DEV ONLY: Initialize DB
# ---------------------------
def init_db():
    """
    Create all tables for development.
    Alembic should be used for production migrations.
    """
    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())

# ---------------------------
# Notes for Alembic Async Support
# ---------------------------
# In alembic/env.py, you can do:
# from app.db.database import engine
# connectable = engine