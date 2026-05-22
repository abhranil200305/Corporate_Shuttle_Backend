# app/auth/service.py
from __future__ import annotations

import re
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.auth.constants import (
    APP_NAME,
    OTP_EXPIRY_MINUTES,
    OTP_MAX_ACTIVE_PER_EMAIL_PER_PURPOSE,
    SELF_SIGNUP_ALLOWED_ROLES,
)
from app.auth.exceptions import (
    DriverDeviceLimitReachedError,
    InvalidEmailError,
    InvalidOTPError,
    InvalidSessionError,
    OTPAlreadyUsedError,
    OTPExpiredError,
    OTPNotFoundError,
    OTPResendTooSoonError,
    SessionExpiredError,
    SignupRoleNotAllowedError,
    TooManyActiveOTPRequestsError,
    UserAlreadyExistsError,
    UserInactiveError,
    UserNotFoundError,
    DeviceSessionNotFoundError,
)
from app.auth.mailer import send_templated_mail
from app.auth.otp_utils import (
    can_resend_otp,
    generate_numeric_otp,
    get_otp_expiry,
    hash_otp,
    is_otp_expired,
    verify_otp,
)
from app.auth.repository import AuthRepository
from app.auth.schemas import (
    AuthTokenResponse,
    AuthUserResponse,
    LoginRequest,
    LogoutRequest,
    MessageResponse,
    OTPVerifyResponse,
    SendLoginOTPRequest,
    SendSignupOTPRequest,
    SignupRequest,
    VerifyLoginOTPRequest,
    VerifySignupOTPRequest,
    DeviceSessionListResponse,
    DeviceSessionResponse,
    AdminDeviceUserListResponse,
    AdminDeviceUserPaginationResponse,
    AdminDeviceUserSummaryResponse,
    AdminUserDeviceListResponse,
    DriverDeviceSettingsResponse,
    DriverDeviceSettingsUpdateRequest,
)
from app.auth.session_utils import (
    generate_session_token,
    get_session_expiry,
    hash_session_token,
    is_session_expired,
)
from app.db.schema import (
    OTPPurpose,
    OTPRequest,
    PlatformSettings,
    User,
    UserRole,
    UserSession,
)
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuthService:
    def __init__(self, db: AsyncSession) -> None:
        self.db: AsyncSession = db
        self.repo = AuthRepository(db)

    # -----------------------------
    # Helpers
    # -----------------------------
    @staticmethod
    def _normalize_email(email: str) -> str:
        return email.strip().lower()

    def _validate_email(self, email: str) -> str:
        normalized = self._normalize_email(email)
        if not normalized or not EMAIL_REGEX.match(normalized):
            raise InvalidEmailError()
        return normalized

    def _ensure_signup_role_allowed(self, role: UserRole) -> None:
        if role.value not in SELF_SIGNUP_ALLOWED_ROLES:
            raise SignupRoleNotAllowedError()

    @staticmethod
    def _build_auth_user_response(user: User) -> AuthUserResponse:
        return AuthUserResponse(
            user_id=user.id,
            email=user.email,
            role=user.role,
            is_active=user.is_active,
        )
    
    @staticmethod
    def _logged_in_for_seconds(
        *,
        created_at: datetime,
        now: datetime,
    ) -> int:
        return max(0, int((now - created_at).total_seconds()))

    @classmethod
    def _build_device_session_response(
        cls,
        session,
        *,
        now: datetime,
        current_token_hash: str,
    ) -> DeviceSessionResponse:
        return DeviceSessionResponse(
            session_id=session.id,
            device_name=session.device_name,
            device_family=session.device_family,
            platform=session.platform,
            browser=session.browser,
            ip_address=session.ip_address,
            created_at=session.created_at,
            last_used_at=session.last_used_at,
            expires_at=session.expires_at,
            logged_in_for_seconds=cls._logged_in_for_seconds(
                created_at=session.created_at,
                now=now,
            ),
            is_current_session=session.token_hash == current_token_hash,
        )
    
    @staticmethod
    def _profile_name_for_user(user: User) -> str | None:
        if user.role == UserRole.DRIVER and user.driver_profile is not None:
            return user.driver_profile.full_name

        if user.role == UserRole.PASSENGER and user.passenger_profile is not None:
            return user.passenger_profile.full_name

        return None
    
    @staticmethod
    def _profile_picture_path_for_user(user: User) -> str | None:
        if user.role == UserRole.DRIVER and user.driver_profile is not None:
            return user.driver_profile.profile_picture_path

        if user.role == UserRole.PASSENGER and user.passenger_profile is not None:
            return user.passenger_profile.profile_picture_path

        return None

    async def _issue_session_for_user(
        self,
        user: User,
        *,
        device_name: str | None = None,
        device_family: str | None = None,
        platform: str | None = None,
        browser: str | None = None,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> AuthTokenResponse:
        now = utcnow()

        await self.repo.delete_expired_user_sessions_by_user_id(
            user_id=user.id,
            now=now,
        )

        if user.role == UserRole.DRIVER:
            max_sessions = await self.repo.get_driver_max_active_sessions()
            active_sessions = await self.repo.count_current_user_sessions(
                user_id=user.id,
                now=now,
            )

            if active_sessions >= max_sessions:
                raise DriverDeviceLimitReachedError(
                    f"Driver device limit reached. Maximum active logins allowed: {max_sessions}."
                )

        raw_token = generate_session_token()
        token_hash = hash_session_token(raw_token)
        expires_at = get_session_expiry()

        await self.repo.create_user_session(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=expires_at,
            device_name=device_name,
            device_family=device_family,
            platform=platform,
            browser=browser,
            user_agent=user_agent,
            ip_address=ip_address,
        )

        return AuthTokenResponse(
            access_token=raw_token,
            expires_at=expires_at,
            user=self._build_auth_user_response(user),
        )

    async def _get_latest_active_otp_or_raise(
        self,
        *,
        email: str,
        role: UserRole,
        purpose: OTPPurpose,
    ) -> OTPRequest:
        otp_request = await self.repo.get_latest_active_otp_request(
            email=email,
            role=role,
            purpose=purpose,
            now=utcnow(),
        )
        if otp_request is None:
            raise OTPNotFoundError()
        if otp_request.used_at is not None:
            raise OTPAlreadyUsedError()
        if is_otp_expired(otp_request.expires_at):
            raise OTPExpiredError()
        return otp_request

    async def _validate_otp_or_raise(
        self,
        *,
        email: str,
        role: UserRole,
        otp: str,
        purpose: OTPPurpose,
    ) -> OTPRequest:
        otp_request = await self._get_latest_active_otp_or_raise(
            email=email,
            role=role,
            purpose=purpose,
        )
        if not verify_otp(
            otp,
            otp_request.otp_code_hash,
            email=email,
            role=role,
            purpose=purpose,
        ):
            raise InvalidOTPError()
        return otp_request

    async def _check_send_otp_preconditions(
        self,
        *,
        email: str,
        role: UserRole,
        purpose: OTPPurpose,
    ) -> None:
        latest_request = await self.repo.get_latest_otp_request(
            email=email,
            role=role,
            purpose=purpose,
        )
        if latest_request and not can_resend_otp(created_at=latest_request.created_at):
            raise OTPResendTooSoonError()

        active_count = await self.repo.count_active_otp_requests(
            email=email,
            role=role,
            purpose=purpose,
            now=utcnow(),
        )
        if active_count >= OTP_MAX_ACTIVE_PER_EMAIL_PER_PURPOSE:
            raise TooManyActiveOTPRequestsError()

    # -----------------------------
    # Signup
    # -----------------------------
    async def send_signup_otp(self, payload: SendSignupOTPRequest) -> MessageResponse:
        email = self._validate_email(payload.email)
        self._ensure_signup_role_allowed(payload.role)

        existing_user = await self.repo.get_user_by_email_and_role(email, payload.role)
        if existing_user is not None:
            raise UserAlreadyExistsError()

        await self._check_send_otp_preconditions(
            email=email,
            role=payload.role,
            purpose=OTPPurpose.SIGNUP,
        )

        otp = generate_numeric_otp()
        otp_hash = hash_otp(
            otp,
            email=email,
            role=payload.role,
            purpose=OTPPurpose.SIGNUP,
        )
        expires_at = get_otp_expiry()

        await self.repo.create_otp_request(
            email=email,
            role=payload.role,
            otp_code_hash=otp_hash,
            purpose=OTPPurpose.SIGNUP,
            expires_at=expires_at,
        )

        send_templated_mail(
            to_email=email,
            template_filename="otp_signup.html",
            replacements={
                "app_name": APP_NAME,
                "otp": otp,
                "expiry_minutes": OTP_EXPIRY_MINUTES,
                "user_email": email,
                "role": payload.role.value,
            },
        )

        await self.db.commit()
        return MessageResponse(message="Signup OTP sent successfully.")

    async def verify_signup_otp(self, payload: VerifySignupOTPRequest) -> OTPVerifyResponse:
        email = self._validate_email(payload.email)
        self._ensure_signup_role_allowed(payload.role)

        existing_user = await self.repo.get_user_by_email_and_role(email, payload.role)
        if existing_user is not None:
            raise UserAlreadyExistsError()

        await self._validate_otp_or_raise(
            email=email,
            role=payload.role,
            otp=payload.otp,
            purpose=OTPPurpose.SIGNUP,
        )
        return OTPVerifyResponse(verified=True, message="Signup OTP verified successfully.")

    async def signup(
        self,
        payload: SignupRequest,
        *,
        device_name: str | None = None,
        device_family: str | None = None,
        platform: str | None = None,
        browser: str | None = None,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> AuthTokenResponse:
        email = self._validate_email(payload.email)
        self._ensure_signup_role_allowed(payload.role)

        existing_user = await self.repo.get_user_by_email_and_role(email, payload.role)
        if existing_user is not None:
            raise UserAlreadyExistsError()

        otp_request = await self._validate_otp_or_raise(
            email=email,
            role=payload.role,
            otp=payload.otp,
            purpose=OTPPurpose.SIGNUP,
        )
        user = await self.repo.create_user(email=email, role=payload.role, is_active=True)
        await self.repo.mark_otp_request_used(otp_request)

        auth_response = await self._issue_session_for_user(
            user,
            device_name=device_name,
            device_family=device_family,
            platform=platform,
            browser=browser,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        await self.db.commit()
        return auth_response

    # -----------------------------
    # Login
    # -----------------------------
    async def send_login_otp(self, payload: SendLoginOTPRequest) -> MessageResponse:
        email = self._validate_email(payload.email)
        user = await self.repo.get_user_by_email_and_role(email, payload.role)
        if user is None:
            raise UserNotFoundError()
        if not user.is_active:
            raise UserInactiveError()

        await self._check_send_otp_preconditions(
            email=email,
            role=payload.role,
            purpose=OTPPurpose.LOGIN,
        )

        otp = generate_numeric_otp()
        otp_hash = hash_otp(
            otp,
            email=email,
            role=payload.role,
            purpose=OTPPurpose.LOGIN,
        )
        expires_at = get_otp_expiry()

        await self.repo.create_otp_request(
            email=email,
            role=payload.role,
            otp_code_hash=otp_hash,
            purpose=OTPPurpose.LOGIN,
            expires_at=expires_at,
        )

        send_templated_mail(
            to_email=email,
            template_filename="otp_login.html",
            replacements={
                "app_name": APP_NAME,
                "otp": otp,
                "expiry_minutes": OTP_EXPIRY_MINUTES,
                "user_email": email,
                "role": payload.role.value,
            },
        )
        await self.db.commit()
        return MessageResponse(message="Login OTP sent successfully.")

    async def verify_login_otp(self, payload: VerifyLoginOTPRequest) -> OTPVerifyResponse:
        email = self._validate_email(payload.email)
        user = await self.repo.get_user_by_email_and_role(email, payload.role)
        if user is None:
            raise UserNotFoundError()
        if not user.is_active:
            raise UserInactiveError()

        await self._validate_otp_or_raise(
            email=email,
            role=payload.role,
            otp=payload.otp,
            purpose=OTPPurpose.LOGIN,
        )
        return OTPVerifyResponse(verified=True, message="Login OTP verified successfully.")

    async def login(
        self,
        payload: LoginRequest,
        *,
        device_name: str | None = None,
        device_family: str | None = None,
        platform: str | None = None,
        browser: str | None = None,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> AuthTokenResponse:
        email = self._validate_email(payload.email)
        user = await self.repo.get_user_by_email_and_role(email, payload.role)
        if user is None:
            raise UserNotFoundError()
        if not user.is_active:
            raise UserInactiveError()

        otp_request = await self._validate_otp_or_raise(
            email=email,
            role=payload.role,
            otp=payload.otp,
            purpose=OTPPurpose.LOGIN,
        )
        await self.repo.mark_otp_request_used(otp_request)

        auth_response = await self._issue_session_for_user(
            user,
            device_name=device_name,
            device_family=device_family,
            platform=platform,
            browser=browser,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        await self.db.commit()
        return auth_response

    # -----------------------------
    # Session
    # -----------------------------

    async def list_current_user_devices(
        self,
        *,
        user: User,
        current_token: str,
    ) -> DeviceSessionListResponse:
        if not current_token or not current_token.strip():
            raise InvalidSessionError()

        now = utcnow()
        current_token_hash = hash_session_token(current_token.strip())

        await self.repo.delete_expired_user_sessions_by_user_id(
            user_id=user.id,
            now=now,
        )

        sessions = await self.repo.list_current_user_sessions_by_user_id(
            user_id=user.id,
            now=now,
        )

        await self.db.commit()

        devices = [
            self._build_device_session_response(
                session,
                now=now,
                current_token_hash=current_token_hash,
            )
            for session in sessions
        ]

        return DeviceSessionListResponse(
            active_login_count=len(devices),
            devices=devices,
        )

    async def remove_current_user_device(
        self,
        *,
        user: User,
        session_id: str,
    ) -> MessageResponse:
        deleted = await self.repo.delete_user_session_by_id_for_user(
            session_id=session_id,
            user_id=user.id,
        )

        if deleted <= 0:
            raise DeviceSessionNotFoundError()

        await self.db.commit()
        return MessageResponse(message="Device login removed successfully.")
    
    async def list_admin_device_users(
        self,
        *,
        role: UserRole | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> AdminDeviceUserListResponse:
        now = utcnow()
        safe_page = max(1, page)
        safe_page_size = min(max(1, page_size), 100)
        offset = (safe_page - 1) * safe_page_size

        user_filters = []
        if role is not None:
            user_filters.append(User.role == role)

        active_session_filter = UserSession.expires_at > now

        total_stmt = select(func.count(User.id)).where(*user_filters)
        total_result = await self.db.execute(total_stmt)
        total = int(total_result.scalar_one())

        stmt = (
            select(
                User,
                func.count(UserSession.id).label("active_login_count"),
                func.max(UserSession.created_at).label("last_login_at"),
                func.max(UserSession.last_used_at).label("last_used_at"),
            )
            .outerjoin(
                UserSession,
                (UserSession.user_id == User.id) & active_session_filter,
            )
            .options(
                selectinload(User.driver_profile),
                selectinload(User.passenger_profile),
            )
            .where(*user_filters)
            .group_by(User.id)
            .order_by(
                func.max(UserSession.last_used_at).desc().nullslast(),
                func.max(UserSession.created_at).desc().nullslast(),
                User.created_at.desc(),
            )
            .offset(offset)
            .limit(safe_page_size)
        )

        result = await self.db.execute(stmt)
        rows = result.all()

        total_pages = (total + safe_page_size - 1) // safe_page_size if total else 0

        return AdminDeviceUserListResponse(
            items=[
                AdminDeviceUserSummaryResponse(
                    user_id=user.id,
                    email=user.email,
                    role=user.role,
                    is_active=user.is_active,
                    name=self._profile_name_for_user(user),
                    profile_picture_path=self._profile_picture_path_for_user(user),
                    active_login_count=int(active_login_count or 0),
                    last_login_at=last_login_at,
                    last_used_at=last_used_at,
                )
                for user, active_login_count, last_login_at, last_used_at in rows
            ],
            pagination=AdminDeviceUserPaginationResponse(
                page=safe_page,
                page_size=safe_page_size,
                total=total,
                total_pages=total_pages,
                has_next=safe_page < total_pages,
                has_previous=safe_page > 1,
            ),
        )

    async def list_admin_user_devices(
        self,
        *,
        user_id: str,
    ) -> AdminUserDeviceListResponse:
        now = utcnow()

        stmt = (
            select(User)
            .where(User.id == user_id)
            .options(
                selectinload(User.driver_profile),
                selectinload(User.passenger_profile),
            )
        )
        result = await self.db.execute(stmt)
        user = result.scalar_one_or_none()
        if user is None:
            raise UserNotFoundError()

        await self.repo.delete_expired_user_sessions_by_user_id(
            user_id=user.id,
            now=now,
        )

        sessions = await self.repo.list_current_user_sessions_by_user_id(
            user_id=user.id,
            now=now,
        )

        await self.db.commit()

        return AdminUserDeviceListResponse(
            user_id=user.id,
            email=user.email,
            role=user.role,
            is_active=user.is_active,
            name=self._profile_name_for_user(user),
            profile_picture_path=self._profile_picture_path_for_user(user),
            active_login_count=len(sessions),
            devices=[
                DeviceSessionResponse(
                    session_id=session.id,
                    device_name=session.device_name,
                    device_family=session.device_family,
                    platform=session.platform,
                    browser=session.browser,
                    ip_address=session.ip_address,
                    created_at=session.created_at,
                    last_used_at=session.last_used_at,
                    expires_at=session.expires_at,
                    logged_in_for_seconds=self._logged_in_for_seconds(
                        created_at=session.created_at,
                        now=now,
                    ),
                    is_current_session=False,
                )
                for session in sessions
            ],
        )

    async def remove_admin_user_device(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> MessageResponse:
        user = await self.repo.get_user_by_id(user_id)
        if user is None:
            raise UserNotFoundError()

        deleted = await self.repo.delete_user_session_by_id_for_user(
            session_id=session_id,
            user_id=user_id,
        )

        if deleted <= 0:
            raise DeviceSessionNotFoundError()

        await self.db.commit()
        return MessageResponse(message="Device login removed successfully.")

    async def remove_all_admin_user_devices(
        self,
        *,
        user_id: str,
    ) -> MessageResponse:
        user = await self.repo.get_user_by_id(user_id)
        if user is None:
            raise UserNotFoundError()

        deleted = await self.repo.delete_user_sessions_by_user_id(user_id)

        await self.db.commit()
        return MessageResponse(
            message=f"Removed {deleted} active device login(s)."
        )

    async def _get_or_create_default_platform_settings(self) -> PlatformSettings:
        stmt = (
            select(PlatformSettings)
            .where(PlatformSettings.settings_key == "default")
            .limit(1)
        )
        result = await self.db.execute(stmt)
        settings = result.scalar_one_or_none()

        if settings is not None:
            return settings

        settings = PlatformSettings(
            settings_key="default",
            commission_percent=0,
            driver_max_active_sessions=2,
        )
        self.db.add(settings)
        await self.db.flush()
        return settings

    async def get_driver_device_settings(self) -> DriverDeviceSettingsResponse:
        settings = await self._get_or_create_default_platform_settings()
        await self.db.commit()

        return DriverDeviceSettingsResponse(
            driver_max_active_sessions=max(
                1,
                int(settings.driver_max_active_sessions or 2),
            ),
        )

    async def update_driver_device_settings(
        self,
        payload: DriverDeviceSettingsUpdateRequest,
    ) -> DriverDeviceSettingsResponse:
        settings = await self._get_or_create_default_platform_settings()
        settings.driver_max_active_sessions = payload.driver_max_active_sessions

        self.db.add(settings)
        await self.db.commit()
        await self.db.refresh(settings)

        return DriverDeviceSettingsResponse(
            driver_max_active_sessions=settings.driver_max_active_sessions,
        )

    async def is_token_fresh(self, token: str) -> bool:
        raw_token = token.strip() if token else ""
        if not raw_token:
            return False

        token_hash = hash_session_token(raw_token)
        return await self.repo.token_has_active_user_session(
            token_hash,
            now=utcnow(),
        )
    
    async def authenticate_token(self, token: str) -> User:
        if not token or not token.strip():
            raise InvalidSessionError()

        token_hash = hash_session_token(token.strip())
        session = await self.repo.get_user_session_by_token_hash(token_hash)
        if session is None:
            raise InvalidSessionError()
        if is_session_expired(session.expires_at):
            raise SessionExpiredError()

        user = await self.repo.get_user_by_id(session.user_id)
        if user is None:
            raise UserNotFoundError()
        if not user.is_active:
            raise UserInactiveError()

        await self.repo.touch_user_session(session)
        await self.db.commit()
        return user

    async def logout(self, token: str) -> MessageResponse:
        if not token or not token.strip():
            raise InvalidSessionError()

        token_hash = hash_session_token(token.strip())
        deleted = await self.repo.delete_user_session_by_token_hash(token_hash)
        if deleted <= 0:
            raise InvalidSessionError()

        await self.db.commit()
        return MessageResponse(message="Logged out successfully.")