# app/db/database.py
import os
from typing import AsyncGenerator
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from app.db.schema import Base

# ---------------------------
# Load environment variables
# ---------------------------
load_dotenv()

# Ensure your .env DATABASE_URL uses asyncpg
# Example: postgresql+asyncpg://user:password@host:port/dbname
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/corporate_shuttle_db"
)

# ---------------------------
# Async SQLAlchemy Engine
# ---------------------------
engine = create_async_engine(
    DATABASE_URL,
    echo=True,         # Show SQL queries in console
    pool_pre_ping=True,
)

# ---------------------------
# Async Session Factory
# ---------------------------
async_sessionmaker = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# ---------------------------
# FastAPI Dependency
# ---------------------------
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Async FastAPI dependency for getting a database session.

    Usage:
        @app.get("/endpoint")
        async def route(db: AsyncSession = Depends(get_async_session)):
            ...
    """
    async with async_sessionmaker() as session:
        yield session

# ---------------------------
# Optional: Initialize all tables (dev only)
# ---------------------------
def init_db():
    """
    Create all tables based on Base metadata.
    Only for initial development/testing.
    Use Alembic migrations in production.
    """
    import asyncio

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())