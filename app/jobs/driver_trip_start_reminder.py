from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.database import AsyncSessionLocal
from app.db.schema import ScheduledTrip, ScheduledTripStatus, UserNotification
from app.jobs.lease import (
    get_job_owner_id,
    release_job_lease,
    try_acquire_or_renew_job_lease,
)
from app.notifications.hub import WSHub
from app.notifications.service import NotificationService

logger = logging.getLogger(__name__)

_JOB_NAME = "driver_trip_reminder"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_interval_seconds() -> int:
    raw = os.getenv("DRIVER_TRIP_REMINDER_INTERVAL_SECONDS", "60").strip()
    try:
        value = int(raw)
    except ValueError:
        return 60
    return max(5, value)


def _get_lease_seconds() -> int:
    default_value = max(_get_interval_seconds() + 60, 120)
    raw = os.getenv("DRIVER_TRIP_REMINDER_LEASE_SECONDS", str(default_value)).strip()
    try:
        value = int(raw)
    except ValueError:
        return default_value
    return max(30, value)


def _seconds_until_next_minute() -> float:
    now = utcnow()
    elapsed = now.second + (now.microsecond / 1_000_000)
    remaining = 60 - elapsed
    return remaining if remaining > 0 else 60.0


def _get_window_seconds() -> int:
    return max(_get_interval_seconds() * 2, 120)


def _serialize_data(data: dict[str, Any]) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def _build_notification_payload(
    trip: ScheduledTrip,
    *,
    reminder_key: str,
    title: str,
    message: str,
) -> tuple[str, str, dict[str, Any], str]:
    data = {
        "scheduled_trip_id": trip.id,
        "driver_user_id": trip.driver_user_id,
        "planned_start_at": trip.planned_start_at.isoformat(),
        "reminder_key": reminder_key,
        "refresh": ["driver_trips", "current_trip"],
    }
    return title, message, data, _serialize_data(data)


async def _notification_already_sent(
    db: AsyncSession,
    *,
    user_id: str,
    title: str,
    message: str,
    data_json: str,
) -> bool:
    stmt = (
        select(UserNotification.id)
        .where(
            UserNotification.user_id == user_id,
            UserNotification.title == title,
            UserNotification.message == message,
            UserNotification.data_json == data_json,
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


async def _fetch_trip_ids_for_window(
    db: AsyncSession,
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[str]:
    stmt = (
        select(ScheduledTrip.id)
        .where(
            ScheduledTrip.status == ScheduledTripStatus.SCHEDULED,
            ScheduledTrip.actual_start_at.is_(None),
            ScheduledTrip.planned_start_at >= window_start,
            ScheduledTrip.planned_start_at <= window_end,
        )
        .order_by(ScheduledTrip.planned_start_at.asc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _send_if_due(
    *,
    db: AsyncSession,
    ws_hub: WSHub | None,
    trip: ScheduledTrip,
    reminder_key: str,
    title: str,
    message: str,
) -> str:
    title_value, message_value, data, data_json = _build_notification_payload(
        trip,
        reminder_key=reminder_key,
        title=title,
        message=message,
    )

    already_sent = await _notification_already_sent(
        db,
        user_id=trip.driver_user_id,
        title=title_value,
        message=message_value,
        data_json=data_json,
    )
    if already_sent:
        return "skip_already_sent"

    notification_service = NotificationService(db=db, ws_hub=ws_hub)
    await notification_service.notify_user(
        user_id=trip.driver_user_id,
        title=title_value,
        message=message_value,
        data=data,
    )
    return "sent"


async def _process_trip_id(
    trip_id: str,
    *,
    ws_hub: WSHub | None,
    reminder_key: str,
    title: str,
    message: str,
) -> str:
    async with AsyncSessionLocal() as db:
        stmt = (
            select(ScheduledTrip)
            .where(
                ScheduledTrip.id == trip_id,
                ScheduledTrip.status == ScheduledTripStatus.SCHEDULED,
                ScheduledTrip.actual_start_at.is_(None),
            )
            .options(selectinload(ScheduledTrip.route))
        )
        result = await db.execute(stmt)
        trip = result.scalar_one_or_none()

        if trip is None:
            await db.rollback()
            return "skip_missing_or_ineligible"

        try:
            outcome = await _send_if_due(
                db=db,
                ws_hub=ws_hub,
                trip=trip,
                reminder_key=reminder_key,
                title=title,
                message=message,
            )
            return outcome
        except Exception:
            await db.rollback()
            raise


async def _run_reminder_window(
    db: AsyncSession,
    *,
    ws_hub: WSHub | None,
    reminder_key: str,
    offset_from_start: timedelta,
    title: str,
    message: str,
) -> None:
    now = utcnow()
    window_seconds = _get_window_seconds()

    target_time = now - offset_from_start
    window_start = target_time - timedelta(seconds=window_seconds)
    window_end = target_time

    trip_ids = await _fetch_trip_ids_for_window(
        db,
        window_start=window_start,
        window_end=window_end,
    )
    await db.rollback()

    for trip_id in trip_ids:
        try:
            outcome = await _process_trip_id(
                trip_id=trip_id,
                ws_hub=ws_hub,
                reminder_key=reminder_key,
                title=title,
                message=message,
            )
            logger.info(
                "driver_trip_reminder reminder=%s trip_id=%s outcome=%s",
                reminder_key,
                trip_id,
                outcome,
            )
        except Exception:
            await db.rollback()
            logger.exception(
                "driver_trip_reminder reminder=%s trip_id=%s outcome=error",
                reminder_key,
                trip_id,
            )


async def send_driver_trip_reminders_once(
    ws_hub: WSHub | None = None,
) -> None:
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
            "driver_trip_reminder skipped: job lease not acquired owner_id=%s",
            owner_id,
        )
        return

    try:
        async with AsyncSessionLocal() as db:
            await _run_reminder_window(
                db,
                ws_hub=ws_hub,
                reminder_key="driver_trip_prestart_15m",
                offset_from_start=timedelta(minutes=-15),
                title="Trip starts in 15 minutes",
                message="Your scheduled trip starts in 15 minutes.",
            )

            await _run_reminder_window(
                db,
                ws_hub=ws_hub,
                reminder_key="driver_trip_poststart_5m",
                offset_from_start=timedelta(minutes=5),
                title="Trip start overdue by 5 minutes",
                message="Your scheduled trip was due to start 5 minutes ago. Please start it now.",
            )

            await _run_reminder_window(
                db,
                ws_hub=ws_hub,
                reminder_key="driver_trip_poststart_10m",
                offset_from_start=timedelta(minutes=10),
                title="Trip start overdue by 10 minutes",
                message="Your scheduled trip was due to start 10 minutes ago. Please start it immediately. Auto-cancellation happens soon.",
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
            logger.exception("driver_trip_reminder lease release failed")


async def driver_trip_reminder_loop(
    ws_hub: WSHub | None = None,
) -> None:
    logger.info("driver_trip_reminder loop started")
    try:
        await send_driver_trip_reminders_once(ws_hub=ws_hub)

        while True:
            await asyncio.sleep(_seconds_until_next_minute())
            try:
                await send_driver_trip_reminders_once(ws_hub=ws_hub)
            except Exception:
                logger.exception("driver_trip_reminder tick failed")
    except asyncio.CancelledError:
        logger.info("driver_trip_reminder loop cancelled")
        raise