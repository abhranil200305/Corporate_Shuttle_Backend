# app/auth/service.py
from __future__ import annotations

import re
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth.constants import (
    APP_NAME,
    OTP_EXPIRY_MINUTES,
    OTP_MAX_ACTIVE_PER_EMAIL_PER_PURPOSE,
    SELF_SIGNUP_ALLOWED_ROLES,
)
from app.auth.exceptions import (
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
)
from app.auth.session_utils import (
    generate_session_token,
    get_session_expiry,
    hash_session_token,
    is_session_expired,
)
from app.db.schema import OTPPurpose, OTPRequest, User, UserRole

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

    async def _issue_session_for_user(self, user: User) -> AuthTokenResponse:
        raw_token = generate_session_token()
        token_hash = hash_session_token(raw_token)
        expires_at = get_session_expiry()

        await self.repo.create_user_session(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=expires_at,
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
        purpose: OTPPurpose,
    ) -> OTPRequest:
        otp_request = await self.repo.get_latest_active_otp_request(
            email=email,
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
        otp: str,
        purpose: OTPPurpose,
    ) -> OTPRequest:
        otp_request = await self._get_latest_active_otp_or_raise(
            email=email,
            purpose=purpose,
        )
        if not verify_otp(otp, otp_request.otp_code_hash, email=email, purpose=purpose):
            raise InvalidOTPError()
        return otp_request

    async def _check_send_otp_preconditions(
        self,
        *,
        email: str,
        purpose: OTPPurpose,
    ) -> None:
        latest_request = await self.repo.get_latest_otp_request(
            email=email,
            purpose=purpose,
        )
        if latest_request and not can_resend_otp(created_at=latest_request.created_at):
            raise OTPResendTooSoonError()

        active_count = await self.repo.count_active_otp_requests(
            email=email,
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

        existing_user = await self.repo.get_user_by_email(email)
        if existing_user is not None:
            raise UserAlreadyExistsError()

        await self._check_send_otp_preconditions(email=email, purpose=OTPPurpose.SIGNUP)

        otp = generate_numeric_otp()
        otp_hash = hash_otp(otp, email=email, purpose=OTPPurpose.SIGNUP)
        expires_at = get_otp_expiry()

        await self.repo.create_otp_request(
            email=email,
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

        existing_user = await self.repo.get_user_by_email(email)
        if existing_user is not None:
            raise UserAlreadyExistsError()

        await self._validate_otp_or_raise(email=email, otp=payload.otp, purpose=OTPPurpose.SIGNUP)
        return OTPVerifyResponse(verified=True, message="Signup OTP verified successfully.")

    async def signup(self, payload: SignupRequest) -> AuthTokenResponse:
        email = self._validate_email(payload.email)
        self._ensure_signup_role_allowed(payload.role)

        existing_user = await self.repo.get_user_by_email_and_role(email, payload.role)
        if existing_user is not None:
            raise UserAlreadyExistsError()

        otp_request = await self._validate_otp_or_raise(email=email, otp=payload.otp, purpose=OTPPurpose.SIGNUP)
        user = await self.repo.create_user(email=email, role=payload.role, is_active=True)
        await self.repo.mark_otp_request_used(otp_request)

        auth_response = await self._issue_session_for_user(user)
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

        await self._check_send_otp_preconditions(email=email, purpose=OTPPurpose.LOGIN)

        otp = generate_numeric_otp()
        otp_hash = hash_otp(otp, email=email, purpose=OTPPurpose.LOGIN)
        expires_at = get_otp_expiry()

        await self.repo.create_otp_request(email=email, otp_code_hash=otp_hash, purpose=OTPPurpose.LOGIN, expires_at=expires_at)

        send_templated_mail(
            to_email=email,
            template_filename="otp_login.html",
            replacements={
                "app_name": APP_NAME,
                "otp": otp,
                "expiry_minutes": OTP_EXPIRY_MINUTES,
                "user_email": email,
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

        await self._validate_otp_or_raise(email=email, otp=payload.otp, purpose=OTPPurpose.LOGIN)
        return OTPVerifyResponse(verified=True, message="Login OTP verified successfully.")

    async def login(self, payload: LoginRequest) -> AuthTokenResponse:
        email = self._validate_email(payload.email)
        user = await self.repo.get_user_by_email_and_role(email, payload.role)
        if user is None:
            raise UserNotFoundError()
        if not user.is_active:
            raise UserInactiveError()

        otp_request = await self._validate_otp_or_raise(email=email, otp=payload.otp, purpose=OTPPurpose.LOGIN)
        await self.repo.mark_otp_request_used(otp_request)

        auth_response = await self._issue_session_for_user(user)
        await self.db.commit()
        return auth_response

    # -----------------------------
    # Session
    # -----------------------------

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