# app/db/database.py

import os
from typing import Generator
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from app.db.schema import Base  # ✅ import Base from schema.py

# ---------------------------
# Load environment variables
# ---------------------------
load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/corporate_shuttle_db"  # fallback
)

# ---------------------------
# SQLAlchemy Engine
# ---------------------------
engine = create_engine(
    DATABASE_URL,
    echo=True,          # Print SQL queries in console
    pool_pre_ping=True
)

# ---------------------------
# Session factory
# ---------------------------
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

# ---------------------------
# FastAPI dependency
# ---------------------------
def get_db() -> Generator[Session, None, None]:
    """
    Dependency for FastAPI routes to get a SQLAlchemy session.
    Usage:
        @app.get("/users")
        def read_users(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------------------------
# Optional: initialize all tables (dev only)
# ---------------------------
def init_db():
    """
    Create all tables based on Base metadata.
    Useful for initial dev/testing (not recommended in production if using Alembic)
    """
    Base.metadata.create_all(bind=engine)