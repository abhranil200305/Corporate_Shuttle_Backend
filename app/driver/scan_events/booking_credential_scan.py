from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from math import asin, cos, radians, sin, sqrt
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.schema import (
    BookingSession,
    BookingSessionStatus,
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

SCAN_ACTIVE_BOOKING_STATUSES = (
    BookingStatus.BOOKED,
    BookingStatus.BOARDED,
)


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1

    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(a))

    return 6371 * c * 1000


def _sort_bookings_for_scan(bookings: list[TripBooking]) -> list[TripBooking]:
    return sorted(
        bookings,
        key=lambda booking: (
            booking.seat_number,
            booking.created_at,
            booking.id,
        ),
    )


async def ensure_driver_owns_trip(
    db: AsyncSession,
    *,
    trip_id: str,
    current_user: User,
) -> ScheduledTrip:
    trip = await db.get(ScheduledTrip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.driver_user_id != current_user.id:
        raise HTTPException(403, "Not your trip")

    return trip


async def _list_active_session_bookings_for_update(
    db: AsyncSession,
    *,
    trip_id: str,
    booking_session_id: str,
) -> list[TripBooking]:
    result = await db.execute(
        select(TripBooking)
        .where(
            TripBooking.booking_session_id == booking_session_id,
            TripBooking.scheduled_trip_id == trip_id,
            TripBooking.booking_status.in_(SCAN_ACTIVE_BOOKING_STATUSES),
        )
        .order_by(TripBooking.seat_number.asc(), TripBooking.created_at.asc())
        .with_for_update()
    )
    bookings = list(result.scalars().unique().all())

    if not bookings:
        raise HTTPException(400, "Booking session not valid for scan")

    return _sort_bookings_for_scan(bookings)


async def resolve_qr_payload_bookings_for_update(
    db: AsyncSession,
    *,
    trip_id: str,
    payload: dict[str, Any],
) -> list[TripBooking]:
    payload_trip_id = payload.get("scheduled_trip_id")
    if payload_trip_id and str(payload_trip_id) != trip_id:
        raise HTTPException(400, "QR does not belong to this trip")

    booking_session_id = payload.get("booking_session_id")
    if booking_session_id:
        return await _list_active_session_bookings_for_update(
            db,
            trip_id=trip_id,
            booking_session_id=str(booking_session_id),
        )

    booking_id = payload.get("booking_id")
    if not booking_id:
        raise HTTPException(400, "Invalid QR")

    result = await db.execute(
        select(TripBooking)
        .where(
            TripBooking.id == str(booking_id),
            TripBooking.scheduled_trip_id == trip_id,
        )
        .with_for_update()
    )
    booking = result.scalar_one_or_none()

    if not booking:
        raise HTTPException(404, "Booking not found")

    # Backward compatibility: old QR tokens only carried a child booking_id.
    # If that booking belongs to a session, scanning the old token should now
    # still process the whole active booking session.
    if booking.booking_session_id:
        return await _list_active_session_bookings_for_update(
            db,
            trip_id=trip_id,
            booking_session_id=booking.booking_session_id,
        )

    if booking.booking_status not in SCAN_ACTIVE_BOOKING_STATUSES:
        raise HTTPException(400, "Invalid booking state")

    return [booking]


async def resolve_otp_bookings_for_update(
    db: AsyncSession,
    *,
    trip_id: str,
    otp_code: str,
) -> list[TripBooking]:
    otp_code = otp_code.strip()
    if not otp_code:
        raise HTTPException(400, "Invalid OTP")

    session_result = await db.execute(
        select(BookingSession.id)
        .where(
            BookingSession.scheduled_trip_id == trip_id,
            BookingSession.otp == otp_code,
            BookingSession.status.in_(
                (
                    BookingSessionStatus.PENDING_PAYMENT,
                    BookingSessionStatus.CONFIRMED,
                )
            ),
        )
        .with_for_update()
    )
    session_ids = [str(item) for item in session_result.scalars().all()]

    booking_result = await db.execute(
        select(TripBooking)
        .where(
            TripBooking.scheduled_trip_id == trip_id,
            TripBooking.otp == otp_code,
            TripBooking.booking_status.in_(SCAN_ACTIVE_BOOKING_STATUSES),
        )
        .order_by(TripBooking.seat_number.asc(), TripBooking.created_at.asc())
        .with_for_update()
    )
    matched_bookings = list(booking_result.scalars().unique().all())

    candidate_keys: set[tuple[str, str]] = {
        ("session", session_id)
        for session_id in session_ids
    }

    for booking in matched_bookings:
        if booking.booking_session_id:
            candidate_keys.add(("session", booking.booking_session_id))
        else:
            candidate_keys.add(("booking", booking.id))

    if not candidate_keys:
        raise HTTPException(400, "Invalid OTP")

    if len(candidate_keys) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "ambiguous_booking_otp",
                "message": (
                    "This OTP matches more than one active booking group for "
                    "this trip. Ask the passenger to refresh and show the QR, "
                    "or contact support."
                ),
            },
        )

    scope, identifier = next(iter(candidate_keys))
    if scope == "session":
        return await _list_active_session_bookings_for_update(
            db,
            trip_id=trip_id,
            booking_session_id=identifier,
        )

    booking = next(
        (item for item in matched_bookings if item.id == identifier),
        None,
    )
    if booking is None:
        raise HTTPException(400, "Invalid OTP")

    return [booking]


