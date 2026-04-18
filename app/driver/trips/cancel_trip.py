# app/driver/trips/cancel_trip.py
from fastapi import APIRouter, Depends, HTTPException, status, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update
from datetime import datetime, timezone

from app.db.schema import (
    ScheduledTrip,
    ScheduledTripStatus,
    TripBooking,
    BookingStatus,
    User,
)
from app.db.database import get_async_session
from app.auth.dependencies import get_current_user

from app.payments.fine_register_service import FineRegisterService
from app.notifications.service import NotificationService


router = APIRouter(
    prefix="/trips",
    tags=["Driver Trips"]
)


def _get_ws_hub_from_app(app):
    return getattr(app.state, "ws_hub", None)


@router.post("/{trip_id}/cancel", status_code=200)
async def cancel_trip(
    request: Request,
    trip_id: str,
    cancellation_reason: str | None = Form(None),
    current_driver: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):

    # ---------------------------
    # FETCH TRIP
    # ---------------------------
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

    # ---------------------------
    # STATUS CHECK
    # ---------------------------
    if trip.status != ScheduledTripStatus.SCHEDULED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel trip with status '{trip.status.value}'."
        )

    # ---------------------------
    # TIME CHECK
    # ---------------------------
    now_utc = datetime.now(timezone.utc)

    planned_start_at = (
        trip.planned_start_at.replace(tzinfo=timezone.utc)
        if trip.planned_start_at.tzinfo is None
        else trip.planned_start_at
    )

    if now_utc >= planned_start_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot cancel trip after planned start time."
        )

    # ---------------------------
    # FETCH ACTIVE BOOKINGS (IMPORTANT)
    # ---------------------------
    bookings_result = await db.execute(
        select(TripBooking).where(
            TripBooking.scheduled_trip_id == trip.id,
            TripBooking.booking_status.in_([
                BookingStatus.PENDING_PAYMENT,
                BookingStatus.BOOKED,
                BookingStatus.BOARDED
            ])
        )
    )
    active_bookings = bookings_result.scalars().all()

    passenger_ids = list({
        booking.passenger_user_id for booking in active_bookings
    })

    # ---------------------------
    # CANCEL TRIP
    # ---------------------------
    trip.status = ScheduledTripStatus.CANCELLED

    if cancellation_reason:
        trip.premature_end_reason = cancellation_reason

    # ---------------------------
    # CANCEL BOOKINGS
    # ---------------------------
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

    # ---------------------------
    # 🔥 REGISTER FINES
    # ---------------------------
    fine_service = FineRegisterService(db)

    fine_result = await fine_service.register_driver_trip_cancellation_fines(
        scheduled_trip_id=trip.id,
        driver_user_id=current_driver.id,
        cancellation_reason=cancellation_reason,
        occurred_at=now_utc,
    )

    # ---------------------------
    # 🔔 SEND NOTIFICATIONS
    # ---------------------------
    ws_hub = _get_ws_hub_from_app(request.app)
    notification_service = NotificationService(db=db, ws_hub=ws_hub)

    if passenger_ids:
        await notification_service.notify_user(
            user_id=passenger_ids[0],   # primary
            user_ids=passenger_ids[1:], # rest
            title="Trip Cancelled",
            message="Your booked trip has been cancelled by the driver.",
            data={
                "trip_id": trip.id,
                "reason": cancellation_reason,
                "type": "TRIP_CANCELLED"
            },
            commit=False  # IMPORTANT: we already commit below
        )

    # ---------------------------
    # COMMIT
    # ---------------------------
    await db.commit()
    await db.refresh(trip)

    # ---------------------------
    # RESPONSE
    # ---------------------------
    return {
        "message": "Trip cancelled successfully.",
        "trip_id": trip.id,
        "status": trip.status.value,
        "cancellation_reason": trip.premature_end_reason,
        "notified_passengers": len(passenger_ids),

        "fine_summary": fine_result
    }