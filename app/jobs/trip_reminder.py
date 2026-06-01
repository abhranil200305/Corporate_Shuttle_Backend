# app/jobs/trip_reminder.py

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import AsyncSessionLocal
from app.db.schema import (
    ScheduledTrip,
    ScheduledTripStatus,
    TripBooking,
    BookingStatus,
)
from app.jobs.lease import (
    get_job_owner_id,
    release_job_lease,
    try_acquire_or_renew_job_lease,
)
from app.notifications.hub import WSHub
from app.notifications.service import NotificationService

logger = logging.getLogger(__name__)

_JOB_NAME = "trip_reminder"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_interval_seconds() -> int:
    raw = os.getenv("TRIP_REMINDER_INTERVAL_SECONDS", "60").strip()

    try:
        value = int(raw)
    except ValueError:
        return 60

    return max(30, value)


def _get_lease_seconds() -> int:
    default_value = max(_get_interval_seconds() + 60, 120)

    raw = os.getenv(
        "TRIP_REMINDER_LEASE_SECONDS",
        str(default_value),
    ).strip()

    try:
        value = int(raw)
    except ValueError:
        return default_value

    return max(60, value)


def _seconds_until_next_minute() -> float:
    now = utcnow()
    elapsed = now.second + (now.microsecond / 1_000_000)

    remaining = 60 - elapsed

    return remaining if remaining > 0 else 60.0


def _get_reminder_message(minutes: int) -> tuple[str, str]:
    return (
        "Upcoming Trip",
        f"Your trip will start in {minutes} minutes.",
    )


async def _send_trip_notification(
    *,
    db: AsyncSession,
    ws_hub: WSHub | None,
    trip: ScheduledTrip,
    minutes: int,
) -> None:
    notification_service = NotificationService(
        db=db,
        ws_hub=ws_hub,
    )

    title, message = _get_reminder_message(minutes)

    booking_result = await db.execute(
        select(TripBooking).where(
            TripBooking.scheduled_trip_id == trip.id,
            TripBooking.booking_status == BookingStatus.BOOKED,
        )
    )

    bookings = booking_result.scalars().all()

    for booking in bookings:
        try:
            await notification_service.notify_user(
                user_id=booking.passenger_user_id,
                title=title,
                message=message,
                data={
                    "trip_id": trip.id,
                    "minutes_remaining": minutes,
                    "refresh": [
                        "bookings_list",
                        "booking_detail",
                    ],
                },
            )
        except Exception:
            logger.exception(
                "trip_reminder notification failed "
                "trip_id=%s passenger_id=%s",
                trip.id,
                booking.passenger_user_id,
            )


async def reconcile_trip_reminders_once(
    ws_hub: WSHub | None = None,
) -> None:
    owner_id = get_job_owner_id()

    acquired = False

    async with AsyncSessionLocal() as lease_db:
        acquired = await try_acquire_or_renew_job_lease(
            db=lease_db,
            job_name=_JOB_NAME,
            owner_id=owner_id,
            lease_seconds=_get_lease_seconds(),
        )

    if not acquired:
        return

    try:
        now = utcnow()

        async with AsyncSessionLocal() as db:

            result = await db.execute(
                select(ScheduledTrip).where(
                    ScheduledTrip.status
                    == ScheduledTripStatus.SCHEDULED
                )
            )

            trips = result.scalars().all()

            for trip in trips:

                seconds_remaining = (
                    trip.planned_start_at - now
                ).total_seconds()

                minutes_remaining = int(
                    seconds_remaining // 60
                )

                #
                # 15 minute reminder
                #
                if 14 <= minutes_remaining <= 15:
                    await _send_trip_notification(
                        db=db,
                        ws_hub=ws_hub,
                        trip=trip,
                        minutes=15,
                    )

                #
                # 10 minute reminder
                #
                elif 9 <= minutes_remaining <= 10:
                    await _send_trip_notification(
                        db=db,
                        ws_hub=ws_hub,
                        trip=trip,
                        minutes=10,
                    )

                #
                # 5 minute reminder
                #
                elif 4 <= minutes_remaining <= 5:
                    await _send_trip_notification(
                        db=db,
                        ws_hub=ws_hub,
                        trip=trip,
                        minutes=5,
                    )

    finally:
        try:
            async with AsyncSessionLocal() as lease_db:
                await release_job_lease(
                    db=lease_db,
                    job_name=_JOB_NAME,
                    owner_id=owner_id,
                )
        except Exception:
            logger.exception(
                "trip_reminder lease release failed"
            )


async def trip_reminder_loop(
    ws_hub: WSHub | None = None,
) -> None:
    logger.info("trip_reminder loop started")

    try:
        await reconcile_trip_reminders_once(
            ws_hub=ws_hub,
        )

        while True:
            await asyncio.sleep(
                _seconds_until_next_minute()
            )

            try:
                await reconcile_trip_reminders_once(
                    ws_hub=ws_hub,
                )
            except Exception:
                logger.exception(
                    "trip_reminder tick failed"
                )

    except asyncio.CancelledError:
        logger.info(
            "trip_reminder loop cancelled"
        )
        raise