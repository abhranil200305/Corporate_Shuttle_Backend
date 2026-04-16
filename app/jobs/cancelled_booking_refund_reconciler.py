from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.database import AsyncSessionLocal
from app.db.schema import (
    BookingPayment,
    BookingPaymentStatus,
    BookingStatus,
    TripBooking,
)
from app.jobs.lease import (
    get_job_owner_id,
    release_job_lease,
    try_acquire_or_renew_job_lease,
)
from app.notifications.hub import WSHub
from app.notifications.service import NotificationService
from app.payments.service import RoutePayoutService

logger = logging.getLogger(__name__)

_JOB_NAME = "cancelled_booking_refund"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_batch_size() -> int:
    raw = os.getenv("CANCELLED_BOOKING_REFUND_BATCH_SIZE", "100").strip()
    try:
        value = int(raw)
    except ValueError:
        return 100
    return max(1, value)


def _get_interval_seconds() -> int:
    raw = os.getenv("CANCELLED_BOOKING_REFUND_INTERVAL_SECONDS", "60").strip()
    try:
        value = int(raw)
    except ValueError:
        return 60
    return max(5, value)


def _get_lease_seconds() -> int:
    default_value = max(_get_interval_seconds() + 60, 120)
    raw = os.getenv("CANCELLED_BOOKING_REFUND_LEASE_SECONDS", str(default_value)).strip()
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


async def _fetch_cancelled_booking_ids(
    db: AsyncSession,
    limit: int,
) -> list[str]:
    paid_payment_exists = (
        select(BookingPayment.id)
        .where(
            BookingPayment.booking_id == TripBooking.id,
            BookingPayment.status == BookingPaymentStatus.PAID,
            BookingPayment.razorpay_payment_id.is_not(None),
        )
        .exists()
    )

    stmt = (
        select(TripBooking.id)
        .where(
            TripBooking.booking_status == BookingStatus.CANCELLED,
            paid_payment_exists,
            or_(
                TripBooking.refund_retry_after.is_(None),
                TripBooking.refund_retry_after <= utcnow(),
            ),
        )
        .order_by(
            func.coalesce(
                TripBooking.refund_retry_after,
                TripBooking.created_at,
            ).asc(),
            TripBooking.created_at.asc(),
        )
        .limit(limit)
    )

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _notify_refund_outcome(
    *,
    db: AsyncSession,
    ws_hub: WSHub | None,
    booking: TripBooking,
    outcome: str,
) -> None:
    if outcome not in {
        "already_refunded_on_provider",
        "refund_already_processed_after_error",
        "refund_processed",
        "refund_processed_after_fetch",
    }:
        return

    notification_service = NotificationService(db=db, ws_hub=ws_hub)
    await notification_service.notify_user(
        user_id=booking.passenger_user_id,
        title="Refund processed",
        message="Your booking refund has been processed.",
        data={
            "booking_id": booking.id,
            "scheduled_trip_id": booking.scheduled_trip_id,
            "booking_status": booking.booking_status.value,
            "refresh": ["bookings_list", "booking_detail", "history"],
        },
    )


async def _process_booking_id(
    db: AsyncSession,
    booking_id: str,
    ws_hub: WSHub | None = None,
) -> str:
    service = RoutePayoutService(db, ws_hub=ws_hub)

    stmt = (
        select(TripBooking)
        .where(
            TripBooking.id == booking_id,
            TripBooking.booking_status == BookingStatus.CANCELLED,
        )
        .options(
            selectinload(TripBooking.payments),
            selectinload(TripBooking.transfer),
            selectinload(TripBooking.scheduled_trip),
        )
        .with_for_update(skip_locked=True)
    )

    result = await db.execute(stmt)
    booking = result.scalars().unique().first()

    if booking is None:
        await db.rollback()
        return "skip_missing_or_locked"

    try:
        outcome = await service.reconcile_cancelled_booking_refund(booking)
        await db.commit()

        await _notify_refund_outcome(
            db=db,
            ws_hub=ws_hub,
            booking=booking,
            outcome=outcome,
        )
        return outcome
    except Exception:
        await db.rollback()
        raise


async def reconcile_cancelled_booking_refunds_once(
    ws_hub: WSHub | None = None,
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
            "cancelled_booking_refund skipped: job lease not acquired owner_id=%s",
            owner_id,
        )
        return

    try:
        async with AsyncSessionLocal() as db:
            total_processed = 0

            while True:
                booking_ids = await _fetch_cancelled_booking_ids(db, batch_size)
                await db.rollback()

                if not booking_ids:
                    break

                for booking_id in booking_ids:
                    try:
                        outcome = await _process_booking_id(
                            db,
                            booking_id,
                            ws_hub=ws_hub,
                        )
                        total_processed += 1
                        logger.info(
                            "cancelled_booking_refund booking_id=%s outcome=%s",
                            booking_id,
                            outcome,
                        )
                    except Exception:
                        await db.rollback()
                        logger.exception(
                            "cancelled_booking_refund booking_id=%s outcome=error",
                            booking_id,
                        )

                if len(booking_ids) < batch_size:
                    break

            logger.info("cancelled_booking_refund done processed=%s", total_processed)
    finally:
        try:
            async with AsyncSessionLocal() as lease_db:
                await release_job_lease(
                    db=lease_db,
                    job_name=_JOB_NAME,
                    owner_id=owner_id,
                )
        except Exception:
            logger.exception("cancelled_booking_refund lease release failed")


async def cancelled_booking_refund_loop(
    ws_hub: WSHub | None = None,
) -> None:
    logger.info("cancelled_booking_refund loop started")
    try:
        await reconcile_cancelled_booking_refunds_once(ws_hub=ws_hub)

        while True:
            await asyncio.sleep(_seconds_until_next_minute())
            try:
                await reconcile_cancelled_booking_refunds_once(ws_hub=ws_hub)
            except Exception:
                logger.exception("cancelled_booking_refund tick failed")
    except asyncio.CancelledError:
        logger.info("cancelled_booking_refund loop cancelled")
        raise