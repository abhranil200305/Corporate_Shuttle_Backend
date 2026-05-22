#app/auth/schemas.py
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.db.schema import UserRole


class SendSignupOTPRequest(BaseModel):
    email: str = Field(..., max_length=255)
    role: UserRole = Field(..., description="Only driver or passenger is allowed for self-signup.")


class VerifySignupOTPRequest(BaseModel):
    email: str = Field(..., max_length=255)
    otp: str = Field(..., min_length=4, max_length=10)
    role: UserRole

class DeviceMetadataInput(BaseModel):
    device_name: str | None = Field(default=None, max_length=255)
    device_family: str | None = Field(default=None, max_length=120)
    platform: str | None = Field(default=None, max_length=120)
    browser: str | None = Field(default=None, max_length=120)

class SignupRequest(BaseModel):
    email: str = Field(..., max_length=255)
    otp: str = Field(..., min_length=4, max_length=10)
    role: UserRole
    device: DeviceMetadataInput | None = None


class SendLoginOTPRequest(BaseModel):
    email: str = Field(..., max_length=255)
    role: UserRole


class VerifyLoginOTPRequest(BaseModel):
    email: str = Field(..., max_length=255)
    otp: str = Field(..., min_length=4, max_length=10)
    role: UserRole


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=255)
    otp: str = Field(..., min_length=4, max_length=10)
    role: UserRole
    device: DeviceMetadataInput | None = None


class LogoutRequest(BaseModel):
    token: str = Field(..., min_length=1)


class AuthUserResponse(BaseModel):
    user_id: str
    email: str
    role: UserRole
    is_active: bool


class AuthTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: AuthUserResponse

class DeviceSessionResponse(BaseModel):
    session_id: str
    device_name: str | None = None
    device_family: str | None = None
    platform: str | None = None
    browser: str | None = None
    ip_address: str | None = None
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime
    logged_in_for_seconds: int
    is_current_session: bool


class DeviceSessionListResponse(BaseModel):
    active_login_count: int
    devices: list[DeviceSessionResponse]

class AdminDeviceUserSummaryResponse(BaseModel):
    user_id: str
    email: str
    role: UserRole
    is_active: bool
    name: str | None = None
    profile_picture_path: str | None = None
    active_login_count: int
    last_login_at: datetime | None = None
    last_used_at: datetime | None = None


class AdminDeviceUserPaginationResponse(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int
    has_next: bool
    has_previous: bool


class AdminDeviceUserListResponse(BaseModel):
    items: list[AdminDeviceUserSummaryResponse]
    pagination: AdminDeviceUserPaginationResponse


class AdminUserDeviceListResponse(BaseModel):
    user_id: str
    email: str
    role: UserRole
    is_active: bool
    name: str | None = None
    profile_picture_path: str | None = None
    active_login_count: int
    devices: list[DeviceSessionResponse]

class DriverDeviceSettingsResponse(BaseModel):
    driver_max_active_sessions: int


class DriverDeviceSettingsUpdateRequest(BaseModel):
    driver_max_active_sessions: int = Field(..., ge=1)

class OTPVerifyResponse(BaseModel):
    verified: bool
    message: str


class MessageResponse(BaseModel):
    message: str


class MailAttachmentSchema(BaseModel):
    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


class SendTemplatedMailRequest(BaseModel):
    to_email: str = Field(..., max_length=255)
    subject: str = Field(..., max_length=255)
    template_filename: str = Field(..., max_length=255)
    replacements: dict[str, Any] = Field(default_factory=dict)
    attachments: list[MailAttachmentSchema] = Field(default_factory=list)