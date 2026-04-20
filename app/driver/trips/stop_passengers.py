# app/driver/trips/stop_passengers.py

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.db.database import get_async_session
from app.auth.dependencies import get_current_active_user
from app.db.schema import (
    TripBooking,
    BookingStatus,
    User,
    PassengerProfile
)

router = APIRouter(prefix="/driver/trips", tags=["Driver Trips"])


@router.get("/stop-passengers")
async def get_stop_passengers(
    trip_id: str = Query(...),
    stop_id: str = Query(...),
    current_driver: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_async_session)
):
    # -----------------------------
    # FETCH BOOKINGS
    # -----------------------------
    result = await session.execute(
        select(TripBooking)
        .where(
            TripBooking.scheduled_trip_id == trip_id,
            TripBooking.booking_status.in_(
                [BookingStatus.BOOKED, BookingStatus.BOARDED]
            )
        )
        .options(
            selectinload(TripBooking.passenger)
            .selectinload(User.passenger_profile),
            selectinload(TripBooking.pickup_stop),
            selectinload(TripBooking.dropoff_stop)
        )
    )

    bookings = result.scalars().all()

    if not bookings:
        return {
            "message": "No active passengers found for this trip",
            "data": []
        }

    # -----------------------------
    # CLASSIFY PASSENGERS
    # -----------------------------
    data = []

    for booking in bookings:
        passenger_profile: PassengerProfile | None = (
            booking.passenger.passenger_profile
        )

        passenger_name = (
            passenger_profile.full_name
            if passenger_profile else "Unknown"
        )

        status = ""

        # -----------------------------
        # CASE 1: Not boarded yet (at this stop)
        # -----------------------------
        if (
            booking.booking_status == BookingStatus.BOOKED
            and booking.pickup_stop_id == stop_id
        ):
            status = "NOT_BOARDED_AT_THIS_STOP"

        # -----------------------------
        # CASE 2: Boarded from this stop
        # -----------------------------
        elif (
            booking.booking_status == BookingStatus.BOARDED
            and booking.pickup_stop_id == stop_id
        ):
            status = "BOARDED_FROM_THIS_STOP"

        # -----------------------------
        # CASE 3: Boarded earlier (in bus)
        # -----------------------------
        elif booking.booking_status == BookingStatus.BOARDED:
            status = "IN_BUS_FROM_PREVIOUS_STOP"

        else:
            continue  # skip anything else

        data.append({
            "booking_id": booking.id,
            "passenger_id": booking.passenger_user_id,
            "passenger_name": passenger_name,
            "status": status,
            "pickup_stop": {
                "id": booking.pickup_stop.id,
                "name": booking.pickup_stop.name
            },
            "dropoff_stop": {
                "id": booking.dropoff_stop.id,
                "name": booking.dropoff_stop.name
            },
            "fare": float(booking.fare_amount),
            "booking_status": booking.booking_status,
        })

    return {
        "trip_id": trip_id,
        "stop_id": stop_id,
        "total_passengers": len(data),
        "data": data
    }