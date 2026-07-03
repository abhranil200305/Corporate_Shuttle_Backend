# app/driver/scan_events/scan.py

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from math import asin, cos, radians, sin, sqrt

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.auth.dependencies import get_current_user
from app.db.database import get_async_session
from app.db.schema import (
    BookingStatus,
    RouteStop,
    ScanType,
    ScheduledTrip,
    Stop,
    TripBooking,
    TripEvent,
    TripScanEvent,
    User,
    UserRole,
)
from app.realtime.events import (
    get_api_refresh_hub,
    publish_departure_allowed_if_eligible,
)

router = APIRouter(prefix="/driver/scan", tags=["Driver Scan"])


class ScanRequest(BaseModel):
    qr_token: str
    lat: float
    lng: float


QR_SECRET = os.getenv("PASSENGER_QR_SECRET")
if not QR_SECRET:
    raise RuntimeError("PASSENGER_QR_SECRET is not set")


# =========================================================
# HELPERS
# =========================================================
def haversine(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1

    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(a))

    return 6371 * c * 1000


def add_padding(s: str) -> str:
    return s + "=" * (-len(s) % 4)


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


# =========================================================
# MAIN API
# =========================================================
@router.post("/{trip_id}/scan")
async def scan_passenger(
    trip_id: str,
    request: Request,
    data: ScanRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    # =====================================================
    # 1. DECODE QR
    # =====================================================
    payload = decode_qr_token(data.qr_token)
    booking_id = payload.get("booking_id")

    if not booking_id:
        raise HTTPException(400, "Invalid QR")

    if datetime.now(timezone.utc).timestamp() > payload["expires_at"]:
        raise HTTPException(400, "QR expired")

    # =====================================================
    # 2. TRIP VALIDATION
    # =====================================================
    trip = await db.get(ScheduledTrip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.driver_user_id != current_user.id:
        raise HTTPException(403, "Not your trip")

    # =====================================================
    # 3. BOOKING VALIDATION
    # =====================================================
    result = await db.execute(
        select(TripBooking).where(
            TripBooking.id == booking_id,
            TripBooking.scheduled_trip_id == trip_id
        )
    )
    booking = result.scalar_one_or_none()

    if not booking:
        raise HTTPException(404, "Booking not found")

    # =====================================================
    # 4. DETECT SCAN TYPE
    # =====================================================
    if booking.booking_status == BookingStatus.BOOKED:
        scan_type = ScanType.BOARD
    elif booking.booking_status == BookingStatus.BOARDED:
        scan_type = ScanType.DROP
    else:
        raise HTTPException(400, "Invalid booking state")

    # =====================================================
    # 5. BLOCK DUPLICATE DROP
    # =====================================================
    if scan_type == ScanType.DROP:
        existing_drop = await db.execute(
            select(TripScanEvent).where(
                TripScanEvent.booking_id == booking.id,
                TripScanEvent.scan_type == ScanType.DROP
            )
        )
        if existing_drop.scalar_one_or_none():
            raise HTTPException(400, "Passenger already dropped")

    # =====================================================
    # 6. BOARD LOGIC
    # =====================================================
    if scan_type == ScanType.BOARD:
        stop = await db.get(Stop, booking.pickup_stop_id)

        if not stop:
            raise HTTPException(404, "Pickup stop not found")

        distance = haversine(
            data.lat,
            data.lng,
            float(stop.lat),
            float(stop.lng),
        )

        if distance > (stop.radius_meters or 0):
            raise HTTPException(400, "Not within pickup stop radius")

    # =====================================================
    # 7. DROP LOGIC (ACTIVE TRIP EVENT = ONLY SOURCE OF TRUTH)
    # =====================================================
    else:
        # -------------------------------------------------
        # GET CURRENT ACTIVE STOP
        # active stop = ARRIVED but NOT DEPARTED
        # -------------------------------------------------
        result = await db.execute(
            select(TripEvent).where(
                TripEvent.scheduled_trip_id == trip_id,
                TripEvent.arrival_time.isnot(None),
                TripEvent.departure_time.is_(None),
            )
        )
        current_event = result.scalar_one_or_none()

        if not current_event:
            raise HTTPException(400, "No active stop. Driver must ARRIVE first")

        stop = await db.get(Stop, current_event.stop_id)
        if not stop:
            raise HTTPException(404, "Active stop not found")

        # -------------------------------------------------
        # GPS VALIDATION ONLY AGAINST ACTIVE STOP
        # -------------------------------------------------
        distance = haversine(
            data.lat,
            data.lng,
            float(stop.lat),
            float(stop.lng),
        )

        if distance > (stop.radius_meters or 0):
            raise HTTPException(400, "Not within current active stop radius")

        # -------------------------------------------------
        # ROUTE SEQUENCE VALIDATION
        # passenger can drop only:
        # after pickup stop
        # on/before booked drop stop
        # -------------------------------------------------
        route_stops = (await db.execute(
            select(RouteStop)
            .where(RouteStop.route_id == booking.route_id)
        )).scalars().all()

        route_map = {str(rs.stop_id): rs for rs in route_stops}

        pickup_rs = route_map.get(str(booking.pickup_stop_id))
        drop_rs = route_map.get(str(booking.dropoff_stop_id))
        current_rs = route_map.get(str(stop.id))

        if not pickup_rs or not drop_rs or not current_rs:
            raise HTTPException(400, "Invalid route mapping")

        if current_rs.sequence_no <= pickup_rs.sequence_no:
            raise HTTPException(400, "Cannot drop before pickup stop")

        if current_rs.sequence_no > drop_rs.sequence_no:
            raise HTTPException(400, "Cannot drop after booked drop stop")

    # =====================================================
    # 8. SAVE SCAN EVENT
    # matched_stop_id = actual active stop where scan happened
    # =====================================================
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

    # =====================================================
    # 9. UPDATE BOOKING
    # =====================================================
    now = datetime.now(timezone.utc)

    if scan_type == ScanType.BOARD:
        booking.booking_status = BookingStatus.BOARDED
        booking.boarded_at = now
        booking.boarded_near_stop_id = stop.id
    else:
        booking.booking_status = BookingStatus.COMPLETED
        booking.completed_at = now
        booking.completed_near_stop_id = stop.id

    db.add(booking)

    # =====================================================
    # 10. COMMIT
    # =====================================================
    await db.commit()

    refresh_hub = get_api_refresh_hub(request.app)
    event_data = {
        "trip_id": trip_id,
        "booking_id": booking.id,
        "stop_id": stop.id,
        "scan_type": scan_type.value,
        "booking_status": booking.booking_status.value,
    }
    await refresh_hub.publish(
        UserRole.PASSENGER,
        event="passenger.scan_completed",
        data=event_data,
        user_ids=[booking.passenger_user_id],
    )
    await refresh_hub.publish(
        UserRole.DRIVER,
        event="passenger.scan_completed",
        data=event_data,
        user_ids=[current_user.id],
    )
    if scan_type == ScanType.DROP:
        await publish_departure_allowed_if_eligible(
            refresh_hub,
            db,
            trip_id=trip_id,
            stop_id=stop.id,
        )

    # =====================================================
    # RESPONSE
    # =====================================================
    return {
        "message": "Scan successful",
        "scan_type": scan_type.value,
        "distance_meters": round(distance, 2),
        "booking_status": booking.booking_status.value,
        "matched_stop_id": stop.id,
    }
