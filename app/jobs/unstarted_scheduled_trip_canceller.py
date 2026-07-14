from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.database import AsyncSessionLocal
from app.db.schema import (
    BookingPaymentStatus,
    BookingStatus,
    ScheduledTrip,
    ScheduledTripStatus,
    TripBooking,
)
from app.jobs.lease import (
    get_job_owner_id,
    release_job_lease,
    try_acquire_or_renew_job_lease,
)
from app.notifications.hub import WSHub
from app.notifications.service import NotificationService
from app.realtime.events import publish_trip_event
from app.realtime.hub import APIRefreshHub

logger = logging.getLogger(__name__)

_JOB_NAME = "unstarted_trip_cancel"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_batch_size() -> int:
    raw = os.getenv("UNSTARTED_TRIP_CANCEL_BATCH_SIZE", "100").strip()
    try:
        value = int(raw)
    except ValueError:
        return 100
    return max(1, value)


def _get_interval_seconds() -> int:
    raw = os.getenv("UNSTARTED_TRIP_CANCEL_INTERVAL_SECONDS", "60").strip()
    try:
        value = int(raw)
    except ValueError:
        return 60
    return max(5, value)


def _get_lease_seconds() -> int:
    default_value = max(_get_interval_seconds() + 60, 120)
    raw = os.getenv("UNSTARTED_TRIP_CANCEL_LEASE_SECONDS", str(default_value)).strip()
    try:
        value = int(raw)
    except ValueError:
        return default_value
    return max(30, value)


def _get_start_grace_minutes() -> int:
    raw = os.getenv("TRIP_START_GRACE_MINUTES", "15").strip()
    try:
        value = int(raw)
    except ValueError:
        return 15
    return max(1, value)


def _seconds_until_next_minute() -> float:
    now = utcnow()
    elapsed = now.second + (now.microsecond / 1_000_000)
    remaining = 60 - elapsed
    return remaining if remaining > 0 else 60.0


def _build_cancellation_reason() -> str:
    grace_minutes = _get_start_grace_minutes()
    return (
        f"Auto-cancelled because the driver did not start the trip within "
        f"{grace_minutes} minutes after the planned start time."
    )


