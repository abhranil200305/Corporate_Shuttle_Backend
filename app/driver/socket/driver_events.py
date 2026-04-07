# app/driver/socket/driver_events.py

from datetime import datetime, timezone
from typing import List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from fastapi import HTTPException

from app.db.schema import (
    ScheduledTrip,
    TripBooking,
    BookingStatus,
)
from app.driver.socket.socket_manager import emit_to_user


# ------------------------------
# Helper: fetch booked passengers
# ------------------------------
async def _get_booked_passengers(session: AsyncSession, trip_id: str) -> tuple[List[str], ScheduledTrip]:
    """
    Returns a list of passenger_user_ids for a trip with BOOKED or BOARDED status
    """
    stmt = (
        select(ScheduledTrip)
        .where(ScheduledTrip.id == trip_id)
        .options(selectinload(ScheduledTrip.bookings))
    )
    result = await session.execute(stmt)
    trip = result.scalar_one_or_none()
    if not trip:
        raise HTTPException(status_code=404, detail="Scheduled trip not found")

    passenger_ids = [
        b.passenger_user_id
        for b in trip.bookings
        if b.booking_status in (BookingStatus.BOOKED, BookingStatus.BOARDED)
        and b.cancelled_at is None
    ]
    return passenger_ids, trip


# ------------------------------
# Notify passengers: Trip Started
# ------------------------------
async def notify_trip_started(session: AsyncSession, trip_id: str):
    """
    Notify all booked passengers that the trip has started
    """
    passenger_ids, trip = await _get_booked_passengers(session, trip_id)

    for passenger_id in passenger_ids:
        await emit_to_user(
            user_id=passenger_id,
            event="TRIP_STARTED",
            data={
                "trip_id": trip.id,
                "driver_id": trip.driver_user_id,
                "route_id": trip.route_id,
                "planned_start_at": str(trip.planned_start_at),
            },
        )


# ------------------------------
# Notify passengers: Passenger Boarded
# ------------------------------
async def notify_passenger_boarded(session: AsyncSession, booking_id: str):
    """
    Notify the passenger that they have successfully boarded
    """
    stmt = select(TripBooking).where(TripBooking.id == booking_id).options(selectinload(TripBooking.passenger))
    result = await session.execute(stmt)
    booking = result.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    await emit_to_user(
        user_id=booking.passenger_user_id,
        event="PASSENGER_BOARDED",
        data={
            "booking_id": booking.id,
            "trip_id": booking.scheduled_trip_id,
            "pickup_stop_id": booking.pickup_stop_id,
            "boarded_at": str(datetime.now(timezone.utc)),
        },
    )


# ------------------------------
# Notify passengers: Trip Ended
# ------------------------------
async def notify_trip_ended(session: AsyncSession, trip_id: str):
    """
    Notify all booked passengers that the trip has ended
    """
    passenger_ids, trip = await _get_booked_passengers(session, trip_id)

    for passenger_id in passenger_ids:
        await emit_to_user(
            user_id=passenger_id,
            event="TRIP_ENDED",
            data={
                "trip_id": trip.id,
                "driver_id": trip.driver_user_id,
                "route_id": trip.route_id,
                "actual_end_at": str(trip.actual_end_at or datetime.now(timezone.utc)),
            },
        )