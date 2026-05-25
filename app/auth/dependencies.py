# app/auth/dependencies.py
from __future__ import annotations

import logging
from typing import AsyncGenerator

logger = logging.getLogger(__name__)

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.exceptions import AuthError
from app.auth.service import AuthService
from app.auth.session_utils import extract_bearer_token
from app.db.database import get_async_session
from app.db.schema import User, UserRole


# ---------------------------
# Database session dependency
# ---------------------------
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Async FastAPI dependency to provide a database session.
    
    Usage:
        @app.get("/endpoint")
        async def route(db: AsyncSession = Depends(get_db)):
            ...
    """
    async for session in get_async_session():
        yield session


# ---------------------------
# AuthService dependency
# ---------------------------
async def get_auth_service(
    db: AsyncSession = Depends(get_db),
) -> AuthService:
    """
    Returns an instance of AuthService using the async session.
    """
    return AuthService(db)


# ---------------------------
# HTTP exception converter
# ---------------------------
def to_http_exception(exc: AuthError) -> HTTPException:
    """
    Converts a custom AuthError into a FastAPI HTTPException.

    Server log intentionally includes only the safe auth error contract:
    status code, machine error code, and human message.
    It does not log email, OTP, bearer token, or request body.
    """
    logger.warning(
        "auth_error status=%s code=%s message=%s",
        exc.status_code,
        exc.error_code,
        exc.message,
    )

    return HTTPException(
        status_code=exc.status_code,
        detail={
            "error": exc.error_code,
            "message": exc.message,
        },
    )


# ---------------------------
# Bearer token extractor
# ---------------------------
def get_bearer_token_from_request(request: Request) -> str:
    """
    Extracts the Bearer token from the Authorization header.
    """
    authorization = request.headers.get("Authorization")
    token = extract_bearer_token(authorization)
    if not token:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "missing_bearer_token",
                "message": "Authorization bearer token is required.",
            },
        )
    return token


# ---------------------------
# Current user dependencies
# ---------------------------
async def get_current_user(
    token: str = Depends(get_bearer_token_from_request),
    auth_service: AuthService = Depends(get_auth_service),
) -> User:
    """
    Resolves the current user from the provided token using AuthService.
    """
    try:
        return await auth_service.authenticate_token(token)
    except AuthError as exc:
        raise to_http_exception(exc) from exc


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Returns the current active user (no additional checks here).
    """
    return current_user


async def get_current_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Ensures the current user is an admin.
    """
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "insufficient_permissions",
                "message": "Access denied. Admin privileges required.",
            },
        )
    return current_user