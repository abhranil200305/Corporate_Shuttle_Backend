from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.database import AsyncSessionLocal, engine
from app.db.schema import (
    BookingPayment,
    BookingPaymentStatus,
    BookingStatus,
    TripBooking,
)
from app.notifications.hub import WSHub
from app.notifications.service import NotificationService
from app.payments.service import RoutePayoutService

logger = logging.getLogger(__name__)


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


def _get_lock_key() -> int:
    raw = os.getenv("CANCELLED_BOOKING_REFUND_LOCK_KEY", "82024002").strip()
    try:
        return int(raw)
    except ValueError:
        return 82024002


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
    db,
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
    lock_key = _get_lock_key()
    batch_size = _get_batch_size()

    async with engine.connect() as conn:
        acquired = bool(
            (
                await conn.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": lock_key},
                )
            ).scalar()
        )

        if not acquired:
            logger.info("cancelled_booking_refund skipped: advisory lock not acquired")
            return

        try:
            async with AsyncSessionLocal(bind=conn) as db:
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
                await conn.rollback()
            except Exception:
                pass

            try:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": lock_key},
                )
                await conn.commit()
            except Exception:
                logger.exception("cancelled_booking_refund advisory unlock failed")


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