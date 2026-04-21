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
    RouteStop,
    User,
)

router = APIRouter(prefix="/driver/otp", tags=["Driver OTP"])


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


@router.post("/{trip_id}/verify")
async def verify_otp_scan(
    trip_id: str,
    data: OTPVerifyRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    # =========================
    # 1. TRIP VALIDATION
    # =========================
    trip = await db.get(ScheduledTrip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.driver_user_id != current_user.id:
        raise HTTPException(403, "Not your trip")

    # =========================
    # 2. FIND BOOKING
    # =========================
    result = await db.execute(
        select(TripBooking).where(
            TripBooking.scheduled_trip_id == trip_id,
            TripBooking.otp == data.otp_code
        )
    )
    booking = result.scalars().first()

    if not booking:
        raise HTTPException(400, "Invalid OTP")

    if booking.booking_status not in [
        BookingStatus.BOOKED,
        BookingStatus.BOARDED,
    ]:
        raise HTTPException(400, "Booking not valid")

    scan_type = (
        ScanType.BOARD
        if booking.booking_status == BookingStatus.BOOKED
        else ScanType.DROP
    )

    # =========================
    # BLOCK DUPLICATE DROP
    # =========================
    if scan_type == ScanType.DROP:
        existing = await db.execute(
            select(TripScanEvent).where(
                TripScanEvent.booking_id == booking.id,
                TripScanEvent.scan_type == ScanType.DROP
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(400, "Already dropped")

    # =========================
    # BOARD LOGIC
    # =========================
    if scan_type == ScanType.BOARD:
        stop = await db.get(Stop, booking.pickup_stop_id)

        distance = haversine(
            data.lat, data.lng,
            float(stop.lat), float(stop.lng)
        )

        if distance > stop.radius_meters:
            raise HTTPException(400, "Outside pickup radius")

    # =========================
    # DROP LOGIC (FINAL FIX)
    # =========================
    else:
        route_stops = (await db.execute(
            select(RouteStop)
            .where(RouteStop.route_id == booking.route_id)
            .order_by(RouteStop.sequence_no)
        )).scalars().all()

        route_map = {rs.stop_id: rs for rs in route_stops}

        pickup_rs = route_map.get(booking.pickup_stop_id)
        drop_rs = route_map.get(booking.dropoff_stop_id)

        if not pickup_rs or not drop_rs:
            raise HTTPException(400, "Invalid route stops")

        # ✅ valid stops only
        valid_rs = [
            rs for rs in route_stops
            if pickup_rs.sequence_no < rs.sequence_no <= drop_rs.sequence_no
        ]

        stops = (await db.execute(
            select(Stop).where(
                Stop.id.in_([rs.stop_id for rs in valid_rs])
            )
        )).scalars().all()

        stop_map = {s.id: s for s in stops}

        matched_stop = None
        min_distance = float("inf")

        # 🔥 FINAL FIX: CLOSEST MATCH ONLY
        for rs in valid_rs:
            s = stop_map.get(rs.stop_id)
            if not s:
                continue

            dist = haversine(
                data.lat,
                data.lng,
                float(s.lat),
                float(s.lng),
            )

            if dist <= s.radius_meters and dist < min_distance:
                matched_stop = s
                min_distance = dist

        if not matched_stop:
            raise HTTPException(400, "Not inside any valid stop")

        stop = matched_stop
        distance = min_distance

    # =========================
    # SAVE EVENT
    # =========================
    scan_event = TripScanEvent(
        scheduled_trip_id=trip_id,
        booking_id=booking.id,
        driver_user_id=current_user.id,
        scan_type=scan_type,
        scan_lat=Decimal(str(data.lat)),
        scan_lng=Decimal(str(data.lng)),
        matched_stop_id=stop.id,   # ✅ ALWAYS CORRECT NOW
        within_radius=True,
        qr_payload_user_id=booking.passenger_user_id,
    )

    db.add(scan_event)

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
    await db.commit()

    return {
        "message": "OTP verified successfully",
        "scan_type": scan_type.value,
        "distance_meters": round(distance, 2),
        "booking_status": booking.booking_status.value,
        "matched_stop_id": stop.id
    }