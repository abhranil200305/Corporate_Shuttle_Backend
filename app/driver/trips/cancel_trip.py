# app/driver/trips/cancel_trip.py
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update
from datetime import datetime, timedelta, timezone

from app.db.schema import ScheduledTrip, ScheduledTripStatus, TripBooking, BookingStatus, User
from app.db.database import get_async_session
from app.auth.dependencies import get_current_user  

router = APIRouter(
    prefix="/trips",
    tags=["Driver Trips"]
)


@router.post("/{trip_id}/cancel", status_code=200)
async def cancel_trip(
    trip_id: str,
    current_driver: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    """
    Cancel a scheduled trip from the driver panel.

    Rules:
    - Only trips with status SCHEDULED can be cancelled.
    - Can cancel only if current time <= planned_start_at - 1 hour.
    - All passenger bookings for this trip are automatically cancelled.
    """

    # Fetch the trip assigned to this driver
    result = await db.execute(
        select(ScheduledTrip).where(
            ScheduledTrip.id == trip_id,
            ScheduledTrip.driver_user_id == current_driver.id
        )
    )
    trip: ScheduledTrip | None = result.scalars().first()

    if not trip:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trip not found or you are not the assigned driver."
        )

    if trip.status != ScheduledTripStatus.SCHEDULED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel trip with status '{trip.status.value}'."
        )

    # Ensure timezone-aware comparison
    now_utc = datetime.now(timezone.utc)
    planned_start_at = (
        trip.planned_start_at.replace(tzinfo=timezone.utc)
        if trip.planned_start_at.tzinfo is None
        else trip.planned_start_at
    )
    cancel_deadline = planned_start_at - timedelta(hours=1)

    if now_utc > cancel_deadline:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot cancel trip less than 1 hour before planned start time."
        )

    # --- CANCEL THE TRIP ---
    trip.status = ScheduledTripStatus.CANCELLED

    # --- CANCEL ALL ACTIVE BOOKINGS ---
    await db.execute(
        update(TripBooking)
        .where(
            TripBooking.scheduled_trip_id == trip.id,
            TripBooking.booking_status.in_([
                BookingStatus.PENDING_PAYMENT,
                BookingStatus.BOOKED,
                BookingStatus.BOARDED
            ])
        )
        .values(
            booking_status=BookingStatus.CANCELLED,
            cancelled_at=now_utc
        )
    )

    # Commit all changes
    await db.commit()
    await db.refresh(trip)

    return {
        "message": "Trip successfully cancelled. All active bookings have been cancelled.",
        "trip_id": trip.id,
        "status": trip.status.value
    }