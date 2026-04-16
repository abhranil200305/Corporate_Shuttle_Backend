# app/driver/scan_events/otp.py

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime, timezone
from decimal import Decimal
from math import radians, cos, sin, asin, sqrt

from pydantic import BaseModel

from app.db.database import get_async_session
from app.auth.dependencies import get_current_user
from app.db.schema import (
    TripBooking,
    ScheduledTrip,
    TripScanEvent,
    ScanType,
    BookingStatus,
    Stop,
    User,
)

router = APIRouter(prefix="/driver/otp", tags=["Driver OTP"])


# =========================
# REQUEST BODY
# =========================
class OTPVerifyRequest(BaseModel):
    otp_code: str
    lat: float
    lng: float


# =========================
# HAVERSINE
# =========================
def haversine(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1

    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(a))

    return 6371 * c * 1000


# =========================
# OTP VERIFY
# =========================
@router.post("/{trip_id}/verify")
async def verify_otp_scan(
    trip_id: str,
    data: OTPVerifyRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    # =========================================
    # 1. TRIP VALIDATION
    # =========================================
    trip = await db.get(ScheduledTrip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.driver_user_id != current_user.id:
        raise HTTPException(403, "Not your trip")

    # =========================================
    # 2. FIND BOOKING USING OTP
    # =========================================
    result = await db.execute(
        select(TripBooking).where(
            TripBooking.scheduled_trip_id == trip_id,
            TripBooking.otp == data.otp_code
        )
    )
    booking = result.scalars().first()

    if not booking:
        raise HTTPException(400, "Invalid OTP")

    # =========================================
    # 3. VALID BOOKING STATE
    # =========================================
    if booking.booking_status not in [
        BookingStatus.BOOKED,
        BookingStatus.BOARDED,
    ]:
        raise HTTPException(400, "Booking not valid for OTP scan")

    # =========================================
    # 4. DETECT TYPE
    # =========================================
    if booking.booking_status == BookingStatus.BOOKED:
        scan_type = ScanType.BOARD
        stop_id = booking.pickup_stop_id

    else:  # BOARDED
        scan_type = ScanType.DROP
        stop_id = booking.dropoff_stop_id

    # =========================================
    # 5. STOP VALIDATION
    # =========================================
    stop = await db.get(Stop, stop_id)
    if not stop:
        raise HTTPException(404, "Stop not found")

    # =========================================
    # 6. DISTANCE CHECK
    # =========================================
    distance = haversine(
        data.lat,
        data.lng,
        float(stop.lat),
        float(stop.lng),
    )

    if distance > stop.radius_meters:
        raise HTTPException(400, "Not within stop radius")

    # =========================================
    # 7. SAVE SCAN EVENT
    # =========================================
    scan_event = TripScanEvent(
        scheduled_trip_id=trip_id,
        booking_id=booking.id,
        driver_user_id=current_user.id,
        scan_type=scan_type,
        scan_lat=Decimal(str(data.lat)),
        scan_lng=Decimal(str(data.lng)),
        matched_stop_id=stop.id,
        within_radius=True,
        qr_payload_user_id=booking.passenger_user_id,
    )

    db.add(scan_event)

    # =========================================
    # 8. UPDATE BOOKING
    # =========================================
    now = datetime.now(timezone.utc)

    if scan_type == ScanType.BOARD:
        booking.booking_status = BookingStatus.BOARDED
        booking.boarded_at = now
        booking.boarded_near_stop_id = stop.id

    else:  # DROP
        booking.booking_status = BookingStatus.COMPLETED
        booking.completed_at = now
        booking.completed_near_stop_id = stop.id

    db.add(booking)

    # =========================================
    # 9. COMMIT
    # =========================================
    await db.commit()

    # =========================================
    # RESPONSE
    # =========================================
    return {
        "message": "OTP verified successfully",
        "scan_type": scan_type.value,
        "distance_meters": round(distance, 2),
        "booking_status": booking.booking_status.value,
    }