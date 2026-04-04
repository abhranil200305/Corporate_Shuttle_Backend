# app/driver/scan_events/scan.py

from fastapi import APIRouter, Depends, HTTPException, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime, timezone
from math import radians, cos, sin, asin, sqrt
from decimal import Decimal
import base64
import json
import hmac
import hashlib

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
# CONFIG (MOVE TO ENV LATER)
# ============================================================
QR_SECRET = "YOUR_SECRET_KEY"


# ============================================================
# HELPER: Distance Calculation (Haversine)
# ============================================================
def haversine(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])

    dlon = lon2 - lon1
    dlat = lat2 - lat1

    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(a))

    km = 6371 * c
    return km * 1000  # meters


# ============================================================
# HELPER: Decode & Verify QR
# ============================================================
def decode_qr_token(qr_token: str):
    try:
        encoded_payload, signature = qr_token.split(".")

        payload_bytes = base64.urlsafe_b64decode(encoded_payload + "==")
        payload = json.loads(payload_bytes)

        expected_signature = hmac.new(
            QR_SECRET.encode(),
            payload_bytes,
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(signature, expected_signature):
            raise HTTPException(400, "Invalid QR signature")

        return payload

    except Exception:
        raise HTTPException(400, "Invalid QR format")


# ============================================================
# SCAN ENDPOINT
# ============================================================
@router.post("/{trip_id}/scan")
async def scan_passenger(
    trip_id: str,
    qr_token: str = Form(...),
    scan_type: ScanType = Form(...),
    lat: float = Form(...),
    lng: float = Form(...),

    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    # --------------------------------------------------------
    # 1. Fetch Trip
    # --------------------------------------------------------
    trip = await db.get(ScheduledTrip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.driver_user_id != current_user.id:
        raise HTTPException(403, "Not your trip")

    # --------------------------------------------------------
    # 2. Decode QR
    # --------------------------------------------------------
    payload = decode_qr_token(qr_token)

    # --------------------------------------------------------
    # 3. Expiry Check
    # --------------------------------------------------------
    if datetime.now(timezone.utc).timestamp() > payload["expires_at"]:
        raise HTTPException(400, "QR expired")

    # --------------------------------------------------------
    # 4. Validate Trip Match
    # --------------------------------------------------------
    if payload["scheduled_trip_id"] != trip_id:
        raise HTTPException(400, "QR does not belong to this trip")

    booking_id = payload["booking_id"]
    qr_payload_user_id = payload["passenger_user_id"]

    # --------------------------------------------------------
    # 5. Fetch Booking
    # --------------------------------------------------------
    result = await db.execute(
        select(TripBooking).where(
            TripBooking.id == booking_id,
            TripBooking.scheduled_trip_id == trip_id
        )
    )
    booking = result.scalar_one_or_none()

    if not booking:
        raise HTTPException(404, "Booking not found")

    # --------------------------------------------------------
    # 6. Validate Passenger
    # --------------------------------------------------------
    if booking.passenger_user_id != qr_payload_user_id:
        raise HTTPException(400, "Passenger mismatch")

    # --------------------------------------------------------
    # 7. Prevent Duplicate Scan
    # --------------------------------------------------------
    if scan_type == ScanType.BOARD and booking.booking_status == BookingStatus.BOARDED:
        raise HTTPException(400, "Passenger already boarded")

    if scan_type == ScanType.DROP and booking.booking_status == BookingStatus.COMPLETED:
        raise HTTPException(400, "Trip already completed")

    # --------------------------------------------------------
    # 8. Get Expected Stop
    # --------------------------------------------------------
    if scan_type == ScanType.BOARD:
        stop_id = booking.pickup_stop_id
    else:
        stop_id = booking.dropoff_stop_id

    stop = await db.get(Stop, stop_id)
    if not stop:
        raise HTTPException(404, "Stop not found")

    # --------------------------------------------------------
    # 9. Check Radius
    # --------------------------------------------------------
    distance = haversine(
        lat,
        lng,
        float(stop.lat),
        float(stop.lng),
    )

    within_radius = distance <= stop.radius_meters

    # --------------------------------------------------------
    # 10. Save Scan Event (ALWAYS)
    # --------------------------------------------------------
    scan_event = TripScanEvent(
        scheduled_trip_id=trip_id,
        booking_id=booking_id,
        driver_user_id=current_user.id,
        scan_type=scan_type,
        scan_lat=Decimal(str(lat)),
        scan_lng=Decimal(str(lng)),
        matched_stop_id=stop.id if within_radius else None,
        within_radius=within_radius,
        qr_payload_user_id=qr_payload_user_id,
    )

    db.add(scan_event)

    # --------------------------------------------------------
    # 11. Update Booking Status (ONLY IF VALID)
    # --------------------------------------------------------
    if within_radius:
        if scan_type == ScanType.BOARD:
            booking.booking_status = BookingStatus.BOARDED
            booking.boarded_at = datetime.now(timezone.utc)
            booking.boarded_near_stop_id = stop.id

        elif scan_type == ScanType.DROP:
            booking.booking_status = BookingStatus.COMPLETED
            booking.completed_at = datetime.now(timezone.utc)
            booking.completed_near_stop_id = stop.id

        db.add(booking)

    await db.commit()

    return {
        "message": "Scan processed",
        "within_radius": within_radius,
        "distance_meters": round(distance, 2),
        "scan_type": scan_type.value,
    }