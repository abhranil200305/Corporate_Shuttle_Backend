##app/auth/router.py
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.dependencies import (
    get_auth_service,
    get_bearer_token_from_request,
    get_current_active_user,
    to_http_exception,
)
from app.auth.exceptions import AuthError
from app.auth.schemas import (
    AuthTokenResponse,
    AuthUserResponse,
    LoginRequest,
    MessageResponse,
    SendLoginOTPRequest,
    SendSignupOTPRequest,
    SignupRequest,
    VerifyLoginOTPRequest,
    VerifySignupOTPRequest,
    OTPVerifyResponse,
)
from app.auth.service import AuthService
from app.db.schema import User

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup/send-otp", response_model=MessageResponse)
def send_signup_otp(
    payload: SendSignupOTPRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
    try:
        return auth_service.send_signup_otp(payload)
    except AuthError as exc:
        raise to_http_exception(exc) from exc


@router.post("/signup/verify-otp", response_model=OTPVerifyResponse)
def verify_signup_otp(
    payload: VerifySignupOTPRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> OTPVerifyResponse:
    try:
        return auth_service.verify_signup_otp(payload)
    except AuthError as exc:
        raise to_http_exception(exc) from exc


@router.post("/signup", response_model=AuthTokenResponse)
def signup(
    payload: SignupRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthTokenResponse:
    try:
        return auth_service.signup(payload)
    except AuthError as exc:
        raise to_http_exception(exc) from exc


@router.post("/login/send-otp", response_model=MessageResponse)
def send_login_otp(
    payload: SendLoginOTPRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
    try:
        return auth_service.send_login_otp(payload)
    except AuthError as exc:
        raise to_http_exception(exc) from exc


@router.post("/login/verify-otp", response_model=OTPVerifyResponse)
def verify_login_otp(
    payload: VerifyLoginOTPRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> OTPVerifyResponse:
    try:
        return auth_service.verify_login_otp(payload)
    except AuthError as exc:
        raise to_http_exception(exc) from exc


@router.post("/login", response_model=AuthTokenResponse)
def login(
    payload: LoginRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthTokenResponse:
    try:
        return auth_service.login(payload)
    except AuthError as exc:
        raise to_http_exception(exc) from exc


@router.post("/logout", response_model=MessageResponse)
def logout(
    token: str = Depends(get_bearer_token_from_request),
    auth_service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
    try:
        return auth_service.logout(token)
    except AuthError as exc:
        raise to_http_exception(exc) from exc


@router.get("/me", response_model=AuthUserResponse)
def me(
    current_user: User = Depends(get_current_active_user),
) -> AuthUserResponse:
    return AuthUserResponse(
        user_id=current_user.id,
        email=current_user.email,
        role=current_user.role,
        is_active=current_user.is_active,
    )