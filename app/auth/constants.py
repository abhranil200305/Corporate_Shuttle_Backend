from __future__ import annotations

from pathlib import Path

APP_NAME = "Shuttle Infra"

AUTH_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = AUTH_DIR / "templates"

OTP_LENGTH = 6
OTP_EXPIRY_MINUTES = 10
OTP_RESEND_COOLDOWN_SECONDS = 60
OTP_MAX_ACTIVE_PER_EMAIL_PER_PURPOSE = 5

SESSION_EXPIRY_DAYS = 7
SESSION_TOKEN_BYTES = 32

SELF_SIGNUP_ALLOWED_ROLES = {
    "driver",
    "passenger",
}

MAIL_FROM_NAME = APP_NAME
MAIL_TEMPLATE_SUBJECTS = {
    "otp_signup.html": f"{APP_NAME} signup OTP",
    "otp_signup.txt": f"{APP_NAME} signup OTP",
    "otp_login.html": f"{APP_NAME} login OTP",
    "otp_login.txt": f"{APP_NAME} login OTP",
}

DEFAULT_ATTACHMENT_CONTENT_TYPE = "application/octet-stream"