async def _validate_board_scan(
    db: AsyncSession,
    *,
    bookings: list[TripBooking],
    lat: float,
    lng: float,
) -> tuple[Stop, float]:
    pickup_stop_ids = {booking.pickup_stop_id for booking in bookings}
    if len(pickup_stop_ids) != 1:
        raise HTTPException(409, "Credential spans multiple pickup stops")

    stop = await db.get(Stop, next(iter(pickup_stop_ids)))
    if not stop:
        raise HTTPException(404, "Pickup stop not found")

    distance = haversine(
        lat,
        lng,
        float(stop.lat),
        float(stop.lng),
    )

    if distance > (stop.radius_meters or 0):
        raise HTTPException(400, "Not within pickup stop radius")

    return stop, distance


async def _get_current_active_stop_or_raise(
    db: AsyncSession,
    *,
    trip_id: str,
) -> Stop:
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

    return stop


async def _validate_drop_scan(
    db: AsyncSession,
    *,
    trip_id: str,
    bookings: list[TripBooking],
    lat: float,
    lng: float,
) -> tuple[Stop, float]:
    existing_drop_result = await db.execute(
        select(TripScanEvent.booking_id).where(
            TripScanEvent.booking_id.in_([booking.id for booking in bookings]),
            TripScanEvent.scan_type == ScanType.DROP,
        )
    )
    existing_drop_booking_ids = set(existing_drop_result.scalars().all())
    if existing_drop_booking_ids:
        raise HTTPException(400, "One or more passengers already dropped")

    stop = await _get_current_active_stop_or_raise(db, trip_id=trip_id)

    distance = haversine(
        lat,
        lng,
        float(stop.lat),
        float(stop.lng),
    )

    if distance > (stop.radius_meters or 0):
        raise HTTPException(400, "Not within current active stop radius")

    route_ids = sorted({booking.route_id for booking in bookings})
    route_stop_result = await db.execute(
        select(RouteStop).where(RouteStop.route_id.in_(route_ids))
    )
    route_maps: dict[str, dict[str, RouteStop]] = {}
    for route_stop in route_stop_result.scalars().all():
        route_maps.setdefault(route_stop.route_id, {})[
            str(route_stop.stop_id)
        ] = route_stop

    for booking in bookings:
        route_map = route_maps.get(booking.route_id, {})
        pickup_rs = route_map.get(str(booking.pickup_stop_id))
        drop_rs = route_map.get(str(booking.dropoff_stop_id))
        current_rs = route_map.get(str(stop.id))

        if not pickup_rs or not drop_rs or not current_rs:
            raise HTTPException(400, "Invalid route mapping")

        if current_rs.sequence_no <= pickup_rs.sequence_no:
            raise HTTPException(400, "Cannot drop before pickup stop")

        if current_rs.sequence_no > drop_rs.sequence_no:
            raise HTTPException(400, "Cannot drop after booked drop stop")

    return stop, distance


