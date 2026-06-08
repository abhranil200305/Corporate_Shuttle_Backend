from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.database import AsyncSessionLocal
from app.db.schema import (
    BookingPaymentStatus,
    BookingSeatRefundRequest,
    BookingSeatRefundRequestStatus,
    BookingSession,
    BookingSessionPayment,
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

_JOB_NAME = "booking_seat_refund"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_batch_size() -> int:
    raw = os.getenv("BOOKING_SEAT_REFUND_BATCH_SIZE", "50").strip()
    try:
        value = int(raw)
    except ValueError:
        return 50
    return max(1, min(value, 100))


def _get_interval_seconds() -> int:
    raw = os.getenv("BOOKING_SEAT_REFUND_INTERVAL_SECONDS", "60").strip()
    try:
        value = int(raw)
    except ValueError:
        return 60
    return max(5, value)


def _get_lease_seconds() -> int:
    default_value = max(_get_interval_seconds() + 60, 120)
    raw = os.getenv("BOOKING_SEAT_REFUND_LEASE_SECONDS", str(default_value)).strip()
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


def _get_retry_delay_seconds(attempt_count: int) -> int:
    # 1m, 2m, 4m, 8m, 16m, 32m, 64m, capped at 2h
    exponent = min(max(attempt_count - 1, 0), 7)
    return min(60 * (2**exponent), 7200)


async def _fetch_due_refund_request_ids(
    db: AsyncSession,
    limit: int,
) -> list[str]:
    stmt = (
        select(BookingSeatRefundRequest.id)
        .where(
            BookingSeatRefundRequest.status.in_(
                (
                    BookingSeatRefundRequestStatus.PENDING,
                    BookingSeatRefundRequestStatus.FAILED,
                )
            ),
            or_(
                BookingSeatRefundRequest.retry_after.is_(None),
                BookingSeatRefundRequest.retry_after <= utcnow(),
            ),
        )
        .order_by(
            func.coalesce(
                BookingSeatRefundRequest.retry_after,
                BookingSeatRefundRequest.created_at,
            ).asc(),
            BookingSeatRefundRequest.created_at.asc(),
        )
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _get_refund_request_for_update(
    db: AsyncSession,
    refund_request_id: str,
) -> BookingSeatRefundRequest | None:
    stmt = (
        select(BookingSeatRefundRequest)
        .where(BookingSeatRefundRequest.id == refund_request_id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )

    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _get_booking_for_update(
    db: AsyncSession,
    booking_id: str,
) -> TripBooking | None:
    stmt = (
        select(TripBooking)
        .where(TripBooking.id == booking_id)
        .with_for_update()
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _get_session_for_update(
    db: AsyncSession,
    booking_session_id: str,
) -> BookingSession | None:
    stmt = (
        select(BookingSession)
        .where(BookingSession.id == booking_session_id)
        .with_for_update()
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _get_payment_for_update(
    db: AsyncSession,
    payment_id: str,
) -> BookingSessionPayment | None:
    stmt = (
        select(BookingSessionPayment)
        .where(BookingSessionPayment.id == payment_id)
        .with_for_update()
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


async def _create_razorpay_refund(
    *,
    service: RoutePayoutService,
    refund_request: BookingSeatRefundRequest,
    payment: BookingSessionPayment,
) -> dict[str, Any]:
    if not payment.razorpay_payment_id:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "booking_session_payment_missing_razorpay_payment_id",
                "message": "Cannot refund because the session payment has no Razorpay payment id.",
                "booking_session_payment_id": payment.id,
            },
        )

    amount_subunits = service._to_subunits(Decimal(refund_request.amount or 0))

    if amount_subunits <= 0:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "refund_amount_not_positive",
                "message": "Refund amount must be greater than zero.",
                "refund_request_id": refund_request.id,
            },
        )

    payload = {
        "amount": amount_subunits,
        "speed": "normal",
        "notes": {
            "booking_seat_refund_request_id": refund_request.id,
            "booking_session_id": refund_request.booking_session_id,
            "booking_id": refund_request.booking_id,
            "owner_user_id": refund_request.owner_user_id,
        },
    }

    return await service._razorpay_request(
        method="POST",
        path=f"/payments/{payment.razorpay_payment_id}/refund",
        json_payload=payload,
    )


def _extract_provider_refund_amount(
    *,
    service: RoutePayoutService,
    provider_response: dict[str, Any],
    fallback_amount: Decimal,
) -> Decimal:
    raw_amount = provider_response.get("amount")
    if raw_amount is None:
        return service._quantize_money(fallback_amount)

    try:
        return service._from_subunits(int(raw_amount))
    except Exception:
        return service._quantize_money(fallback_amount)


def _is_provider_refund_success_status(provider_status: str | None) -> bool:
    normalized = (provider_status or "").strip().lower()
    return normalized in {"processed", "pending", "created"}


async def _mark_refund_succeeded(
    *,
    db: AsyncSession,
    refund_request: BookingSeatRefundRequest,
    payment: BookingSessionPayment,
    provider_response: dict[str, Any],
    refunded_amount: Decimal,
) -> None:
    now = utcnow()

    refund_request.status = BookingSeatRefundRequestStatus.SUCCEEDED
    refund_request.razorpay_refund_id = provider_response.get("id")
    refund_request.provider_response_json = _json_dumps(provider_response)
    refund_request.failure_reason = None
    refund_request.retry_after = None
    refund_request.processed_at = refund_request.processed_at or now

    existing_refunded_amount = Decimal(payment.refunded_amount or 0)
    payment_amount = Decimal(payment.amount or 0)
    next_refunded_amount = existing_refunded_amount + refunded_amount

    if next_refunded_amount > payment_amount:
        next_refunded_amount = payment_amount

    payment.refunded_amount = next_refunded_amount

    if payment.refund_requested_at is None:
        payment.refund_requested_at = refund_request.requested_at

    if payment.refunded_amount >= payment_amount:
        payment.status = BookingPaymentStatus.REFUNDED
        payment.refund_processed_at = payment.refund_processed_at or now
        payment.refund_retry_after = None
        payment.refund_failure_reason = None
    else:
        # Partial refund done; payment remains paid.
        payment.status = BookingPaymentStatus.PAID
        payment.refund_retry_after = None
        payment.refund_failure_reason = None

    db.add(refund_request)
    db.add(payment)
    await db.flush()


async def _mark_refund_failed(
    *,
    db: AsyncSession,
    refund_request: BookingSeatRefundRequest,
    payment: BookingSessionPayment | None,
    failure_reason: str,
    provider_payload: Any | None = None,
) -> None:
    attempt_count = int(refund_request.attempt_count or 0)
    retry_delay_seconds = _get_retry_delay_seconds(attempt_count)

    refund_request.status = BookingSeatRefundRequestStatus.FAILED
    refund_request.failure_reason = failure_reason
    refund_request.retry_after = utcnow() + timedelta(seconds=retry_delay_seconds)

    if provider_payload is not None:
        refund_request.provider_response_json = _json_dumps(provider_payload)

    db.add(refund_request)

    if payment is not None:
        payment.refund_retry_after = refund_request.retry_after
        payment.refund_failure_reason = failure_reason
        payment.refund_attempt_count = max(
            int(payment.refund_attempt_count or 0),
            attempt_count,
        )
        db.add(payment)

    await db.flush()


async def _notify_refund_outcome(
    *,
    db: AsyncSession,
    ws_hub: WSHub | None,
    refund_request: BookingSeatRefundRequest,
    booking: TripBooking,
    outcome: str,
) -> None:
    if outcome != "refund_processed":
        return

    notification_service = NotificationService(db=db, ws_hub=ws_hub)
    await notification_service.notify_user(
        user_id=refund_request.owner_user_id,
        title="Seat refund processed",
        message="Refund for your cancelled seat has been processed.",
        data={
            "type": "booking_seat_refund_processed",
            "booking_seat_refund_request_id": refund_request.id,
            "booking_session_id": refund_request.booking_session_id,
            "booking_id": refund_request.booking_id,
            "scheduled_trip_id": booking.scheduled_trip_id,
            "refresh": [
                "bookings_list",
                "booking_session_detail",
                "refunds",
            ],
        },
    )


async def _process_refund_request_id(
    db: AsyncSession,
    refund_request_id: str,
    *,
    ws_hub: WSHub | None = None,
) -> str:
    refund_request = await _get_refund_request_for_update(db, refund_request_id)

    if refund_request is None:
        await db.rollback()
        return "skip_missing_or_locked"

    if refund_request.status == BookingSeatRefundRequestStatus.SUCCEEDED:
        await db.rollback()
        return "skip_already_succeeded"

    if refund_request.status == BookingSeatRefundRequestStatus.PROCESSING:
        await db.rollback()
        return "skip_processing"

    booking = await _get_booking_for_update(db, refund_request.booking_id)
    session = await _get_session_for_update(db, refund_request.booking_session_id)
    payment = await _get_payment_for_update(
        db,
        refund_request.booking_session_payment_id,
    )

    if booking is None:
        await _mark_refund_failed(
            db=db,
            refund_request=refund_request,
            payment=None,
            failure_reason="seat_booking_missing",
        )
        await db.commit()
        return "failed_booking_missing"

    if session is None:
        await _mark_refund_failed(
            db=db,
            refund_request=refund_request,
            payment=None,
            failure_reason="booking_session_missing",
        )
        await db.commit()
        return "failed_session_missing"

    if payment is None:
        await _mark_refund_failed(
            db=db,
            refund_request=refund_request,
            payment=None,
            failure_reason="booking_session_payment_missing",
        )
        await db.commit()
        return "failed_payment_missing"

    if booking.booking_status != BookingStatus.CANCELLED:
        await _mark_refund_failed(
            db=db,
            refund_request=refund_request,
            payment=payment,
            failure_reason="seat_booking_not_cancelled",
        )
        await db.commit()
        return "failed_booking_not_cancelled"

    if payment.status not in {
        BookingPaymentStatus.PAID,
        BookingPaymentStatus.REFUNDED,
    }:
        await _mark_refund_failed(
            db=db,
            refund_request=refund_request,
            payment=payment,
            failure_reason="booking_session_payment_not_refundable",
        )
        await db.commit()
        return "failed_payment_not_refundable"

    if payment.status == BookingPaymentStatus.REFUNDED:
        refund_request.status = BookingSeatRefundRequestStatus.SUCCEEDED
        refund_request.failure_reason = None
        refund_request.retry_after = None
        refund_request.processed_at = refund_request.processed_at or utcnow()
        db.add(refund_request)
        await db.commit()
        return "skip_payment_already_refunded"

    refund_amount = Decimal(refund_request.amount or 0)

    if refund_amount <= Decimal("0.00"):
        refund_request.status = BookingSeatRefundRequestStatus.SKIPPED
        refund_request.failure_reason = "refund_amount_not_positive"
        refund_request.retry_after = None
        refund_request.processed_at = refund_request.processed_at or utcnow()
        db.add(refund_request)
        await db.commit()
        return "skip_non_positive_amount"

    payment_amount = Decimal(payment.amount or 0)
    refunded_amount = Decimal(payment.refunded_amount or 0)
    remaining_refundable_amount = payment_amount - refunded_amount

    if remaining_refundable_amount <= Decimal("0.00"):
        payment.status = BookingPaymentStatus.REFUNDED
        payment.refund_processed_at = payment.refund_processed_at or utcnow()
        payment.refund_retry_after = None
        payment.refund_failure_reason = None

        refund_request.status = BookingSeatRefundRequestStatus.SUCCEEDED
        refund_request.failure_reason = None
        refund_request.retry_after = None
        refund_request.processed_at = refund_request.processed_at or utcnow()

        db.add(payment)
        db.add(refund_request)
        await db.commit()
        return "skip_no_remaining_refundable_amount"

    if refund_amount > remaining_refundable_amount:
        await _mark_refund_failed(
            db=db,
            refund_request=refund_request,
            payment=payment,
            failure_reason="refund_amount_exceeds_remaining_refundable_amount",
        )
        await db.commit()
        return "failed_amount_exceeds_remaining"

    now = utcnow()
    refund_request.status = BookingSeatRefundRequestStatus.PROCESSING
    refund_request.attempt_count = int(refund_request.attempt_count or 0) + 1
    refund_request.retry_after = None

    payment.refund_requested_at = payment.refund_requested_at or refund_request.requested_at
    payment.refund_attempt_count = max(
        int(payment.refund_attempt_count or 0),
        int(refund_request.attempt_count or 0),
    )
    payment.refund_retry_after = None
    payment.refund_failure_reason = None

    db.add(refund_request)
    db.add(payment)
    await db.flush()

    service = RoutePayoutService(db, ws_hub=ws_hub)

    try:
        provider_response = await _create_razorpay_refund(
            service=service,
            refund_request=refund_request,
            payment=payment,
        )
    except HTTPException as exc:
        await _mark_refund_failed(
            db=db,
            refund_request=refund_request,
            payment=payment,
            failure_reason="razorpay_refund_request_failed",
            provider_payload=exc.detail,
        )
        await db.commit()
        return "failed_provider_request"
    except Exception as exc:
        await _mark_refund_failed(
            db=db,
            refund_request=refund_request,
            payment=payment,
            failure_reason=str(exc),
        )
        await db.commit()
        return "failed_unhandled_exception"

    provider_refund_id = provider_response.get("id")
    provider_status = provider_response.get("status")

    if not provider_refund_id:
        await _mark_refund_failed(
            db=db,
            refund_request=refund_request,
            payment=payment,
            failure_reason="razorpay_refund_id_missing",
            provider_payload=provider_response,
        )
        await db.commit()
        return "failed_refund_id_missing"

    if not _is_provider_refund_success_status(provider_status):
        await _mark_refund_failed(
            db=db,
            refund_request=refund_request,
            payment=payment,
            failure_reason=f"razorpay_refund_status_not_success:{provider_status}",
            provider_payload=provider_response,
        )
        await db.commit()
        return "failed_provider_status"

    provider_refunded_amount = _extract_provider_refund_amount(
        service=service,
        provider_response=provider_response,
        fallback_amount=refund_amount,
    )

    if provider_refunded_amount <= Decimal("0.00"):
        await _mark_refund_failed(
            db=db,
            refund_request=refund_request,
            payment=payment,
            failure_reason="razorpay_refund_amount_invalid",
            provider_payload=provider_response,
        )
        await db.commit()
        return "failed_provider_amount_invalid"

    if provider_refunded_amount > remaining_refundable_amount:
        await _mark_refund_failed(
            db=db,
            refund_request=refund_request,
            payment=payment,
            failure_reason="razorpay_refund_amount_exceeds_remaining_refundable_amount",
            provider_payload=provider_response,
        )
        await db.commit()
        return "failed_provider_amount_exceeds_remaining"

    await _mark_refund_succeeded(
        db=db,
        refund_request=refund_request,
        payment=payment,
        provider_response=provider_response,
        refunded_amount=provider_refunded_amount,
    )

    booking.refund_retry_after = None
    booking.refund_attempt_count = int(refund_request.attempt_count or 0)
    db.add(booking)

    await db.commit()

    await _notify_refund_outcome(
        db=db,
        ws_hub=ws_hub,
        refund_request=refund_request,
        booking=booking,
        outcome="refund_processed",
    )

    return "refund_processed"


async def reconcile_booking_seat_refunds_once(
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
            "booking_seat_refund skipped: job lease not acquired owner_id=%s",
            owner_id,
        )
        return

    try:
        async with AsyncSessionLocal() as db:
            total_processed = 0

            while True:
                refund_request_ids = await _fetch_due_refund_request_ids(
                    db,
                    batch_size,
                )
                await db.rollback()

                if not refund_request_ids:
                    break

                for refund_request_id in refund_request_ids:
                    try:
                        outcome = await _process_refund_request_id(
                            db,
                            refund_request_id,
                            ws_hub=ws_hub,
                        )
                        total_processed += 1
                        logger.info(
                            "booking_seat_refund refund_request_id=%s outcome=%s",
                            refund_request_id,
                            outcome,
                        )
                    except Exception:
                        await db.rollback()
                        logger.exception(
                            "booking_seat_refund refund_request_id=%s outcome=error",
                            refund_request_id,
                        )

                if len(refund_request_ids) < batch_size:
                    break

            logger.info("booking_seat_refund done processed=%s", total_processed)

    finally:
        try:
            async with AsyncSessionLocal() as lease_db:
                await release_job_lease(
                    db=lease_db,
                    job_name=_JOB_NAME,
                    owner_id=owner_id,
                )
        except Exception:
            logger.exception("booking_seat_refund lease release failed")


async def booking_seat_refund_loop(
    ws_hub: WSHub | None = None,
) -> None:
    logger.info("booking_seat_refund loop started")
    try:
        await reconcile_booking_seat_refunds_once(ws_hub=ws_hub)

        while True:
            await asyncio.sleep(_seconds_until_next_minute())
            try:
                await reconcile_booking_seat_refunds_once(ws_hub=ws_hub)
            except Exception:
                logger.exception("booking_seat_refund tick failed")
    except asyncio.CancelledError:
        logger.info("booking_seat_refund loop cancelled")
        raise