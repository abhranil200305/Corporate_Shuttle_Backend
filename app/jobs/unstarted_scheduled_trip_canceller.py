from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app.db.database import AsyncSessionLocal, engine
from app.db.schema import ScheduledTrip, ScheduledTripStatus, TripBooking
from app.driver.trips.scheduled_trip import (
    build_unstarted_trip_cancellation_reason,
    cancel_scheduled_trip_and_bookings,
    get_trip_start_grace_minutes,
)

logger = logging.getLogger(__name__)


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


def _get_lock_key() -> int:
    raw = os.getenv("UNSTARTED_TRIP_CANCEL_LOCK_KEY", "82024003").strip()
    try:
        return int(raw)
    except ValueError:
        return 82024003


def _seconds_until_next_minute() -> float:
    now = utcnow()
    elapsed = now.second + (now.microsecond / 1_000_000)
    remaining = 60 - elapsed
    return remaining if remaining > 0 else 60.0


async def _fetch_overdue_scheduled_trip_ids(limit: int) -> list[str]:
    cutoff = utcnow() - timedelta(minutes=get_trip_start_grace_minutes())

    async with AsyncSessionLocal() as db:
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


async def _process_trip_id(trip_id: str) -> str:
    async with AsyncSessionLocal() as db:
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
            minutes=get_trip_start_grace_minutes()
        )

        if utcnow() <= late_start_deadline:
            await db.rollback()
            return "skip_not_yet_overdue"

        try:
            cancelled_booking_count = await cancel_scheduled_trip_and_bookings(
                db,
                trip,
                cancellation_reason=build_unstarted_trip_cancellation_reason(),
            )
            await db.commit()
            return f"cancelled_trip_bookings={cancelled_booking_count}"
        except Exception:
            await db.rollback()
            raise


async def cancel_unstarted_scheduled_trips_once() -> None:
    lock_key = _get_lock_key()
    batch_size = _get_batch_size()

    async with engine.begin() as conn:
        acquired = bool(
            (
                await conn.execute(
                    text("SELECT pg_try_advisory_xact_lock(:key)"),
                    {"key": lock_key},
                )
            ).scalar()
        )

        if not acquired:
            logger.info("unstarted_trip_cancel skipped: advisory lock not acquired")
            return

        total_processed = 0

        while True:
            trip_ids = await _fetch_overdue_scheduled_trip_ids(batch_size)
            if not trip_ids:
                break

            for trip_id in trip_ids:
                try:
                    outcome = await _process_trip_id(trip_id)
                    total_processed += 1
                    logger.info(
                        "unstarted_trip_cancel trip_id=%s outcome=%s",
                        trip_id,
                        outcome,
                    )
                except Exception:
                    logger.exception(
                        "unstarted_trip_cancel trip_id=%s outcome=error",
                        trip_id,
                    )

            if len(trip_ids) < batch_size:
                break

        logger.info("unstarted_trip_cancel done processed=%s", total_processed)


async def unstarted_trip_cancel_loop() -> None:
    logger.info("unstarted_trip_cancel loop started")
    try:
        await cancel_unstarted_scheduled_trips_once()

        while True:
            await asyncio.sleep(_seconds_until_next_minute())
            try:
                await cancel_unstarted_scheduled_trips_once()
            except Exception:
                logger.exception("unstarted_trip_cancel tick failed")
    except asyncio.CancelledError:
        logger.info("unstarted_trip_cancel loop cancelled")
        raise