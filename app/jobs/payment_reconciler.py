from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.database import AsyncSessionLocal
from app.db.schema import (
    BookingPaymentStatus,
    BookingSession,
    BookingSessionPayment,
    BookingSessionStatus,
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
from app.passenger.service import PassengerService
from app.realtime.events import publish_booking_change
from app.realtime.hub import APIRefreshHub

logger = logging.getLogger(__name__)

_JOB_NAME = "payment_reconcile"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_batch_size() -> int:
    raw = os.getenv("PAYMENT_RECONCILE_BATCH_SIZE", "100").strip()
    try:
        value = int(raw)
    except ValueError:
        return 100
    return max(1, value)


def _get_interval_seconds() -> int:
    raw = os.getenv("PAYMENT_RECONCILE_INTERVAL_SECONDS", "60").strip()
    try:
        value = int(raw)
    except ValueError:
        return 60
    return max(5, value)


def _get_lease_seconds() -> int:
    default_value = max(_get_interval_seconds() + 60, 120)
    raw = os.getenv("PAYMENT_RECONCILE_LEASE_SECONDS", str(default_value)).strip()
    try:
        value = int(raw)
    except ValueError:
        return default_value
    return max(30, value)


def _get_closed_session_lookback_hours() -> int:
    raw = os.getenv(
        "PAYMENT_RECONCILE_CLOSED_SESSION_LOOKBACK_HOURS",
        "24",
    ).strip()
    try:
        value = int(raw)
    except ValueError:
        return 24
    return max(1, value)


def _seconds_until_next_minute() -> float:
    now = utcnow()
    elapsed = now.second + (now.microsecond / 1_000_000)
    remaining = 60 - elapsed
    return remaining if remaining > 0 else 60.0


async def _fetch_pending_booking_ids(
    db: AsyncSession,
    limit: int,
) -> list[str]:
    stmt = (
        select(TripBooking.id)
        .where(
            TripBooking.booking_status == BookingStatus.PENDING_PAYMENT,
            TripBooking.booking_session_id.is_(None),
        )
        .order_by(TripBooking.created_at.asc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _fetch_pending_booking_session_ids(
    db: AsyncSession,
    limit: int,
) -> list[str]:
    stmt = (
        select(BookingSession.id)
        .where(BookingSession.status == BookingSessionStatus.PENDING_PAYMENT)
        .order_by(BookingSession.created_at.asc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _fetch_closed_booking_session_ids(
    db: AsyncSession,
    limit: int,
) -> list[str]:
    cutoff = utcnow() - timedelta(
        hours=_get_closed_session_lookback_hours()
    )
    payment_needs_late_capture_check = (
        select(BookingSessionPayment.id)
        .where(
            BookingSessionPayment.booking_session_id == BookingSession.id,
            or_(
                BookingSessionPayment.status.in_(
                    (
                        BookingPaymentStatus.CREATED,
                        BookingPaymentStatus.FAILED,
                    )
                ),
                (
                    BookingSessionPayment.status == BookingPaymentStatus.PAID
                )
                & BookingSessionPayment.refund_requested_at.is_(None),
            ),
        )
        .exists()
    )
    closed_at = func.coalesce(
        BookingSession.expired_at,
        BookingSession.cancelled_at,
        BookingSession.updated_at,
    )
    stmt = (
        select(BookingSession.id)
        .where(
            BookingSession.status.in_(
                (
                    BookingSessionStatus.EXPIRED,
                    BookingSessionStatus.CANCELLED,
                )
            ),
            closed_at >= cutoff,
            payment_needs_late_capture_check,
        )
        # Failed/uncaptured orders remain eligible until the lookback expires.
        # Random sampling prevents a fixed first page from starving other
        # recently closed sessions when the candidate count exceeds the batch.
        .order_by(func.random())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _notify_payment_reconcile_outcome(
    *,
    db: AsyncSession,
    ws_hub: WSHub | None,
    booking: TripBooking,
    outcome: str,
) -> None:
    notification_service = NotificationService(db=db, ws_hub=ws_hub)

    if outcome in {
        "promoted_local_paid",
        "booked_from_captured_payment",
        "booked_after_capture",
    }:
        await notification_service.notify_user(
            user_id=booking.passenger_user_id,
            title="Payment verified",
            message="Your booking is confirmed.",
            data={
                "booking_id": booking.id,
                "scheduled_trip_id": booking.scheduled_trip_id,
                "booking_status": booking.booking_status.value,
                "refresh": ["bookings_list", "booking_detail", "current_booking"],
            },
        )
        return

    if outcome in {
        "paid_after_hold_expiry",
        "captured_after_hold_expiry",
    }:
        await notification_service.notify_user(
            user_id=booking.passenger_user_id,
            title="Booking cancelled",
            message="Your payment arrived after the hold expired, so the booking stayed cancelled.",
            data={
                "booking_id": booking.id,
                "scheduled_trip_id": booking.scheduled_trip_id,
                "booking_status": booking.booking_status.value,
                "refresh": ["bookings_list", "booking_detail", "history"],
            },
        )
        return

    if outcome.startswith("expired_"):
        await notification_service.notify_user(
            user_id=booking.passenger_user_id,
            title="Booking cancelled",
            message="Your payment window expired, so the seat was released.",
            data={
                "booking_id": booking.id,
                "scheduled_trip_id": booking.scheduled_trip_id,
                "booking_status": booking.booking_status.value,
                "refresh": ["bookings_list", "booking_detail", "history"],
            },
        )

async def _broadcast_payment_reconcile_seatmap_outcome(
    *,
    booking: TripBooking,
    outcome: str,
) -> None:
    seat_releasing_outcomes = {
        "paid_after_hold_expiry",
        "captured_after_hold_expiry",
    }

    if not outcome.startswith("expired_") and outcome not in seat_releasing_outcomes:
        return

    try:
        from app.passenger.seatmap_ws import broadcast_all_seatmap_snapshots_for_trip

        await broadcast_all_seatmap_snapshots_for_trip(
            scheduled_trip_id=booking.scheduled_trip_id,
            reason="payment_hold_expired",
        )
    except Exception:
        logger.exception(
            "payment_reconcile_seatmap_broadcast_failed booking_id=%s outcome=%s",
            booking.id,
            outcome,
        )

async def _process_booking_id(
    db: AsyncSession,
    booking_id: str,
    ws_hub: WSHub | None = None,
    api_refresh_hub: APIRefreshHub | None = None,
) -> str:
    service = PassengerService(db, ws_hub=ws_hub)

    stmt = (
        select(TripBooking)
        .where(
            TripBooking.id == booking_id,
            TripBooking.booking_status == BookingStatus.PENDING_PAYMENT,
        )
        .options(selectinload(TripBooking.payments))
        .with_for_update(skip_locked=True)
    )
    result = await db.execute(stmt)
    booking = result.scalars().unique().first()

    if booking is None:
        await db.rollback()
        return "skip_missing_or_locked"

    try:
        outcome = await service.reconcile_pending_booking_payment(booking)
        await db.commit()

        await _notify_payment_reconcile_outcome(
            db=db,
            ws_hub=ws_hub,
            booking=booking,
            outcome=outcome,
        )

        await _broadcast_payment_reconcile_seatmap_outcome(
            booking=booking,
            outcome=outcome,
        )

        unchanged_outcomes = {
            "skip_non_pending",
            "pending_without_local_payment",
            "pending_without_provider_payment",
            "pending_authorized_without_payment_id",
        }
        if api_refresh_hub is not None and outcome not in unchanged_outcomes:
            await publish_booking_change(
                api_refresh_hub,
                db,
                trip_id=booking.scheduled_trip_id,
                passenger_user_id=booking.passenger_user_id,
                reason=f"payment_reconcile:{outcome}",
                booking_id=booking.id,
                booking_session_id=booking.booking_session_id,
                route_id=booking.route_id,
            )

        return outcome
    except Exception:
        await db.rollback()
        raise


async def _process_booking_session_id(
    db: AsyncSession,
    booking_session_id: str,
    ws_hub: WSHub | None = None,
    api_refresh_hub: APIRefreshHub | None = None,
) -> str:
    service = PassengerService(db, ws_hub=ws_hub)
    stmt = (
        select(BookingSession)
        .where(
            BookingSession.id == booking_session_id,
            BookingSession.status == BookingSessionStatus.PENDING_PAYMENT,
        )
        .with_for_update(skip_locked=True)
    )
    result = await db.execute(stmt)
    booking_session = result.scalars().unique().first()

    if booking_session is None:
        await db.rollback()
        return "skip_missing_or_locked"

    try:
        payments = await service._list_booking_session_payments_for_update(
            booking_session.id
        )
        bookings = await service._list_booking_session_bookings_for_update(
            booking_session.id
        )
        outcome = await service.reconcile_pending_booking_session_payment(
            booking_session,
            bookings=bookings,
            payments=payments,
        )
        confirmed_outcomes = {
            "promoted_local_paid",
            "confirmed_from_captured_payment",
            "confirmed_after_capture",
        }
        late_payment_outcomes = {
            "paid_after_hold_expiry",
            "captured_after_hold_expiry",
        }

        if outcome in confirmed_outcomes:
            await service._queue_booking_session_traveller_notifications(
                booking_session=booking_session,
                bookings=bookings,
                event_type="traveller_seat_confirmed",
            )

        await db.commit()

        notification_service = NotificationService(db=db, ws_hub=ws_hub)
        if outcome in confirmed_outcomes:
            await notification_service.notify_user(
                user_id=booking_session.owner_user_id,
                title="Payment verified",
                message="Your booking session is confirmed.",
                data={
                    "type": "booking_session_confirmed",
                    "booking_session_id": booking_session.id,
                    "scheduled_trip_id": booking_session.scheduled_trip_id,
                    "refresh": [
                        "bookings_list",
                        "booking_session_detail",
                        "current_booking",
                        "seatmap",
                    ],
                },
            )
        elif outcome in late_payment_outcomes:
            await notification_service.notify_user(
                user_id=booking_session.owner_user_id,
                title="Booking session expired",
                message=(
                    "Your payment arrived after the seat hold expired. "
                    "The seats were released and a refund was requested."
                ),
                data={
                    "type": "booking_session_late_payment_refund_requested",
                    "booking_session_id": booking_session.id,
                    "scheduled_trip_id": booking_session.scheduled_trip_id,
                    "refresh": [
                        "bookings_list",
                        "booking_session_detail",
                        "seatmap",
                    ],
                },
            )
        elif outcome.startswith("expired_"):
            await notification_service.notify_user(
                user_id=booking_session.owner_user_id,
                title="Booking session expired",
                message="Your payment window expired, so the selected seats were released.",
                data={
                    "type": "booking_session_expired",
                    "booking_session_id": booking_session.id,
                    "scheduled_trip_id": booking_session.scheduled_trip_id,
                    "refresh": [
                        "bookings_list",
                        "booking_session_detail",
                        "seatmap",
                    ],
                },
            )

        changed = (
            outcome in confirmed_outcomes
            or outcome in late_payment_outcomes
            or outcome.startswith("expired_")
        )
        if changed:
            try:
                from app.passenger.seatmap_ws import (
                    broadcast_all_seatmap_snapshots_for_trip,
                )

                await broadcast_all_seatmap_snapshots_for_trip(
                    scheduled_trip_id=booking_session.scheduled_trip_id,
                    reason=f"booking_session_payment_reconcile:{outcome}",
                )
            except Exception:
                logger.exception(
                    "payment_reconcile_session_seatmap_failed booking_session_id=%s outcome=%s",
                    booking_session.id,
                    outcome,
                )

            if api_refresh_hub is not None:
                await publish_booking_change(
                    api_refresh_hub,
                    db,
                    trip_id=booking_session.scheduled_trip_id,
                    passenger_user_id=booking_session.owner_user_id,
                    reason=f"booking_session_payment_reconcile:{outcome}",
                    booking_session_id=booking_session.id,
                    route_id=booking_session.route_id,
                )

        return outcome
    except Exception:
        await db.rollback()
        raise


async def _process_closed_booking_session_id(
    db: AsyncSession,
    booking_session_id: str,
    ws_hub: WSHub | None = None,
    api_refresh_hub: APIRefreshHub | None = None,
) -> str:
    service = PassengerService(db, ws_hub=ws_hub)
    stmt = (
        select(BookingSession)
        .where(
            BookingSession.id == booking_session_id,
            BookingSession.status.in_(
                (
                    BookingSessionStatus.EXPIRED,
                    BookingSessionStatus.CANCELLED,
                )
            ),
        )
        .with_for_update(skip_locked=True)
    )
    result = await db.execute(stmt)
    booking_session = result.scalar_one_or_none()

    if booking_session is None:
        await db.rollback()
        return "skip_missing_or_locked"

    try:
        payments = await service._list_booking_session_payments_for_update(
            booking_session.id
        )
        bookings = await service._list_booking_session_bookings_for_update(
            booking_session.id
        )
        outcome = await service.reconcile_closed_booking_session_payment(
            booking_session,
            bookings=bookings,
            payments=payments,
        )
        await db.commit()

        refund_queued_outcomes = {
            "closed_paid_payment_refund_queued",
            "closed_captured_payment_refund_queued",
        }
        if outcome in refund_queued_outcomes:
            notification_service = NotificationService(db=db, ws_hub=ws_hub)
            await notification_service.notify_user(
                user_id=booking_session.owner_user_id,
                title="Late payment refund requested",
                message=(
                    "A payment completed after your booking session closed. "
                    "The seats remain released and a refund was requested."
                ),
                data={
                    "type": "booking_session_late_payment_refund_requested",
                    "booking_session_id": booking_session.id,
                    "scheduled_trip_id": booking_session.scheduled_trip_id,
                    "refresh": [
                        "bookings_list",
                        "booking_session_detail",
                        "refunds",
                    ],
                },
            )

            if api_refresh_hub is not None:
                await publish_booking_change(
                    api_refresh_hub,
                    db,
                    trip_id=booking_session.scheduled_trip_id,
                    passenger_user_id=booking_session.owner_user_id,
                    reason=f"closed_session_payment_reconcile:{outcome}",
                    booking_session_id=booking_session.id,
                    route_id=booking_session.route_id,
                )

        return outcome
    except Exception:
        await db.rollback()
        raise


async def reconcile_pending_booking_payments_once(
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
            "payment_reconcile skipped: job lease not acquired owner_id=%s",
            owner_id,
        )
        return

    try:
        async with AsyncSessionLocal() as db:
            total_processed = 0

            booking_ids = await _fetch_pending_booking_ids(db, batch_size)
            await db.rollback()

            # Process at most one batch per tick. Pending provider payments stay
            # pending, so immediately fetching the first page again would loop
            # over the same rows forever and starve booking-session payments.
            for booking_id in booking_ids:
                try:
                    outcome = await _process_booking_id(
                        db,
                        booking_id,
                        ws_hub=ws_hub,
                        api_refresh_hub=api_refresh_hub,
                    )
                    total_processed += 1
                    logger.info(
                        "payment_reconcile booking_id=%s outcome=%s",
                        booking_id,
                        outcome,
                    )
                except Exception:
                    await db.rollback()
                    logger.exception(
                        "payment_reconcile booking_id=%s outcome=error",
                        booking_id,
                    )

            booking_session_ids = await _fetch_pending_booking_session_ids(
                db,
                batch_size,
            )
            await db.rollback()

            for booking_session_id in booking_session_ids:
                try:
                    outcome = await _process_booking_session_id(
                        db,
                        booking_session_id,
                        ws_hub=ws_hub,
                        api_refresh_hub=api_refresh_hub,
                    )
                    total_processed += 1
                    logger.info(
                        "payment_reconcile booking_session_id=%s outcome=%s",
                        booking_session_id,
                        outcome,
                    )
                except Exception:
                    await db.rollback()
                    logger.exception(
                        "payment_reconcile booking_session_id=%s outcome=error",
                        booking_session_id,
                    )

            closed_booking_session_ids = (
                await _fetch_closed_booking_session_ids(db, batch_size)
            )
            await db.rollback()

            for booking_session_id in closed_booking_session_ids:
                try:
                    outcome = await _process_closed_booking_session_id(
                        db,
                        booking_session_id,
                        ws_hub=ws_hub,
                        api_refresh_hub=api_refresh_hub,
                    )
                    total_processed += 1
                    logger.info(
                        "payment_reconcile closed_booking_session_id=%s outcome=%s",
                        booking_session_id,
                        outcome,
                    )
                except Exception:
                    await db.rollback()
                    logger.exception(
                        "payment_reconcile closed_booking_session_id=%s outcome=error",
                        booking_session_id,
                    )

            logger.info("payment_reconcile done processed=%s", total_processed)
    finally:
        try:
            async with AsyncSessionLocal() as lease_db:
                await release_job_lease(
                    db=lease_db,
                    job_name=_JOB_NAME,
                    owner_id=owner_id,
                )
        except Exception:
            logger.exception("payment_reconcile lease release failed")


async def payment_reconcile_loop(
    ws_hub: WSHub | None = None,
    api_refresh_hub: APIRefreshHub | None = None,
) -> None:
    logger.info("payment_reconcile loop started")
    try:
        await reconcile_pending_booking_payments_once(
            ws_hub=ws_hub,
            api_refresh_hub=api_refresh_hub,
        )

        while True:
            await asyncio.sleep(_seconds_until_next_minute())
            try:
                await reconcile_pending_booking_payments_once(
                    ws_hub=ws_hub,
                    api_refresh_hub=api_refresh_hub,
                )
            except Exception:
                logger.exception("payment_reconcile tick failed")
    except asyncio.CancelledError:
        logger.info("payment_reconcile loop cancelled")
        raise
