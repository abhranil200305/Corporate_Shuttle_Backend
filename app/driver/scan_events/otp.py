# app/driver/scan_events/otp.py

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

router = APIRouter(prefix="/driver/otp", tags=["Driver OTP"])


class OTPVerifyRequest(BaseModel):
    otp_code: str
    lat: float
    lng: float


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
    # =====================================================
    # 1. TRIP VALIDATION
    # =====================================================
    trip = await db.get(ScheduledTrip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.driver_user_id != current_user.id:
        raise HTTPException(403, "Not your trip")

    # =====================================================
    # 2. FIND BOOKING
    # =====================================================
    result = await db.execute(
        select(TripBooking).where(
            TripBooking.scheduled_trip_id == trip_id,
            TripBooking.otp == data.otp_code
        )
    )
    booking = result.scalars().first()

    if not booking:
        raise HTTPException(400, "Invalid OTP")

    # =====================================================
    # 3. VALID BOOKING STATE
    # =====================================================
    if booking.booking_status not in [
        BookingStatus.BOOKED,
        BookingStatus.BOARDED,
    ]:
        raise HTTPException(400, "Booking not valid for OTP scan")

    scan_type = (
        ScanType.BOARD
        if booking.booking_status == BookingStatus.BOOKED
        else ScanType.DROP
    )

    # =====================================================
    # 4. BLOCK DUPLICATE DROP
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
    # 5. BOARD LOGIC
    # =====================================================
    # =====================================================
    # 5. BOARD LOGIC
    # Driver must ARRIVE at the passenger pickup stop first
    # =====================================================
    if scan_type == ScanType.BOARD:

        # ------------------------------------------
        # Get the passenger pickup stop
        # ------------------------------------------
        stop = await db.get(Stop, booking.pickup_stop_id)

        if not stop:
            raise HTTPException(404, "Pickup stop not found")

        # ------------------------------------------
        # Get current ACTIVE stop
        # active stop = ARRIVED but NOT DEPARTED
        # ------------------------------------------
        result = await db.execute(
            select(TripEvent).where(
                TripEvent.scheduled_trip_id == trip_id,
                TripEvent.arrival_time.isnot(None),
                TripEvent.departure_time.is_(None),
            )
        )

        current_event = result.scalar_one_or_none()

        if not current_event:
            raise HTTPException(
                400,
                "Driver must ARRIVE at the pickup stop before boarding passengers"
            )

        # ------------------------------------------
        # Ensure driver is currently at the
        # passenger's pickup stop
        # ------------------------------------------
        if str(current_event.stop_id) != str(booking.pickup_stop_id):
            raise HTTPException(
                400,
                "Passenger can only board at their pickup stop after the driver has arrived"
            )

        # ------------------------------------------
        # GPS validation
        # ------------------------------------------
        distance = haversine(
            data.lat,
            data.lng,
            float(stop.lat),
            float(stop.lng),
        )

        if distance > (stop.radius_meters or 0):
            raise HTTPException(
                400,
                "Not within pickup stop radius"
            )

    # =====================================================
    # 6. DROP LOGIC (ACTIVE TRIP EVENT = ONLY SOURCE OF TRUTH)
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
            select(RouteStop).where(RouteStop.route_id == booking.route_id)
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
    # 7. SAVE SCAN EVENT
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
    # 8. UPDATE BOOKING
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
    # 9. COMMIT
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
    await refresh_hub.publish(
        UserRole.ADMIN,
        event="passenger.scan_completed",
        data=event_data,
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
        "message": "OTP verified successfully",
        "booking_id": booking.id,
        "seat_number": booking.seat_number,
        "scan_type": scan_type.value,
        "distance_meters": round(distance, 2),
        "booking_status": booking.booking_status.value,
        "matched_stop_id": stop.id,
    }
