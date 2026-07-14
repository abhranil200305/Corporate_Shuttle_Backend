from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.mailer import send_mail
from app.auth.schemas import MailAttachmentSchema
from app.db.database import AsyncSessionLocal
from app.db.schema import (
    BookingSession,
    InvoiceEmailDelivery,
    Route,
    RouteStop,
    ScheduledTrip,
    TripBooking,
    User,
    utcnow,
)
from app.passenger.invoice_pdf import generate_invoice_pdf
from app.passenger.service import PassengerService


logger = logging.getLogger(__name__)


def _truthy_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _positive_int_env(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except ValueError:
        value = default
    return min(max(value, 1), maximum)


def _retry_delay_seconds(attempt_count: int) -> int:
    exponent = min(max(attempt_count - 1, 0), 6)
    return min(60 * (2**exponent), 3600)


def _booking_load_options():
    return (
        selectinload(TripBooking.passenger).selectinload(User.passenger_profile),
        selectinload(TripBooking.payments),
        selectinload(TripBooking.booking_session).selectinload(
            BookingSession.payments
        ),
        selectinload(TripBooking.pickup_stop),
        selectinload(TripBooking.dropoff_stop),
        selectinload(TripBooking.scheduled_trip)
        .selectinload(ScheduledTrip.route)
        .selectinload(Route.route_stops)
        .selectinload(RouteStop.stop),
    )


async def _load_booking(
    db: AsyncSession,
    booking_id: str,
) -> TripBooking | None:
    result = await db.execute(
        select(TripBooking)
        .where(TripBooking.id == booking_id)
        .options(*_booking_load_options())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _load_delivery_bookings(
    db: AsyncSession,
    representative_booking: TripBooking,
) -> list[TripBooking]:
    if representative_booking.booking_session_id is None:
        return [representative_booking]

    result = await db.execute(
        select(TripBooking)
        .where(
            TripBooking.booking_session_id
            == representative_booking.booking_session_id
        )
        .options(*_booking_load_options())
        .order_by(TripBooking.seat_number.asc())
    )
    return list(result.scalars().unique().all())


async def _process_delivery(
    db: AsyncSession,
    delivery: InvoiceEmailDelivery,
) -> None:
    delivery.attempt_count = int(delivery.attempt_count or 0) + 1
    booking = await _load_booking(db, delivery.booking_id)

    if booking is None:
        delivery.status = "skipped"
        delivery.failure_reason = "booking_not_found"
        delivery.retry_after = None
        db.add(delivery)
        return

    recipient_email = (booking.passenger.email or "").strip()
    if not recipient_email:
        delivery.status = "skipped"
        delivery.failure_reason = "passenger_email_missing"
        delivery.retry_after = None
        db.add(delivery)
        return

    delivery.recipient_email = recipient_email

    try:
        delivery_bookings = await _load_delivery_bookings(db, booking)
        invoice_service = PassengerService(db)
        invoices = [
            await invoice_service._build_booking_invoice_payload(
                booking=item,
                passenger_user=item.passenger,
                passenger_profile=item.passenger.passenger_profile,
            )
            for item in delivery_bookings
        ]
        if not invoices:
            raise RuntimeError("No invoice-eligible bookings were found for delivery.")

        invoice_number = str(invoices[0]["invoice_number"])
        supplier = invoices[0].get("supplier") or {}
        supplier_name = (
            supplier.get("trade_name")
            or supplier.get("legal_name")
            or "Shuttle"
        )
        body = (
            "Your payment was successful and your booking is confirmed.\n\n"
            f"Invoice/receipt count: {len(invoices)}\n"
            f"Total amount: INR {sum(Decimal(str(item.fare_amount)) for item in delivery_bookings):.2f}\n\n"
            "The GST invoice/payment receipt PDF files are attached. "
            "It is currently marked as a preview until formal invoice finalisation "
            "and digital signing are introduced.\n\n"
            f"Regards,\n{supplier_name}"
        )
        await asyncio.to_thread(
            send_mail,
            to_email=recipient_email,
            subject=f"Payment successful - invoice {invoice_number}",
            body=body,
            attachments=[
                MailAttachmentSchema(
                    filename=f"{invoice['invoice_number']}.pdf",
                    content=generate_invoice_pdf(invoice),
                    content_type="application/pdf",
                )
                for invoice in invoices
            ],
        )
    except Exception as exc:
        delivery.status = "failed"
        delivery.failure_reason = str(exc)[:2000]
        delivery.retry_after = utcnow() + timedelta(
            seconds=_retry_delay_seconds(delivery.attempt_count)
        )
        db.add(delivery)
        logger.warning(
            "invoice_email_delivery_failed booking_id=%s attempt=%s error=%s",
            delivery.booking_id,
            delivery.attempt_count,
            exc,
        )
        return

    delivery.status = "sent"
    delivery.sent_at = utcnow()
    delivery.retry_after = None
    delivery.failure_reason = None
    delivery.message_id = f"email:{uuid4().hex}"
    db.add(delivery)


async def process_invoice_email_delivery_batch() -> int:
    batch_size = _positive_int_env(
        "INVOICE_EMAIL_DELIVERY_BATCH_SIZE", 25, 100
    )
    now = utcnow()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(InvoiceEmailDelivery)
            .where(
                InvoiceEmailDelivery.status.in_(("pending", "failed")),
                or_(
                    InvoiceEmailDelivery.retry_after.is_(None),
                    InvoiceEmailDelivery.retry_after <= now,
                ),
            )
            .order_by(InvoiceEmailDelivery.created_at.asc())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        deliveries = list(result.scalars().all())
        for delivery in deliveries:
            await _process_delivery(db, delivery)
        await db.commit()
        return len(deliveries)


async def invoice_email_delivery_loop() -> None:
    if not _truthy_env("INVOICE_EMAIL_DELIVERY_ENABLED", True):
        logger.info("invoice_email_delivery_disabled")
        return

    interval_seconds = _positive_int_env(
        "INVOICE_EMAIL_DELIVERY_INTERVAL_SECONDS", 30, 3600
    )
    while True:
        try:
            processed = await process_invoice_email_delivery_batch()
            if processed:
                logger.info("invoice_email_delivery_batch_processed count=%s", processed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("invoice_email_delivery_loop_failed")
        await asyncio.sleep(interval_seconds)
