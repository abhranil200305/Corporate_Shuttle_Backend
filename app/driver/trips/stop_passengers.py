# app/driver/trips/stop_passengers.py

from fastapi import APIRouter, Depends, Query
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
    # FETCH BOOKINGS (only relevant)
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
            "trip_id": trip_id,
            "stop_id": stop_id,
            "boarding_count": 0,
            "drop_count": 0,
            "boarding_passengers": [],
            "drop_passengers": []
        }

    boarding_list = []
    drop_list = []

    # -----------------------------
    # CLASSIFY
    # -----------------------------
    for booking in bookings:
        passenger_profile: PassengerProfile | None = (
            booking.passenger.passenger_profile
        )

        passenger_name = (
            passenger_profile.full_name
            if passenger_profile else "Unknown"
        )

        passenger_data = {
            "booking_id": booking.id,
            "passenger_id": booking.passenger_user_id,
            "passenger_name": passenger_name,
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
        }

        # -----------------------------
        # BOARDING PASSENGERS
        # -----------------------------
        if (
            booking.pickup_stop_id == stop_id
            and booking.booking_status == BookingStatus.BOOKED
        ):
            boarding_list.append(passenger_data)

        # -----------------------------
        # DROPPING PASSENGERS
        # -----------------------------
        elif (
            booking.dropoff_stop_id == stop_id
            and booking.booking_status == BookingStatus.BOARDED
        ):
            drop_list.append(passenger_data)

    return {
        "trip_id": trip_id,
        "stop_id": stop_id,
        "boarding_count": len(boarding_list),
        "drop_count": len(drop_list),
        "boarding_passengers": boarding_list,
        "drop_passengers": drop_list
    }