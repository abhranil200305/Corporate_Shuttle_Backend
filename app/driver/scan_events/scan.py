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
# REQUEST BODY
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
# BASE64 PADDING
# ============================================================
def add_padding(s: str) -> str:
    return s + '=' * (-len(s) % 4)


# ============================================================
# DECODE QR (FIXED)
# ============================================================
def decode_qr_token(qr_token: str):
    qr_token = qr_token.strip()

    print("\n========== QR DEBUG START ==========")
    print("TOKEN LENGTH:", len(qr_token))
    print("RAW TOKEN:", qr_token)

    # -----------------------------
    # 1. Split token
    # -----------------------------
    if "." not in qr_token:
        raise HTTPException(400, "QR format invalid (missing signature separator)")

    try:
        encoded_payload, signature = qr_token.rsplit(".", 1)
    except Exception as e:
        print("SPLIT ERROR:", str(e))
        raise HTTPException(400, "QR split failed")

    # -----------------------------
    # 2. Decode payload
    # -----------------------------
    try:
        payload_bytes = base64.urlsafe_b64decode(add_padding(encoded_payload))
        payload = json.loads(payload_bytes)
    except Exception as e:
        print("DECODE ERROR:", str(e))
        raise HTTPException(400, f"Base64/JSON decode failed: {str(e)}")

    # -----------------------------
    # 3. Signature verify
    # -----------------------------
    try:
        expected_signature = hmac.new(
            QR_SECRET.encode(),
            payload_bytes,
            hashlib.sha256
        ).hexdigest()

        print("EXPECTED SIGNATURE:", expected_signature)
        print("RECEIVED SIGNATURE:", signature)

        if not hmac.compare_digest(signature, expected_signature):
            raise HTTPException(400, "Invalid QR signature")

    except HTTPException:
        raise
    except Exception as e:
        print("SIGNATURE ERROR:", str(e))
        raise HTTPException(400, f"Signature verification failed: {str(e)}")

    print("DECODE SUCCESS PAYLOAD:", payload)
    print("========== QR DEBUG END ==========\n")

    return payload


# ============================================================
# SCAN ENDPOINT (WITH FULL DEBUG)
# ============================================================
@router.post("/{trip_id}/scan")
async def scan_passenger(
    trip_id: str,
    data: ScanRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    try:
        qr_token = data.qr_token
        lat = data.lat
        lng = data.lng

        print("\n==== SCAN REQUEST START ====")

        # --------------------------------------------------------
        # 1. Decode QR
        # --------------------------------------------------------
        payload = decode_qr_token(qr_token)

        qr_trip_id = payload.get("scheduled_trip_id")
        booking_id = payload.get("booking_id")
        passenger_id = payload.get("passenger_user_id")
        scan_type = payload.get("scan_type", "board")

        print("PAYLOAD:", payload)

        if scan_type not in ["board", "drop"]:
            raise HTTPException(400, "Invalid scan type in QR")

        scan_type_enum = ScanType(scan_type)

        # --------------------------------------------------------
        # 2. Expiry
        # --------------------------------------------------------
        if datetime.now(timezone.utc).timestamp() > payload["expires_at"]:
            raise HTTPException(400, "QR expired")

        # --------------------------------------------------------
        # 3. Trip
        # --------------------------------------------------------
        if qr_trip_id != trip_id:
            raise HTTPException(400, "QR does not belong to this trip")

        trip = await db.get(ScheduledTrip, trip_id)
        if not trip:
            raise HTTPException(404, "Trip not found")

        if trip.driver_user_id != current_user.id:
            raise HTTPException(403, "Not your trip")

        # --------------------------------------------------------
        # 4. Booking
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

        if booking.passenger_user_id != passenger_id:
            raise HTTPException(400, "Passenger mismatch")

        # --------------------------------------------------------
        # 5. State
        # --------------------------------------------------------
        if scan_type_enum == ScanType.BOARD:
            if booking.booking_status != BookingStatus.BOOKED:
                raise HTTPException(400, "Passenger not eligible for boarding")

        elif scan_type_enum == ScanType.DROP:
            if booking.booking_status != BookingStatus.BOARDED:
                raise HTTPException(400, "Passenger not boarded yet")

        # --------------------------------------------------------
        # 6. Stop
        # --------------------------------------------------------
        stop_id = (
            booking.pickup_stop_id
            if scan_type_enum == ScanType.BOARD
            else booking.dropoff_stop_id
        )

        stop = await db.get(Stop, stop_id)
        if not stop:
            raise HTTPException(404, "Stop not found")

        # --------------------------------------------------------
        # 7. Radius
        # --------------------------------------------------------
        distance = haversine(lat, lng, float(stop.lat), float(stop.lng))
        within_radius = distance <= stop.radius_meters

        # --------------------------------------------------------
        # 8. Save event
        # --------------------------------------------------------
        scan_event = TripScanEvent(
            scheduled_trip_id=trip_id,
            booking_id=booking_id,
            driver_user_id=current_user.id,
            scan_type=scan_type_enum,
            scan_lat=Decimal(str(lat)),
            scan_lng=Decimal(str(lng)),
            matched_stop_id=stop.id if within_radius else None,
            within_radius=within_radius,
            qr_payload_user_id=passenger_id,
        )
        db.add(scan_event)

        # --------------------------------------------------------
        # 9. Update booking
        # --------------------------------------------------------
        if within_radius:
            if scan_type_enum == ScanType.BOARD:
                booking.booking_status = BookingStatus.BOARDED
                booking.boarded_at = datetime.now(timezone.utc)
                booking.boarded_near_stop_id = stop.id

            elif scan_type_enum == ScanType.DROP:
                booking.booking_status = BookingStatus.COMPLETED
                booking.completed_at = datetime.now(timezone.utc)
                booking.completed_near_stop_id = stop.id

            db.add(booking)

        await db.commit()

        print("==== SCAN SUCCESS ====\n")

        return {
            "message": "Scan processed",
            "scan_type": scan_type_enum.value,
            "within_radius": within_radius,
            "distance_meters": round(distance, 2),
            "booking_status": booking.booking_status.value,
        }

    except HTTPException as e:
        print("HTTP ERROR:", e.detail)
        raise e

    except Exception as e:
        print("UNEXPECTED ERROR:", str(e))
        raise HTTPException(500, f"Internal server error: {str(e)}")