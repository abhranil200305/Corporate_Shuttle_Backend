# app/driver/trips/current_trip_passengers.py

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.db.database import get_async_session
from app.auth.dependencies import get_current_user
from app.db.schema import (
    ScheduledTrip,
    ScheduledTripStatus,
    TripBooking,
    BookingStatus,
    User,
    UserRole,
)

router = APIRouter(prefix="/driver/trips", tags=["Driver Trips"])


# ============================================================
# DRIVER - GET CURRENT TRIP PASSENGERS
# ============================================================
@router.get("/current/passengers")
async def get_current_trip_passengers(
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """
    Returns passengers for the driver's current IN_PROGRESS trip
    """

    # ---------------------------
    # Role check
    # ---------------------------
    if current_user.role != UserRole.DRIVER:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "driver_only",
                "message": "Only drivers can access this endpoint.",
            },
        )

    # ---------------------------
    # Get IN_PROGRESS trip only
    # ---------------------------
    stmt = (
        select(ScheduledTrip)
        .where(
            ScheduledTrip.driver_user_id == current_user.id,
            ScheduledTrip.status == ScheduledTripStatus.IN_PROGRESS,
        )
        .limit(1)
    )

    result = await session.execute(stmt)
    trip = result.scalar_one_or_none()

    if trip is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "no_in_progress_trip",
                "message": "No IN_PROGRESS trip found for this driver.",
            },
        )

    # ---------------------------
    # Fetch bookings (passengers)
    # ---------------------------
    booking_stmt = (
        select(TripBooking)
        .where(
            TripBooking.scheduled_trip_id == trip.id,
            TripBooking.booking_status.in_(
                [
                    BookingStatus.BOOKED,
                    BookingStatus.BOARDED,
                ]
            ),
        )
        .options(
            selectinload(TripBooking.passenger)
            .selectinload(User.passenger_profile)
        )
    )

    booking_result = await session.execute(booking_stmt)
    bookings = booking_result.scalars().all()

    passengers = []

    for booking in bookings:
        profile = (
            booking.passenger.passenger_profile
            if booking.passenger else None
        )

        passengers.append({
            "booking_id": booking.id,
            "passenger_id": booking.passenger_user_id,
            "name": profile.full_name if profile else None,
            "seat_number": booking.seat_number,
            "status": booking.booking_status,
        })

    # ---------------------------
    # Response
    # ---------------------------
    return {
        "trip_id": trip.id,
        "total_passengers": len(passengers),
        "passengers": passengers,
    }