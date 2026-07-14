# app/driver/trips/cancel_trip.py

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.auth.dependencies import get_current_user
from app.db.database import get_async_session
from app.db.schema import (
    BookingStatus,
    ScheduledTrip,
    ScheduledTripStatus,
    TripBooking,
    User,
)
from app.notifications.service import NotificationService
from app.payments.fine_register_service import FineRegisterService
from app.realtime.events import get_api_refresh_hub, publish_trip_event

router = APIRouter(
    prefix="/trips",
    tags=["Driver Trips"]
)


def _get_ws_hub_from_app(app):
    return getattr(app.state, "ws_hub", None)


def _to_utc(dt):
    if dt is None:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)

    return dt


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
            ScheduledTrip.driver_user_id == current_driver.id,
        )
    )

    trip: ScheduledTrip | None = result.scalars().first()

    if not trip:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trip not found or you are not the assigned driver.",
        )

    # ---------------------------
    # STATUS CHECK
    # ---------------------------
    if trip.status != ScheduledTripStatus.SCHEDULED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel trip with status '{trip.status.value}'.",
        )

    # ---------------------------
    # TIME CHECK
    # RULE:
    # allow cancel BEFORE start
    # allow cancel AFTER planned_end_at
    # block DURING active trip window
    # ---------------------------
    now_utc = datetime.now(timezone.utc)

    planned_start_at = _to_utc(trip.planned_start_at)
    planned_end_at = _to_utc(trip.planned_end_at)

    if (
        planned_start_at
        and planned_end_at
        and planned_start_at <= now_utc < planned_end_at
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Trip cannot be cancelled after start "
                "until planned end time is crossed."
            ),
        )

    # ---------------------------
    # FETCH ACTIVE BOOKINGS
    # ---------------------------
    bookings_result = await db.execute(
        select(TripBooking).where(
            TripBooking.scheduled_trip_id == trip.id,
            TripBooking.booking_status.in_(
                [
                    BookingStatus.PENDING_PAYMENT,
                    BookingStatus.BOOKED,
                    BookingStatus.BOARDED,
                ]
            ),
        )
    )

    active_bookings = bookings_result.scalars().all()

    passenger_ids = list(
        {
            booking.passenger_user_id
            for booking in active_bookings
        }
    )

    # ---------------------------
    # CANCEL TRIP
    # ---------------------------
    trip.status = ScheduledTripStatus.CANCELLED
    normalized_reason = (
        cancellation_reason.strip()
        if cancellation_reason and cancellation_reason.strip()
        else "Trip cancelled by driver."
    )
    trip.cancellation_reason = normalized_reason
    trip.cancelled_at = now_utc
    trip.cancellation_source = "driver"
    trip.cancelled_by_user_id = current_driver.id

    # ---------------------------
    # CANCEL BOOKINGS
    # ---------------------------
    await db.execute(
        update(TripBooking)
        .where(
            TripBooking.scheduled_trip_id == trip.id,
            TripBooking.booking_status.in_(
                [
                    BookingStatus.PENDING_PAYMENT,
                    BookingStatus.BOOKED,
                    BookingStatus.BOARDED,
                ]
            ),
        )
        .values(
            booking_status=BookingStatus.CANCELLED,
            cancelled_at=now_utc,
            cancellation_reason=normalized_reason,
            cancellation_source="driver",
            cancelled_by_user_id=current_driver.id,
            refund_retry_after=now_utc,
        )
    )

    # ---------------------------
    # REGISTER FINES
    # ---------------------------
    fine_service = FineRegisterService(db)

    fine_result = (
        await fine_service.register_driver_trip_cancellation_fines(
            scheduled_trip_id=trip.id,
            driver_user_id=current_driver.id,
            cancellation_reason=normalized_reason,
            occurred_at=now_utc,
        )
    )

    # ---------------------------
    # SEND NOTIFICATIONS
    # ---------------------------
    ws_hub = _get_ws_hub_from_app(request.app)

    notification_service = NotificationService(
        db=db,
        ws_hub=ws_hub,
    )

    if passenger_ids:
        cancellation_metadata = {
            "cancelled_at": now_utc.isoformat(),
            "reason": normalized_reason,
            "source": "driver",
            "cancelled_by_user_id": current_driver.id,
        }
        await notification_service.notify_user(
            user_id=passenger_ids[0],
            user_ids=passenger_ids[1:],
            title="Trip Cancelled",
            message="Your booked trip has been cancelled by the driver.",
            data={
                "trip_id": trip.id,
                "reason": normalized_reason,
                "type": "TRIP_CANCELLED",
                "cancellation_metadata": cancellation_metadata,
            },
            commit=False,
        )

    # ---------------------------
    # COMMIT
    # ---------------------------
    await db.commit()

    await db.refresh(trip)

    refresh_hub = get_api_refresh_hub(request.app)
    await refresh_hub.cancel_scheduled(f"trip-start-{trip.id}")
    await publish_trip_event(
        refresh_hub,
        db,
        event="trip.cancelled",
        trip_id=trip.id,
        data={
            "route_id": trip.route_id,
            "reason": normalized_reason,
            "cancellation_metadata": cancellation_metadata,
        },
        broadcast_catalog=True,
    )

    # ---------------------------
    # RESPONSE
    # ---------------------------
    return {
        "message": "Trip cancelled successfully.",
        "trip_id": trip.id,
        "status": trip.status.value,
        "cancellation_reason": trip.cancellation_reason,
        "cancellation_metadata": cancellation_metadata,
        "notified_passengers": len(passenger_ids),
        "fine_summary": fine_result,
    }