async def _build_scan_plan(
    db: AsyncSession,
    *,
    trip_id: str,
    bookings: list[TripBooking],
    lat: float,
    lng: float,
) -> tuple[ScanType, list[TripBooking], Stop, float]:
    booked = [
        booking
        for booking in bookings
        if booking.booking_status == BookingStatus.BOOKED
    ]
    boarded = [
        booking
        for booking in bookings
        if booking.booking_status == BookingStatus.BOARDED
    ]

    board_error: HTTPException | None = None
    if booked:
        try:
            stop, distance = await _validate_board_scan(
                db,
                bookings=booked,
                lat=lat,
                lng=lng,
            )
            return (
                ScanType.BOARD,
                _sort_bookings_for_scan(booked),
                stop,
                distance,
            )
        except HTTPException as exc:
            board_error = exc

    if boarded:
        try:
            stop, distance = await _validate_drop_scan(
                db,
                trip_id=trip_id,
                bookings=boarded,
                lat=lat,
                lng=lng,
            )
            return (
                ScanType.DROP,
                _sort_bookings_for_scan(boarded),
                stop,
                distance,
            )
        except HTTPException:
            if not board_error:
                raise

    if board_error:
        raise board_error

    raise HTTPException(400, "Invalid booking state")


async def execute_credential_scan(
    *,
    trip_id: str,
    request: Request,
    lat: float,
    lng: float,
    db: AsyncSession,
    current_user: User,
    bookings: list[TripBooking],
    success_message: str,
    driver_trip_validated: bool = False,
) -> dict[str, Any]:
    if not driver_trip_validated:
        await ensure_driver_owns_trip(
            db,
            trip_id=trip_id,
            current_user=current_user,
        )

    bookings = _sort_bookings_for_scan(
        [
            booking
            for booking in bookings
            if booking.booking_status in SCAN_ACTIVE_BOOKING_STATUSES
        ]
    )

    if not bookings:
        raise HTTPException(400, "Invalid booking state")

    scan_type, process_bookings, stop, distance = await _build_scan_plan(
        db,
        trip_id=trip_id,
        bookings=bookings,
        lat=lat,
        lng=lng,
    )

    now = datetime.now(timezone.utc)

    for booking in process_bookings:
        scan_event = TripScanEvent(
            scheduled_trip_id=trip_id,
            booking_id=booking.id,
            driver_user_id=current_user.id,
            scan_type=scan_type,
            scan_lat=Decimal(str(lat)),
            scan_lng=Decimal(str(lng)),
            matched_stop_id=stop.id,
            within_radius=True,
            qr_payload_user_id=booking.passenger_user_id,
        )
        db.add(scan_event)

        if scan_type == ScanType.BOARD:
            booking.booking_status = BookingStatus.BOARDED
            booking.boarded_at = booking.boarded_at or now
            booking.boarded_near_stop_id = stop.id
        else:
            booking.booking_status = BookingStatus.COMPLETED
            booking.completed_at = booking.completed_at or now
            booking.completed_near_stop_id = stop.id

        db.add(booking)

    await db.commit()

    booking_ids = [booking.id for booking in process_bookings]
    seat_numbers = [booking.seat_number for booking in process_bookings]
    passenger_user_ids = sorted(
        {booking.passenger_user_id for booking in process_bookings}
    )
    booking_session_ids = {
        booking.booking_session_id
        for booking in process_bookings
        if booking.booking_session_id
    }
    booking_session_id = (
        next(iter(booking_session_ids))
        if len(booking_session_ids) == 1
        else None
    )

    primary_booking = process_bookings[0]
    event_data = {
        "trip_id": trip_id,
        "booking_id": primary_booking.id,
        "booking_ids": booking_ids,
        "booking_session_id": booking_session_id,
        "seat_number": primary_booking.seat_number,
        "seat_numbers": seat_numbers,
        "processed_count": len(process_bookings),
        "stop_id": stop.id,
        "scan_type": scan_type.value,
        "booking_status": primary_booking.booking_status.value,
        "booking_statuses": {
            booking.id: booking.booking_status.value
            for booking in process_bookings
        },
    }

    refresh_hub = get_api_refresh_hub(request.app)
    await refresh_hub.publish(
        UserRole.PASSENGER,
        event="passenger.scan_completed",
        data=event_data,
        user_ids=passenger_user_ids,
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

    return {
        "message": success_message,
        "booking_id": primary_booking.id,
        "booking_ids": booking_ids,
        "booking_session_id": booking_session_id,
        "seat_number": primary_booking.seat_number,
        "seat_numbers": seat_numbers,
        "processed_count": len(process_bookings),
        "scan_type": scan_type.value,
        "distance_meters": round(distance, 2),
        "booking_status": primary_booking.booking_status.value,
        "booking_statuses": {
            booking.id: booking.booking_status.value
            for booking in process_bookings
        },
        "matched_stop_id": stop.id,
    }
