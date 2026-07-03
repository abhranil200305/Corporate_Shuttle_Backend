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
    DeviceSessionListResponse,
)
from app.auth.service import AuthService
from app.auth.session_utils import extract_bearer_token
from app.realtime.events import get_api_refresh_hub, publish_admin_event
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

def _clean_optional_header_text(value: str | None, max_length: int) -> str | None:
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    return cleaned[:max_length]

def _client_ip_from_request(request: Request) -> str | None:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        first_ip = forwarded_for.split(",", 1)[0].strip()
        if first_ip:
            return first_ip[:64]

    real_ip = request.headers.get("x-real-ip")
    if real_ip and real_ip.strip():
        return real_ip.strip()[:64]

    if request.client is None:
        return None

    return request.client.host[:64]


def _device_metadata_from_request(
    request: Request,
    device,
) -> dict[str, str | None]:
    user_agent = (request.headers.get("user-agent") or "").strip()

    fallback_device_name = user_agent[:255] if user_agent else None

    return {
        "device_name": _clean_optional_header_text(
            None if device is None else device.device_name,
            255,
        )
        or fallback_device_name,
        "device_family": _clean_optional_header_text(
            None if device is None else device.device_family,
            120,
        ),
        "platform": _clean_optional_header_text(
            None if device is None else device.platform,
            120,
        ),
        "browser": _clean_optional_header_text(
            None if device is None else device.browser,
            120,
        ),
        "user_agent": user_agent or None,
        "ip_address": _client_ip_from_request(request),
    }

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
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthTokenResponse:
    try:
        result = await auth_service.signup(
            payload,
            **_device_metadata_from_request(request, payload.device),
        )
        await publish_admin_event(
            get_api_refresh_hub(request.app),
            event="admin.users_changed",
            data={
                "user_id": result.user.user_id,
                "role": result.user.role.value,
                "reason": "user_signed_up",
            },
        )
        return result
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
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthTokenResponse:
    try:
        result = await auth_service.login(
            payload,
            **_device_metadata_from_request(request, payload.device),
        )
        await publish_admin_event(
            get_api_refresh_hub(request.app),
            event="admin.users_changed",
            data={
                "user_id": result.user.user_id,
                "role": result.user.role.value,
                "reason": "user_logged_in",
            },
        )
        return result
    except AuthError as exc:
        raise to_http_exception(exc) from exc


# -----------------------------
# Logout Endpoint
# -----------------------------
@router.post("/logout", response_model=MessageResponse)
async def logout(
    request: Request,
    token: str = Depends(get_bearer_token_from_request),
    current_user: User = Depends(get_current_active_user),
    auth_service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
    try:
        result = await auth_service.logout(token)
        await publish_admin_event(
            get_api_refresh_hub(request.app),
            event="admin.users_changed",
            data={
                "user_id": current_user.id,
                "role": current_user.role.value,
                "reason": "user_logged_out",
            },
        )
        return result
    except AuthError as exc:
        raise to_http_exception(exc) from exc
    
# -----------------------------
# Current User Devices
# -----------------------------
@router.get("/devices", response_model=DeviceSessionListResponse)
async def list_my_devices(
    token: str = Depends(get_bearer_token_from_request),
    current_user: User = Depends(get_current_active_user),
    auth_service: AuthService = Depends(get_auth_service),
) -> DeviceSessionListResponse:
    try:
        return await auth_service.list_current_user_devices(
            user=current_user,
            current_token=token,
        )
    except AuthError as exc:
        raise to_http_exception(exc) from exc


@router.delete("/devices/{session_id}", response_model=MessageResponse)
async def remove_my_device(
    session_id: str,
    request: Request,
    current_user: User = Depends(get_current_active_user),
    auth_service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
    try:
        result = await auth_service.remove_current_user_device(
            user=current_user,
            session_id=session_id,
        )
        await publish_admin_event(
            get_api_refresh_hub(request.app),
            event="admin.users_changed",
            data={
                "user_id": current_user.id,
                "role": current_user.role.value,
                "session_id": session_id,
                "reason": "user_device_removed",
            },
        )
        return result
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
