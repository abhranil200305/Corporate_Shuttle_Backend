#app/auth/otp_utils.py
from __future__ import annotations

import hmac
import os
import secrets
import string
from datetime import datetime, timedelta, timezone

from app.auth.constants import (
    OTP_EXPIRY_MINUTES,
    OTP_LENGTH,
    OTP_RESEND_COOLDOWN_SECONDS,
)
from app.db.schema import OTPPurpose


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _get_otp_hash_secret() -> bytes:
    secret = os.getenv("OTP_HASH_SECRET", "").strip()
    if not secret:
        raise RuntimeError("OTP_HASH_SECRET is not set.")
    return secret.encode("utf-8")


def generate_numeric_otp(length: int = OTP_LENGTH) -> str:
    if length <= 0:
        raise ValueError("OTP length must be positive.")

    digits = string.digits
    return "".join(secrets.choice(digits) for _ in range(length))


def hash_otp(
    otp: str,
    *,
    email: str,
    purpose: OTPPurpose | str,
) -> str:
    normalized_email = _normalize_email(email)
    purpose_value = purpose.value if isinstance(purpose, OTPPurpose) else str(purpose)
    payload = f"{normalized_email}:{purpose_value}:{otp}".encode("utf-8")

    return hmac.new(
        _get_otp_hash_secret(),
        payload,
        digestmod="sha256",
    ).hexdigest()


def verify_otp(
    otp: str,
    stored_hash: str,
    *,
    email: str,
    purpose: OTPPurpose | str,
) -> bool:
    computed_hash = hash_otp(
        otp,
        email=email,
        purpose=purpose,
    )
    return hmac.compare_digest(computed_hash, stored_hash)


def get_otp_expiry(*, now: datetime | None = None) -> datetime:
    current_time = now or utcnow()
    return current_time + timedelta(minutes=OTP_EXPIRY_MINUTES)


def is_otp_expired(expires_at: datetime, *, now: datetime | None = None) -> bool:
    current_time = now or utcnow()
    return expires_at <= current_time


def get_otp_resend_available_at(
    *,
    created_at: datetime,
) -> datetime:
    return created_at + timedelta(seconds=OTP_RESEND_COOLDOWN_SECONDS)


def can_resend_otp(
    *,
    created_at: datetime,
    now: datetime | None = None,
) -> bool:
    current_time = now or utcnow()
    return current_time >= get_otp_resend_available_at(created_at=created_at)