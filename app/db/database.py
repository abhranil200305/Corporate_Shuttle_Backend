# app/db/database.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
import os
from dotenv import load_dotenv

# Load .env variables
load_dotenv()

# DATABASE_URL from .env
DATABASE_URL = os.getenv("DATABASE_URL")

# Create SQLAlchemy engine
engine = create_engine(
    DATABASE_URL,
    echo=True,           # Show SQL statements in console (good for debugging)
    pool_pre_ping=True   # Automatically checks connection health
)

# SessionLocal is used to get DB sessions
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

# Base class for models (must be inherited in schema.py)
Base = declarative_base()