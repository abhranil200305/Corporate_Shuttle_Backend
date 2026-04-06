# app/driver/stats/driver_stats.py

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.db.database import get_async_session
from app.auth.dependencies import get_current_user
from app.db.schema import (
    User,
    UserRole,
    ScheduledTrip,
    ScheduledTripStatus,
    TripBooking,
    BookingStatus,
)

router = APIRouter(prefix="/driver/stats", tags=["Driver Stats"])


@router.get("/")
async def get_driver_stats(
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    # =========================
    # ROLE CHECK
    # =========================
    if current_user.role != UserRole.DRIVER:
        raise HTTPException(status_code=403, detail="Only drivers allowed")

    driver_id = current_user.id

    # =========================
    # TOTAL TRIPS
    # =========================
    total_trips_q = await session.execute(
        select(func.count(ScheduledTrip.id)).where(
            ScheduledTrip.driver_user_id == driver_id
        )
    )
    total_trips = total_trips_q.scalar() or 0

    # =========================
    # COMPLETED TRIPS
    # =========================
    completed_trips_q = await session.execute(
        select(func.count(ScheduledTrip.id)).where(
            ScheduledTrip.driver_user_id == driver_id,
            ScheduledTrip.status == ScheduledTripStatus.COMPLETED
        )
    )
    completed_trips = completed_trips_q.scalar() or 0

    # =========================
    # CANCELLED TRIPS
    # =========================
    cancelled_trips_q = await session.execute(
        select(func.count(ScheduledTrip.id)).where(
            ScheduledTrip.driver_user_id == driver_id,
            ScheduledTrip.status == ScheduledTripStatus.CANCELLED
        )
    )
    cancelled_trips = cancelled_trips_q.scalar() or 0

    # =========================
    # PER TRIP DETAILS
    # =========================
    trips_q = await session.execute(
        select(ScheduledTrip).where(
            ScheduledTrip.driver_user_id == driver_id
        )
    )
    trips = trips_q.scalars().all()

    trip_stats = []

    for trip in trips:
        # passenger count
        passenger_q = await session.execute(
            select(func.count(TripBooking.id)).where(
                TripBooking.scheduled_trip_id == trip.id,
                TripBooking.booking_status.in_(
                    [BookingStatus.BOOKED, BookingStatus.COMPLETED]
                )
            )
        )
        passenger_count = passenger_q.scalar() or 0

        # earning (sum of driver payout)
        earning_q = await session.execute(
            select(func.sum(TripBooking.driver_payout_amount)).where(
                TripBooking.scheduled_trip_id == trip.id,
                TripBooking.booking_status == BookingStatus.COMPLETED
            )
        )
        earning = earning_q.scalar() or 0

        trip_stats.append({
            "trip_id": trip.id,
            "status": trip.status,
            "passenger_count": passenger_count,
            "earning": float(earning),
        })

    return {
        "driver_id": driver_id,
        "total_trips": total_trips,
        "completed_trips": completed_trips,
        "cancelled_trips": cancelled_trips,
        "trips": trip_stats
    }