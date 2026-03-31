from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.schema import OTPPurpose, OTPRequest, User, UserRole, UserSession


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(email: str) -> str:
    return email.strip().lower()


class AuthRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ============================================================
    # users
    # ============================================================

    def get_user_by_email(self, email: str) -> User | None:
        normalized_email = normalize_email(email)
        stmt = select(User).where(User.email == normalized_email)
        return self.db.execute(stmt).scalar_one_or_none()

    def get_user_by_id(self, user_id: str) -> User | None:
        stmt = select(User).where(User.id == user_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def create_user(
        self,
        *,
        email: str,
        role: UserRole,
        is_active: bool = True,
    ) -> User:
        user = User(
            email=normalize_email(email),
            role=role,
            is_active=is_active,
        )
        self.db.add(user)
        self.db.flush()
        return user

    # ============================================================
    # otp
    # ============================================================

    def create_otp_request(
        self,
        *,
        email: str,
        otp_code_hash: str,
        purpose: OTPPurpose,
        expires_at: datetime,
    ) -> OTPRequest:
        otp_request = OTPRequest(
            email=normalize_email(email),
            otp_code_hash=otp_code_hash,
            purpose=purpose,
            expires_at=expires_at,
        )
        self.db.add(otp_request)
        self.db.flush()
        return otp_request

    def get_latest_otp_request(
        self,
        *,
        email: str,
        purpose: OTPPurpose,
    ) -> OTPRequest | None:
        normalized_email = normalize_email(email)
        stmt = (
            select(OTPRequest)
            .where(
                OTPRequest.email == normalized_email,
                OTPRequest.purpose == purpose,
            )
            .order_by(OTPRequest.created_at.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()

    def get_latest_active_otp_request(
        self,
        *,
        email: str,
        purpose: OTPPurpose,
        now: datetime | None = None,
    ) -> OTPRequest | None:
        current_time = now or utcnow()
        normalized_email = normalize_email(email)

        stmt = (
            select(OTPRequest)
            .where(
                OTPRequest.email == normalized_email,
                OTPRequest.purpose == purpose,
                OTPRequest.used_at.is_(None),
                OTPRequest.expires_at > current_time,
            )
            .order_by(OTPRequest.created_at.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()

    def count_active_otp_requests(
        self,
        *,
        email: str,
        purpose: OTPPurpose,
        now: datetime | None = None,
    ) -> int:
        current_time = now or utcnow()
        normalized_email = normalize_email(email)

        stmt = (
            select(func.count(OTPRequest.id))
            .where(
                OTPRequest.email == normalized_email,
                OTPRequest.purpose == purpose,
                OTPRequest.used_at.is_(None),
                OTPRequest.expires_at > current_time,
            )
        )
        return int(self.db.execute(stmt).scalar_one())

    def mark_otp_request_used(
        self,
        otp_request: OTPRequest,
        *,
        used_at: datetime | None = None,
    ) -> OTPRequest:
        otp_request.used_at = used_at or utcnow()
        self.db.add(otp_request)
        self.db.flush()
        return otp_request

    # ============================================================
    # sessions
    # ============================================================

    def create_user_session(
        self,
        *,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
    ) -> UserSession:
        session = UserSession(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        self.db.add(session)
        self.db.flush()
        return session

    def get_user_session_by_token_hash(
        self,
        token_hash: str,
    ) -> UserSession | None:
        stmt = select(UserSession).where(UserSession.token_hash == token_hash)
        return self.db.execute(stmt).scalar_one_or_none()

    def get_active_user_session_by_token_hash(
        self,
        token_hash: str,
        *,
        now: datetime | None = None,
    ) -> UserSession | None:
        current_time = now or utcnow()
        stmt = (
            select(UserSession)
            .where(
                UserSession.token_hash == token_hash,
                UserSession.expires_at > current_time,
            )
            .limit(1)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def touch_user_session(
        self,
        session: UserSession,
        *,
        used_at: datetime | None = None,
    ) -> UserSession:
        session.last_used_at = used_at or utcnow()
        self.db.add(session)
        self.db.flush()
        return session

    def delete_user_session_by_token_hash(self, token_hash: str) -> int:
        stmt = delete(UserSession).where(UserSession.token_hash == token_hash)
        result = self.db.execute(stmt)
        return int(result.rowcount or 0)

    def delete_user_sessions_by_user_id(self, user_id: str) -> int:
        stmt = delete(UserSession).where(UserSession.user_id == user_id)
        result = self.db.execute(stmt)
        return int(result.rowcount or 0)

    def delete_expired_user_sessions(self, *, now: datetime | None = None) -> int:
        current_time = now or utcnow()
        stmt = delete(UserSession).where(UserSession.expires_at <= current_time)
        result = self.db.execute(stmt)
        return int(result.rowcount or 0)