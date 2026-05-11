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


class SignupRequest(BaseModel):
    email: str = Field(..., max_length=255)
    otp: str = Field(..., min_length=4, max_length=10)
    role: UserRole


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