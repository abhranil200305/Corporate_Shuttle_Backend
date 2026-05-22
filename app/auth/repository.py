# app/auth/repository.py
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.schema import (
    OTPPurpose,
    OTPRequest,
    PlatformSettings,
    User,
    UserRole,
    UserSession,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(email: str) -> str:
    return email.strip().lower()


class AuthRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ============================================================
    # users
    # ============================================================
 
    async def get_user_by_email_and_role(
        self,
        email: str,
        role: UserRole,
    ) -> User | None:
        normalized_email = normalize_email(email)
        stmt = select(User).where(
            User.email == normalized_email,
            User.role == role,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_user_by_id(self, user_id: str) -> User | None:
        stmt = select(User).where(User.id == user_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create_user(
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
        await self.db.flush()
        return user

    # ============================================================
    # otp
    # ============================================================

    async def create_otp_request(
        self,
        *,
        email: str,
        role: UserRole,
        otp_code_hash: str,
        purpose: OTPPurpose,
        expires_at: datetime,
    ) -> OTPRequest:
        otp_request = OTPRequest(
            email=normalize_email(email),
            role=role,
            otp_code_hash=otp_code_hash,
            purpose=purpose,
            expires_at=expires_at,
        )
        self.db.add(otp_request)
        await self.db.flush()
        return otp_request

    async def get_latest_otp_request(
        self,
        *,
        email: str,
        role: UserRole,
        purpose: OTPPurpose,
    ) -> OTPRequest | None:
        normalized_email = normalize_email(email)
        stmt = (
            select(OTPRequest)
            .where(
                OTPRequest.email == normalized_email,
                OTPRequest.role == role,
                OTPRequest.purpose == purpose,
            )
            .order_by(OTPRequest.created_at.desc())
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def get_latest_active_otp_request(
        self,
        *,
        email: str,
        role: UserRole,
        purpose: OTPPurpose,
        now: datetime | None = None,
    ) -> OTPRequest | None:
        current_time = now or utcnow()
        normalized_email = normalize_email(email)
        stmt = (
            select(OTPRequest)
            .where(
                OTPRequest.email == normalized_email,
                OTPRequest.role == role,
                OTPRequest.purpose == purpose,
                OTPRequest.used_at.is_(None),
                OTPRequest.expires_at > current_time,
            )
            .order_by(OTPRequest.created_at.desc())
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def count_active_otp_requests(
        self,
        *,
        email: str,
        role: UserRole,
        purpose: OTPPurpose,
        now: datetime | None = None,
    ) -> int:
        current_time = now or utcnow()
        normalized_email = normalize_email(email)
        stmt = (
            select(func.count(OTPRequest.id))
            .where(
                OTPRequest.email == normalized_email,
                OTPRequest.role == role,
                OTPRequest.purpose == purpose,
                OTPRequest.used_at.is_(None),
                OTPRequest.expires_at > current_time,
            )
        )
        result = await self.db.execute(stmt)
        return int(result.scalar_one())
        
    async def mark_otp_request_used(
        self,
        otp_request: OTPRequest,
        *,
        used_at: datetime | None = None,
    ) -> OTPRequest:
        otp_request.used_at = used_at or utcnow()
        self.db.add(otp_request)
        await self.db.flush()
        return otp_request

    # ============================================================
    # sessions
    # ============================================================

    async def create_user_session(
        self,
        *,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
        device_name: str | None = None,
        device_family: str | None = None,
        platform: str | None = None,
        browser: str | None = None,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> UserSession:
        session = UserSession(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            device_name=device_name,
            device_family=device_family,
            platform=platform,
            browser=browser,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        self.db.add(session)
        await self.db.flush()
        return session
    
    async def get_driver_max_active_sessions(self) -> int:
        stmt = (
            select(PlatformSettings)
            .where(PlatformSettings.settings_key == "default")
            .limit(1)
        )
        result = await self.db.execute(stmt)
        settings = result.scalar_one_or_none()

        if settings is None:
            return 2

        return max(1, int(settings.driver_max_active_sessions or 2))

    async def count_current_user_sessions(
        self,
        *,
        user_id: str,
        now: datetime | None = None,
    ) -> int:
        current_time = now or utcnow()
        stmt = (
            select(func.count(UserSession.id))
            .where(
                UserSession.user_id == user_id,
                UserSession.expires_at > current_time,
            )
        )
        result = await self.db.execute(stmt)
        return int(result.scalar_one())

    async def list_current_user_sessions_by_user_id(
        self,
        *,
        user_id: str,
        now: datetime | None = None,
    ) -> list[UserSession]:
        current_time = now or utcnow()
        stmt = (
            select(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.expires_at > current_time,
            )
            .order_by(
                UserSession.last_used_at.desc().nullslast(),
                UserSession.created_at.desc(),
            )
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def delete_expired_user_sessions_by_user_id(
        self,
        *,
        user_id: str,
        now: datetime | None = None,
    ) -> int:
        current_time = now or utcnow()
        stmt = delete(UserSession).where(
            UserSession.user_id == user_id,
            UserSession.expires_at <= current_time,
        )
        result = await self.db.execute(stmt)
        return int(result.rowcount or 0)

    async def delete_user_session_by_id_for_user(
        self,
        *,
        session_id: str,
        user_id: str,
    ) -> int:
        stmt = delete(UserSession).where(
            UserSession.id == session_id,
            UserSession.user_id == user_id,
        )
        result = await self.db.execute(stmt)
        return int(result.rowcount or 0)

    async def get_user_session_by_token_hash(
        self,
        token_hash: str,
    ) -> UserSession | None:
        stmt = select(UserSession).where(UserSession.token_hash == token_hash)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_user_session_by_token_hash(
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
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()
    
    async def token_has_active_user_session(
        self,
        token_hash: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        current_time = now or utcnow()
        stmt = (
            select(UserSession.id)
            .join(User, User.id == UserSession.user_id)
            .where(
                UserSession.token_hash == token_hash,
                UserSession.expires_at > current_time,
                User.is_active.is_(True),
            )
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def touch_user_session(
        self,
        session: UserSession,
        *,
        used_at: datetime | None = None,
    ) -> UserSession:
        session.last_used_at = used_at or utcnow()
        self.db.add(session)
        await self.db.flush()
        return session

    async def delete_user_session_by_token_hash(self, token_hash: str) -> int:
        stmt = delete(UserSession).where(UserSession.token_hash == token_hash)
        result = await self.db.execute(stmt)
        return int(result.rowcount or 0)

    async def delete_user_sessions_by_user_id(self, user_id: str) -> int:
        stmt = delete(UserSession).where(UserSession.user_id == user_id)
        result = await self.db.execute(stmt)
        return int(result.rowcount or 0)

    async def delete_expired_user_sessions(self, *, now: datetime | None = None) -> int:
        current_time = now or utcnow()
        stmt = delete(UserSession).where(UserSession.expires_at <= current_time)
        result = await self.db.execute(stmt)
        return int(result.rowcount or 0)