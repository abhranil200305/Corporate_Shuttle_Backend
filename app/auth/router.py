# app/auth/router.py
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
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
from app.auth.session_utils import extract_bearer_token
from app.db.schema import User

router = APIRouter(prefix="/auth", tags=["auth"])
def _empty_auth_probe_response(status_code: int) -> Response:
    return Response(
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        },
    )

# -----------------------------
# Signup Endpoints
# -----------------------------
@router.post("/signup/send-otp", response_model=MessageResponse)
async def send_signup_otp(
    payload: SendSignupOTPRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
    try:
        return await auth_service.send_signup_otp(payload)
    except AuthError as exc:
        raise to_http_exception(exc) from exc


@router.post("/signup/verify-otp", response_model=OTPVerifyResponse)
async def verify_signup_otp(
    payload: VerifySignupOTPRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> OTPVerifyResponse:
    try:
        return await auth_service.verify_signup_otp(payload)
    except AuthError as exc:
        raise to_http_exception(exc) from exc


@router.post("/signup", response_model=AuthTokenResponse)
async def signup(
    payload: SignupRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthTokenResponse:
    try:
        return await auth_service.signup(payload)
    except AuthError as exc:
        raise to_http_exception(exc) from exc


# -----------------------------
# Login Endpoints
# -----------------------------
@router.post("/login/send-otp", response_model=MessageResponse)
async def send_login_otp(
    payload: SendLoginOTPRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
    try:
        return await auth_service.send_login_otp(payload)
    except AuthError as exc:
        raise to_http_exception(exc) from exc


@router.post("/login/verify-otp", response_model=OTPVerifyResponse)
async def verify_login_otp(
    payload: VerifyLoginOTPRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> OTPVerifyResponse:
    try:
        return await auth_service.verify_login_otp(payload)
    except AuthError as exc:
        raise to_http_exception(exc) from exc


@router.post("/login", response_model=AuthTokenResponse)
async def login(
    payload: LoginRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthTokenResponse:
    try:
        return await auth_service.login(payload)
    except AuthError as exc:
        raise to_http_exception(exc) from exc


# -----------------------------
# Logout Endpoint
# -----------------------------
@router.post("/logout", response_model=MessageResponse)
async def logout(
    token: str = Depends(get_bearer_token_from_request),
    auth_service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
    try:
        return await auth_service.logout(token)
    except AuthError as exc:
        raise to_http_exception(exc) from exc
    

# -----------------------------
# Token Freshness Endpoint
# -----------------------------
@router.get("/session/freshness", status_code=status.HTTP_204_NO_CONTENT)
async def session_freshness(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
) -> Response:
    token = extract_bearer_token(request.headers.get("Authorization"))
    if not token:
        return _empty_auth_probe_response(status.HTTP_401_UNAUTHORIZED)

    if not await auth_service.is_token_fresh(token):
        return _empty_auth_probe_response(status.HTTP_401_UNAUTHORIZED)

    return _empty_auth_probe_response(status.HTTP_204_NO_CONTENT)


# -----------------------------
# Current User Endpoint
# -----------------------------
@router.get("/me", response_model=AuthUserResponse)
async def me(
    current_user: User = Depends(get_current_active_user),
) -> AuthUserResponse:
    """
    Returns the current authenticated user's information.
    """
    return AuthUserResponse(
        user_id=current_user.id,
        email=current_user.email,
        role=current_user.role,
        is_active=current_user.is_active,
    )