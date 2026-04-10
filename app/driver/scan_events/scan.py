# app/driver/scan_events/scan.py

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime, timezone
from math import radians, cos, sin, asin, sqrt
from decimal import Decimal
import base64
import json
import hmac
import hashlib
import os

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

router = APIRouter(prefix="/driver/scan", tags=["Driver Scan"])


# ============================================================
# REQUEST BODY (FIXED ❌ removed scan_type)
# ============================================================
class ScanRequest(BaseModel):
    qr_token: str
    lat: float
    lng: float


# ============================================================
# SECRET
# ============================================================
QR_SECRET = os.getenv("PASSENGER_QR_SECRET")
if not QR_SECRET:
    raise RuntimeError("PASSENGER_QR_SECRET is not set")


# ============================================================
# HAVERSINE
# ============================================================
def haversine(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1

    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(a))

    return 6371 * c * 1000


# ============================================================
# BASE64 FIX
# ============================================================
def add_padding(s: str) -> str:
    return s + "=" * (-len(s) % 4)


# ============================================================
# DECODE QR TOKEN
# ============================================================
def decode_qr_token(qr_token: str):
    if "." not in qr_token:
        raise HTTPException(400, "Invalid QR format")

    encoded_payload, signature = qr_token.rsplit(".", 1)

    try:
        payload_bytes = base64.urlsafe_b64decode(add_padding(encoded_payload))
        payload = json.loads(payload_bytes)
    except Exception:
        raise HTTPException(400, "Invalid QR payload")

    expected_signature = hmac.new(
        QR_SECRET.encode(),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(400, "Invalid QR signature")

    return payload


# ============================================================
# SCAN ENDPOINT
# ============================================================
@router.post("/{trip_id}/scan")
async def scan_passenger(
    trip_id: str,
    data: ScanRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    # 1. Decode QR
    payload = decode_qr_token(data.qr_token)
    booking_id = payload.get("booking_id")

    if not booking_id:
        raise HTTPException(400, "Invalid QR")

    # 2. Expiry
    if datetime.now(timezone.utc).timestamp() > payload["expires_at"]:
        raise HTTPException(400, "QR expired")

    # 3. Trip validation
    trip = await db.get(ScheduledTrip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.driver_user_id != current_user.id:
        raise HTTPException(403, "Not your trip")

    # 4. Booking validation
    result = await db.execute(
        select(TripBooking).where(
            TripBooking.id == booking_id,
            TripBooking.scheduled_trip_id == trip_id
        )
    )
    booking = result.scalar_one_or_none()

    if not booking:
        raise HTTPException(404, "Booking not found")

    # ============================================================
    # 🔥 AUTO DETECT SCAN TYPE (MAIN FIX)
    # ============================================================
    if booking.booking_status == BookingStatus.BOOKED:
        scan_type = ScanType.BOARD

    elif booking.booking_status == BookingStatus.BOARDED:
        scan_type = ScanType.DROP

    elif booking.booking_status == BookingStatus.COMPLETED:
        raise HTTPException(400, "Already completed")

    else:
        raise HTTPException(400, "Invalid booking state")

    # 5. Stop logic
    stop_id = (
        booking.pickup_stop_id
        if scan_type == ScanType.BOARD
        else booking.dropoff_stop_id
    )

    stop = await db.get(Stop, stop_id)
    if not stop:
        raise HTTPException(404, "Stop not found")

    # 6. Distance check
    distance = haversine(
        data.lat,
        data.lng,
        float(stop.lat),
        float(stop.lng),
    )

    if distance > stop.radius_meters:
        raise HTTPException(400, "Not within stop radius")

    # 7. Save scan event
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

    # 8. Update booking
    now = datetime.now(timezone.utc)

    if scan_type == ScanType.BOARD:
        booking.booking_status = BookingStatus.BOARDED
        booking.boarded_at = now
        booking.boarded_near_stop_id = stop.id

    elif scan_type == ScanType.DROP:
        booking.booking_status = BookingStatus.COMPLETED
        booking.completed_at = now
        booking.completed_near_stop_id = stop.id

    db.add(booking)

    await db.commit()

    return {
        "message": "Scan successful",
        "scan_type": scan_type.value,
        "distance_meters": round(distance, 2),
        "booking_status": booking.booking_status.value,
    }