from __future__ import annotations

import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from app.auth.constants import SESSION_EXPIRY_DAYS, SESSION_TOKEN_BYTES


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_session_hash_secret() -> bytes:
    secret = os.getenv("SESSION_HASH_SECRET", "").strip()
    if not secret:
        raise RuntimeError("SESSION_HASH_SECRET is not set.")
    return secret.encode("utf-8")


def generate_session_token(byte_length: int = SESSION_TOKEN_BYTES) -> str:
    if byte_length <= 0:
        raise ValueError("Session token byte length must be positive.")
    return secrets.token_urlsafe(byte_length)


def hash_session_token(token: str) -> str:
    if not token:
        raise ValueError("Session token cannot be empty.")

    return hmac.new(
        _get_session_hash_secret(),
        token.encode("utf-8"),
        digestmod="sha256",
    ).hexdigest()


def get_session_expiry(*, now: datetime | None = None) -> datetime:
    current_time = now or utcnow()
    return current_time + timedelta(days=SESSION_EXPIRY_DAYS)


def is_session_expired(expires_at: datetime, *, now: datetime | None = None) -> bool:
    current_time = now or utcnow()
    return expires_at <= current_time


def extract_bearer_token(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None

    value = authorization_header.strip()
    if not value:
        return None

    prefix = "Bearer "
    if not value.startswith(prefix):
        return None

    token = value[len(prefix):].strip()
    return token or None