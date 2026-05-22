#app/auth/exceptions.py
from __future__ import annotations


class AuthError(Exception):
    default_message = "Authentication error."
    status_code = 400
    error_code = "auth_error"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class InvalidEmailError(AuthError):
    default_message = "Invalid email address."
    status_code = 400
    error_code = "invalid_email"


class SignupRoleNotAllowedError(AuthError):
    default_message = "This role is not allowed for self-signup."
    status_code = 403
    error_code = "signup_role_not_allowed"


class UserAlreadyExistsError(AuthError):
    default_message = "User already exists."
    status_code = 409
    error_code = "user_already_exists"


class UserNotFoundError(AuthError):
    default_message = "User not found."
    status_code = 404
    error_code = "user_not_found"


class UserInactiveError(AuthError):
    default_message = "User is inactive."
    status_code = 403
    error_code = "user_inactive"


class OTPNotFoundError(AuthError):
    default_message = "OTP request not found."
    status_code = 404
    error_code = "otp_not_found"


class OTPExpiredError(AuthError):
    default_message = "OTP has expired."
    status_code = 400
    error_code = "otp_expired"


class OTPAlreadyUsedError(AuthError):
    default_message = "OTP has already been used."
    status_code = 400
    error_code = "otp_already_used"


class InvalidOTPError(AuthError):
    default_message = "Invalid OTP."
    status_code = 400
    error_code = "invalid_otp"


class OTPResendTooSoonError(AuthError):
    default_message = "OTP was sent recently. Please wait before requesting another one."
    status_code = 429
    error_code = "otp_resend_too_soon"


class TooManyActiveOTPRequestsError(AuthError):
    default_message = "Too many active OTP requests. Please wait for existing ones to expire."
    status_code = 429
    error_code = "too_many_active_otp_requests"


class InvalidSessionError(AuthError):
    default_message = "Invalid session."
    status_code = 401
    error_code = "invalid_session"


class SessionExpiredError(AuthError):
    default_message = "Session has expired."
    status_code = 401
    error_code = "session_expired"

class DriverDeviceLimitReachedError(AuthError):
    default_message = "Driver device limit reached. Remove one active login and try again."
    status_code = 409
    error_code = "driver_device_limit_reached"

class DeviceSessionNotFoundError(AuthError):
    default_message = "Device login not found."
    status_code = 404
    error_code = "device_session_not_found"