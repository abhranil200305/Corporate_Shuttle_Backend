from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.db.schema import (
    BookingPaymentStatus,
    BookingSessionPayment,
    InvoiceEmailDelivery,
)
from app.jobs.invoice_email_delivery import _process_delivery
from app.passenger.invoice_pdf import generate_invoice_pdf
from app.passenger.service import PassengerService


def sample_invoice() -> dict:
    now = datetime(2026, 7, 14, 10, 30, tzinfo=timezone.utc)
    return {
        "invoice_number": "INV-20260714-ABCDEF12",
        "booking_id": "booking-1",
        "booking_created_at": now,
        "invoice_generated_at": now,
        "invoice_status": "preview",
        "currency": "INR",
        "supplier": {
            "gstin": "19ABCDE1234F1Z5",
            "legal_name": "TransEV Private Limited",
            "trade_name": "TransEV",
            "registered_address": "Kolkata, West Bengal",
            "state_name": "West Bengal",
            "state_code": "19",
            "postal_code": "700001",
        },
        "passenger": {
            "full_name": "Passenger Name",
            "email": "passenger@example.com",
            "traveller_name": "Passenger Name",
            "traveller_phone": "+919876543210",
            "traveller_email": None,
        },
        "service": {
            "sac_code": "996411",
            "description": "Passenger transportation service",
            "quantity": 1,
            "unit": "ride",
        },
        "place_of_supply": {"name": "West Bengal", "state_code": "19"},
        "compliance": {"reverse_charge_applicable": False},
        "trip": {
            "route_name": "Office Shuttle",
            "route_code": "OS-01",
            "seat_number": 4,
            "pickup_stop": {"name": "Pickup"},
            "dropoff_stop": {"name": "Dropoff"},
            "planned_start_at": now,
            "completed_at": None,
        },
        "breakdown": {
            "total_booking_amount": Decimal("30.00"),
            "taxable_value": Decimal("28.57"),
            "cgst_rate_percent": Decimal("2.50"),
            "cgst_amount": Decimal("0.71"),
            "sgst_rate_percent": Decimal("2.50"),
            "sgst_amount": Decimal("0.72"),
            "igst_rate_percent": Decimal("0.00"),
            "igst_amount": Decimal("0.00"),
            "total_tax_amount": Decimal("1.43"),
            "rounding_adjustment": Decimal("0.00"),
            "gst_inclusive": True,
        },
        "payment": {
            "status": "paid",
            "razorpay_order_id": "order_1",
            "razorpay_payment_id": "pay_1",
            "amount": Decimal("30.00"),
        },
    }


class InvoicePDFTests(unittest.TestCase):
    def test_pdf_contains_invoice_and_tax_details(self) -> None:
        pdf = generate_invoice_pdf(sample_invoice())

        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertTrue(pdf.endswith(b"%%EOF\n"))
        self.assertIn(b"INV-20260714-ABCDEF12", pdf)
        self.assertIn(b"19ABCDE1234F1Z5", pdf)
        self.assertIn(b"RAZORPAY PAYMENT ID", pdf)
        self.assertIn(b"pay_1", pdf)
        self.assertIn(b"FARE AND TAX SUMMARY", pdf)
        self.assertIn(b"System-generated payment receipt", pdf)
        self.assertIn(b"/MediaBox [0 0 595.28 841.89]", pdf)
        self.assertIn(b"/BaseFont /Helvetica-Bold", pdf)

    def test_pdf_distinguishes_traveller_from_booking_account(self) -> None:
        invoice = deepcopy(sample_invoice())
        invoice["passenger"].update(
            {
                "full_name": "Account Holder",
                "email": "account@example.com",
                "traveller_name": "Guest Traveller",
                "traveller_email": "guest@example.com",
            }
        )

        pdf = generate_invoice_pdf(invoice)

        self.assertIn(b"Guest Traveller", pdf)
        self.assertIn(b"BOOKED BY", pdf)
        self.assertIn(b"Account Holder", pdf)
        self.assertIn(b"ACCOUNT EMAIL", pdf)
        self.assertIn(b"account@example.com", pdf)

    def test_long_invoice_repeats_header_and_footer_across_pages(self) -> None:
        invoice = deepcopy(sample_invoice())
        invoice["supplier"]["registered_address"] = " ".join(
            ["Long registered office address"] * 45
        )

        pdf = generate_invoice_pdf(invoice)

        self.assertIn(b"/Count 2", pdf)
        self.assertEqual(pdf.count(b"(GST INVOICE) Tj"), 2)
        self.assertEqual(
            pdf.count(b"(System-generated payment receipt."), 2
        )
        self.assertIn(b"(Page 1 of 2) Tj", pdf)
        self.assertIn(b"(Page 2 of 2) Tj", pdf)

    def test_session_payment_is_eligible_for_seat_invoice(self) -> None:
        created_at = datetime(2026, 7, 14, 10, 30, tzinfo=timezone.utc)
        payment = BookingSessionPayment(
            id="session-payment-1",
            booking_session_id="session-1",
            razorpay_order_id="order_session_1",
            razorpay_payment_id="pay_session_1",
            status=BookingPaymentStatus.PAID,
            amount=Decimal("60.00"),
            created_at=created_at,
            updated_at=created_at,
        )
        booking = SimpleNamespace(
            payments=[],
            booking_session=SimpleNamespace(payments=[payment]),
        )

        selected = PassengerService.__new__(
            PassengerService
        )._get_latest_paid_invoice_payment(booking)

        self.assertIs(selected, payment)


class InvoiceDeliveryQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_is_created_once_per_booking(self) -> None:
        db = MagicMock()
        db.execute = AsyncMock()
        db.flush = AsyncMock()
        service = PassengerService(db)
        booking = SimpleNamespace(id="booking-1", booking_session_id=None)

        await service._queue_invoice_email_delivery(booking)

        statement = db.execute.await_args.args[0]
        compiled = str(statement.compile())
        self.assertIn("ON CONFLICT (delivery_key) DO NOTHING", compiled)
        db.flush.assert_awaited()

    async def test_existing_queue_is_not_duplicated(self) -> None:
        db = MagicMock()
        db.execute = AsyncMock()
        db.flush = AsyncMock()
        service = PassengerService(db)

        await service._queue_invoice_email_delivery(
            SimpleNamespace(id="booking-1", booking_session_id=None)
        )

        self.assertEqual(db.execute.await_count, 1)
        statement = db.execute.await_args.args[0]
        self.assertIn("DO NOTHING", str(statement.compile()))


class InvoicePassengerIdentityTests(unittest.TestCase):
    def test_account_identity_fills_missing_traveller_snapshots(self) -> None:
        service = PassengerService(MagicMock())
        party = service._build_invoice_passenger_party(
            booking=SimpleNamespace(
                traveller_name_snapshot=None,
                traveller_phone_snapshot=None,
                traveller_email_snapshot=None,
                traveller_relationship_label_snapshot=None,
            ),
            passenger_user=SimpleNamespace(
                id="passenger-1",
                email=" passenger@example.com ",
            ),
            passenger_profile=SimpleNamespace(full_name=" Passenger Name "),
        )

        self.assertEqual(party["full_name"], "Passenger Name")
        self.assertEqual(party["email"], "passenger@example.com")
        self.assertEqual(party["traveller_name"], "Passenger Name")
        self.assertEqual(party["traveller_email"], "passenger@example.com")

    def test_booking_snapshot_fills_missing_optional_profile(self) -> None:
        service = PassengerService(MagicMock())
        party = service._build_invoice_passenger_party(
            booking=SimpleNamespace(
                traveller_name_snapshot="Booked Traveller",
                traveller_phone_snapshot=" +919876543210 ",
                traveller_email_snapshot="traveller@example.com",
                traveller_relationship_label_snapshot="Guest",
            ),
            passenger_user=SimpleNamespace(
                id="passenger-1",
                email="account@example.com",
            ),
            passenger_profile=None,
        )

        self.assertEqual(party["full_name"], "Booked Traveller")
        self.assertEqual(party["email"], "account@example.com")
        self.assertEqual(party["traveller_name"], "Booked Traveller")
        self.assertEqual(party["traveller_email"], "traveller@example.com")
        self.assertEqual(party["traveller_phone"], "+919876543210")


class InvoiceEmailWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_delivery_attaches_generated_pdf(self) -> None:
        db = MagicMock()
        passenger = SimpleNamespace(
            id="passenger-1",
            email="passenger@example.com",
            passenger_profile=SimpleNamespace(full_name="Passenger Name"),
        )
        booking = SimpleNamespace(
            id="booking-1",
            booking_session_id=None,
            fare_amount=Decimal("30.00"),
            passenger=passenger,
        )
        delivery = InvoiceEmailDelivery(
            id="delivery-1",
            delivery_key="booking:booking-1",
            booking_id="booking-1",
            status="pending",
            attempt_count=0,
        )

        async def run_immediately(function, *args, **kwargs):
            return function(*args, **kwargs)

        with (
            patch(
                "app.jobs.invoice_email_delivery._load_booking",
                new=AsyncMock(return_value=booking),
            ),
            patch.object(
                PassengerService,
                "_build_booking_invoice_payload",
                new=AsyncMock(return_value=sample_invoice()),
            ),
            patch(
                "app.jobs.invoice_email_delivery._load_delivery_bookings",
                new=AsyncMock(return_value=[booking]),
            ),
            patch("app.jobs.invoice_email_delivery.send_mail") as send_mail,
            patch(
                "app.jobs.invoice_email_delivery.asyncio.to_thread",
                new=run_immediately,
            ),
        ):
            await _process_delivery(db, delivery)

        self.assertEqual(delivery.status, "sent")
        self.assertEqual(delivery.recipient_email, "passenger@example.com")
        send_mail.assert_called_once()
        attachment = send_mail.call_args.kwargs["attachments"][0]
        self.assertEqual(attachment.content_type, "application/pdf")
        self.assertTrue(attachment.content.startswith(b"%PDF-1.4"))


if __name__ == "__main__":
    unittest.main()
