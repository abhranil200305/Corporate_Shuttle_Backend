# app/driver/trips/trip_bookings.py

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.db.database import get_async_session
from app.auth.dependencies import get_current_user
from app.db.schema import (
    ScheduledTrip,
    TripBooking,
    BookingStatus,
    User,
    UserRole,
)

router = APIRouter(prefix="/driver/trips", tags=["Driver Trips"])


# ============================================================
# DRIVER - GET TRIP BOOKINGS DETAIL
# ============================================================
@router.get("/{trip_id}/bookings")
async def get_trip_bookings(
    trip_id: str,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """
    Returns full booking details for a given trip_id
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
    # Validate trip belongs to driver
    # ---------------------------
    trip_stmt = select(ScheduledTrip).where(
        ScheduledTrip.id == trip_id,
        ScheduledTrip.driver_user_id == current_user.id,
    )

    trip_result = await session.execute(trip_stmt)
    trip = trip_result.scalar_one_or_none()

    if trip is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "trip_not_found",
                "message": "Trip not found for this driver.",
            },
        )

    # ---------------------------
    # Fetch bookings
    # ---------------------------
    booking_stmt = (
        select(TripBooking)
        .where(
            TripBooking.scheduled_trip_id == trip_id,
            TripBooking.booking_status.in_(
                [
                    BookingStatus.BOOKED,
                    BookingStatus.BOARDED,
                    BookingStatus.COMPLETED,
                ]
            ),
        )
        .options(
            selectinload(TripBooking.passenger)
            .selectinload(User.passenger_profile),
            selectinload(TripBooking.pickup_stop),
            selectinload(TripBooking.dropoff_stop),
        )
    )

    booking_result = await session.execute(booking_stmt)
    bookings = booking_result.scalars().all()

    response_data = []

    for booking in bookings:
        profile = (
            booking.passenger.passenger_profile
            if booking.passenger else None
        )

        response_data.append({
            "booking_id": booking.id,
            "passenger_id": booking.passenger_user_id,
            "passenger_name": profile.full_name if profile else None,
            "fare": float(booking.fare_amount) if booking.fare_amount else None,
            "status": booking.booking_status,
            "seat_number": booking.seat_number,

            "pickup_stop": {
                "id": booking.pickup_stop.id,
                "name": booking.pickup_stop.name,
            } if booking.pickup_stop else None,

            "dropoff_stop": {
                "id": booking.dropoff_stop.id,
                "name": booking.dropoff_stop.name,
            } if booking.dropoff_stop else None,

            "boarded_at": booking.boarded_at,
            "completed_at": booking.completed_at,
        })

    # ---------------------------
    # Response
    # ---------------------------
    return {
        "trip_id": trip_id,
        "total_bookings": len(response_data),
        "bookings": response_data,
    }