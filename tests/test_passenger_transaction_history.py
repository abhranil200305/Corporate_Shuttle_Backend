from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.db.schema import (
    BookingPayment,
    BookingPaymentStatus,
    BookingSession,
    BookingSessionPayment,
    TripBooking,
)
from app.passenger.service import PassengerService


def payment(model, *, status: BookingPaymentStatus):
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    kwargs = {
        "id": "payment-1",
        "razorpay_order_id": "order-1",
        "razorpay_payment_id": "pay-1" if status == BookingPaymentStatus.PAID else None,
        "status": status,
        "amount": Decimal("30.00"),
        "taxable_amount": Decimal("28.57"),
        "cgst_amount": Decimal("0.71"),
        "sgst_amount": Decimal("0.72"),
        "igst_amount": Decimal("0.00"),
        "total_tax_amount": Decimal("1.43"),
        "created_at": now,
        "updated_at": now,
    }
    if model is BookingPayment:
        kwargs["booking_id"] = "booking-1"
    else:
        kwargs["booking_session_id"] = "session-1"
    return model(**kwargs)


class PassengerTransactionHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_payment_does_not_expand_booking_or_invoice(self) -> None:
        service = PassengerService(MagicMock())
        failed_payment = payment(BookingPayment, status=BookingPaymentStatus.FAILED)
        service._serialize_transaction = MagicMock(
            return_value={
                "booking": None,
                "bookings": None,
                "invoice": None,
                "invoices": None,
                "failure": {"code": "payment_failed"},
            }
        )
        service._serialize_booking_detail = AsyncMock()
        service._build_booking_invoice_payload = AsyncMock()

        result = await service._serialize_detailed_transaction(
            failed_payment,
            current_user=SimpleNamespace(id="passenger-1"),
            passenger_profile=None,
        )

        self.assertEqual(result["failure"]["code"], "payment_failed")
        self.assertIsNone(result["booking"])
        self.assertIsNone(result["invoice"])
        service._serialize_booking_detail.assert_not_awaited()
        service._build_booking_invoice_payload.assert_not_awaited()

    async def test_paid_direct_payment_reuses_booking_and_invoice_payloads(self) -> None:
        service = PassengerService(MagicMock())
        paid_payment = payment(BookingPayment, status=BookingPaymentStatus.PAID)
        paid_payment.booking = TripBooking(id="booking-1", seat_number=1)
        service._serialize_transaction = MagicMock(
            return_value={
                "booking": None,
                "bookings": None,
                "invoice": None,
                "invoices": None,
                "invoice_unavailable_reason": None,
            }
        )
        service._serialize_booking_detail = AsyncMock(
            return_value={"id": "booking-1"}
        )
        service._build_booking_invoice_payload = AsyncMock(
            return_value={"booking_id": "booking-1"}
        )

        result = await service._serialize_detailed_transaction(
            paid_payment,
            current_user=SimpleNamespace(id="passenger-1"),
            passenger_profile=SimpleNamespace(full_name="Passenger"),
        )

        self.assertEqual(result["booking"], {"id": "booking-1"})
        self.assertEqual(result["bookings"], [{"id": "booking-1"}])
        self.assertEqual(result["invoice"], {"booking_id": "booking-1"})
        self.assertEqual(result["invoices"], [{"booking_id": "booking-1"}])

    async def test_paid_session_expands_every_seat(self) -> None:
        service = PassengerService(MagicMock())
        paid_payment = payment(
            BookingSessionPayment,
            status=BookingPaymentStatus.PAID,
        )
        paid_payment.booking_session = BookingSession(
            id="session-1",
            bookings=[
                TripBooking(id="booking-2", seat_number=2),
                TripBooking(id="booking-1", seat_number=1),
            ]
        )
        service._serialize_booking_session_transaction = MagicMock(
            return_value={
                "booking": None,
                "bookings": None,
                "invoice": None,
                "invoices": None,
                "invoice_unavailable_reason": None,
            }
        )
        service._serialize_booking_detail = AsyncMock(
            side_effect=lambda booking: {"id": booking.id}
        )
        service._build_booking_invoice_payload = AsyncMock(
            side_effect=lambda **kwargs: {"booking_id": kwargs["booking"].id}
        )

        result = await service._serialize_detailed_transaction(
            paid_payment,
            current_user=SimpleNamespace(id="passenger-1"),
            passenger_profile=None,
        )

        self.assertIsNone(result["booking"])
        self.assertIsNone(result["invoice"])
        self.assertEqual(
            result["bookings"],
            [{"id": "booking-1"}, {"id": "booking-2"}],
        )
        self.assertEqual(
            result["invoices"],
            [{"booking_id": "booking-1"}, {"booking_id": "booking-2"}],
        )


if __name__ == "__main__":
    unittest.main()