async def _fetch_overdue_trip_ids(
    db: AsyncSession,
    limit: int,
) -> list[str]:
    cutoff = utcnow() - timedelta(minutes=_get_start_grace_minutes())

    stmt = (
        select(ScheduledTrip.id)
        .where(
            ScheduledTrip.status == ScheduledTripStatus.SCHEDULED,
            ScheduledTrip.actual_start_at.is_(None),
            ScheduledTrip.planned_start_at <= cutoff,
        )
        .order_by(ScheduledTrip.planned_start_at.asc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _cancel_trip_and_bookings(
    db: AsyncSession,
    trip_id: str,
    ws_hub: WSHub | None = None,
    api_refresh_hub: APIRefreshHub | None = None,
) -> str:
    stmt = (
        select(ScheduledTrip)
        .where(
            ScheduledTrip.id == trip_id,
            ScheduledTrip.status == ScheduledTripStatus.SCHEDULED,
            ScheduledTrip.actual_start_at.is_(None),
        )
        .options(
            selectinload(ScheduledTrip.bookings).selectinload(TripBooking.payments),
        )
        .with_for_update(skip_locked=True)
    )
    result = await db.execute(stmt)
    trip = result.scalar_one_or_none()

    if trip is None:
        await db.rollback()
        return "skip_missing_or_locked"

    late_start_deadline = trip.planned_start_at + timedelta(
        minutes=_get_start_grace_minutes()
    )

    if utcnow() <= late_start_deadline:
        await db.rollback()
        return "skip_not_yet_overdue"

    current_time = utcnow()

    trip.status = ScheduledTripStatus.CANCELLED
    trip.cancellation_reason = _build_cancellation_reason()
    trip.cancelled_at = current_time
    trip.cancellation_source = "system"
    trip.cancelled_by_user_id = None
    db.add(trip)

    affected_bookings: list[TripBooking] = []
    cancelled_booking_count = 0
    failed_created_payment_count = 0

    for booking in trip.bookings:
        if booking.booking_status in {
            BookingStatus.CANCELLED,
            BookingStatus.COMPLETED,
            BookingStatus.MISSED,
        }:
            continue

        booking.booking_status = BookingStatus.CANCELLED
        booking.cancelled_at = booking.cancelled_at or current_time
        booking.cancellation_reason = trip.cancellation_reason
        booking.cancellation_source = "system"
        booking.cancelled_by_user_id = None
        booking.payment_hold_expires_at = None
        booking.refund_retry_after = None
        db.add(booking)

        for payment in booking.payments:
            if payment.status == BookingPaymentStatus.CREATED:
                payment.status = BookingPaymentStatus.FAILED
                db.add(payment)
                failed_created_payment_count += 1

        affected_bookings.append(booking)
        cancelled_booking_count += 1

    await db.commit()

    notification_service = NotificationService(db=db, ws_hub=ws_hub)

    for booking in affected_bookings:
        await notification_service.notify_user(
            user_id=booking.passenger_user_id,
            title="Trip cancelled",
            message=trip.cancellation_reason or "Your scheduled trip was cancelled.",
            data={
                "booking_id": booking.id,
                "scheduled_trip_id": booking.scheduled_trip_id,
                "booking_status": booking.booking_status.value,
                "cancellation_metadata": {
                    "cancelled_at": current_time.isoformat(),
                    "reason": trip.cancellation_reason,
                    "source": "system",
                    "cancelled_by_user_id": None,
                },
                "refresh": ["bookings_list", "booking_detail", "history"],
            },
        )

    await notification_service.notify_user(
        user_id=trip.driver_user_id,
        title="Trip auto-cancelled",
        message=trip.cancellation_reason or "The trip was auto-cancelled.",
        data={
            "scheduled_trip_id": trip.id,
            "trip_status": trip.status.value,
            "cancellation_metadata": {
                "cancelled_at": current_time.isoformat(),
                "reason": trip.cancellation_reason,
                "source": "system",
                "cancelled_by_user_id": None,
            },
            "refresh": ["driver_trips"],
        },
    )

    if api_refresh_hub is not None:
        await publish_trip_event(
            api_refresh_hub,
            db,
            event="trip.cancelled",
            trip_id=trip.id,
            data={
                "route_id": trip.route_id,
                "reason": trip.cancellation_reason,
                "automatic": True,
                "cancellation_metadata": {
                    "cancelled_at": current_time.isoformat(),
                    "reason": trip.cancellation_reason,
                    "source": "system",
                    "cancelled_by_user_id": None,
                },
            },
            broadcast_catalog=True,
        )

    return (
        f"cancelled_trip"
        f"_bookings={cancelled_booking_count}"
        f"_failed_created_payments={failed_created_payment_count}"
    )


async def cancel_unstarted_trips_once(
    ws_hub: WSHub | None = None,
    api_refresh_hub: APIRefreshHub | None = None,
) -> None:
    batch_size = _get_batch_size()
    lease_seconds = _get_lease_seconds()
    owner_id = get_job_owner_id()

    async with AsyncSessionLocal() as lease_db:
        acquired = await try_acquire_or_renew_job_lease(
            db=lease_db,
            job_name=_JOB_NAME,
            owner_id=owner_id,
            lease_seconds=lease_seconds,
        )

    if not acquired:
        logger.info(
            "unstarted_trip_cancel skipped: job lease not acquired owner_id=%s",
            owner_id,
        )
        return

    try:
        async with AsyncSessionLocal() as db:
            total_processed = 0

            while True:
                trip_ids = await _fetch_overdue_trip_ids(db, batch_size)
                await db.rollback()

                if not trip_ids:
                    break

                for trip_id in trip_ids:
                    try:
                        outcome = await _cancel_trip_and_bookings(
                            db,
                            trip_id,
                            ws_hub=ws_hub,
                            api_refresh_hub=api_refresh_hub,
                        )
                        total_processed += 1
                        logger.info(
                            "unstarted_trip_cancel trip_id=%s outcome=%s",
                            trip_id,
                            outcome,
                        )
                    except Exception:
                        await db.rollback()
                        logger.exception(
                            "unstarted_trip_cancel trip_id=%s outcome=error",
                            trip_id,
                        )

                if len(trip_ids) < batch_size:
                    break

            logger.info("unstarted_trip_cancel done processed=%s", total_processed)
    finally:
        try:
            async with AsyncSessionLocal() as lease_db:
                await release_job_lease(
                    db=lease_db,
                    job_name=_JOB_NAME,
                    owner_id=owner_id,
                )
        except Exception:
            logger.exception("unstarted_trip_cancel lease release failed")


async def unstarted_trip_cancel_loop(
    ws_hub: WSHub | None = None,
    api_refresh_hub: APIRefreshHub | None = None,
) -> None:
    logger.info("unstarted_trip_cancel loop started")
    try:
        await cancel_unstarted_trips_once(
            ws_hub=ws_hub,
            api_refresh_hub=api_refresh_hub,
        )

        while True:
            await asyncio.sleep(_seconds_until_next_minute())
            try:
                await cancel_unstarted_trips_once(
                    ws_hub=ws_hub,
                    api_refresh_hub=api_refresh_hub,
                )
            except Exception:
                logger.exception("unstarted_trip_cancel tick failed")
    except asyncio.CancelledError:
        logger.info("unstarted_trip_cancel loop cancelled")
        raise
