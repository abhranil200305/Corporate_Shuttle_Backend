from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth.exceptions import AuthError
from app.auth.service import AuthService
from app.auth.session_utils import extract_bearer_token
from app.db.database import SessionLocal
from app.db.schema import User, UserRole


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_auth_service(db: Session = Depends(get_db)) -> AuthService:
    return AuthService(db)


def to_http_exception(exc: AuthError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={
            "error": exc.error_code,
            "message": exc.message,
        },
    )


def get_bearer_token_from_request(request: Request) -> str:
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


def get_current_user(
    token: str = Depends(get_bearer_token_from_request),
    auth_service: AuthService = Depends(get_auth_service),
) -> User:
    try:
        return auth_service.authenticate_token(token)
    except AuthError as exc:
        raise to_http_exception(exc) from exc


def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    return current_user


def get_current_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "insufficient_permissions",
                "message": "Access denied. Admin privileges required.",
            },
        )
    return current_user