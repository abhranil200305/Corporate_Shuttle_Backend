# app/driver/scan_events/otp.py

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.db.database import get_async_session
from app.db.schema import User
from app.driver.scan_events.booking_credential_scan import (
    ensure_driver_owns_trip,
    execute_credential_scan,
    resolve_otp_bookings_for_update,
)

router = APIRouter(prefix="/driver/otp", tags=["Driver OTP"])


class OTPVerifyRequest(BaseModel):
    otp_code: str
    lat: float
    lng: float


# =========================================================
# OTP VERIFY
# =========================================================
@router.post("/{trip_id}/verify")
async def verify_otp_scan(
    trip_id: str,
    request: Request,
    data: OTPVerifyRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    await ensure_driver_owns_trip(
        db,
        trip_id=trip_id,
        current_user=current_user,
    )

    bookings = await resolve_otp_bookings_for_update(
        db,
        trip_id=trip_id,
        otp_code=data.otp_code,
    )

    return await execute_credential_scan(
        trip_id=trip_id,
        request=request,
        lat=data.lat,
        lng=data.lng,
        db=db,
        current_user=current_user,
        bookings=bookings,
        success_message="OTP verified successfully",
        driver_trip_validated=True,
    )
