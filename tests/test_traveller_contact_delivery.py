from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.auth.schemas import MailAttachmentSchema
from app.db.schema import (
    TravellerContactNotificationStatus,
)
from app.notifications.traveller_contact_delivery import (
    SMTPEmailSender,
    TravellerContactDeliveryService,
)
from app.passenger.booking_qr import generate_booking_qr_png
from app.passenger.service import PassengerService


class TravellerNotificationQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_is_idempotent_per_booking_and_event(self) -> None:
        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = None
        db = MagicMock()
        db.execute = AsyncMock(return_value=execute_result)
        db.flush = AsyncMock()
        service = PassengerService(db)
        booking_session = SimpleNamespace(
            id="session-1",
            owner_user_id="owner-1",
        )
        booking = SimpleNamespace(
            id="booking-1",
            scheduled_trip_id="trip-1",
            route_id="route-1",
            pickup_stop_id="pickup-1",
            dropoff_stop_id="dropoff-1",
            pickup_sequence_no_snapshot=1,
            dropoff_sequence_no_snapshot=2,
            seat_number=3,
            traveller_profile_id="traveller-1",
            traveller_name_snapshot="Traveller",
            traveller_phone_snapshot="+919876543210",
            traveller_email_snapshot="traveller@example.com",
            booking_status=SimpleNamespace(value="booked"),
        )

        was_created = await service._queue_traveller_contact_notification(
            booking_session=booking_session,
            booking=booking,
            event_type="traveller_seat_confirmed",
            title="Seat confirmed",
            message="Confirmed",
        )

        statement = str(db.execute.await_args.args[0])
        self.assertFalse(was_created)
        self.assertIn(
            "ON CONFLICT ON CONSTRAINT "
            "uq_traveller_contact_notifications_booking_event DO NOTHING",
            statement,
        )


class TravellerNotificationTimeTests(unittest.TestCase):
    def test_utc_trip_time_is_rendered_in_ist(self) -> None:
        rendered = PassengerService._format_sms_datetime(
            datetime(2026, 7, 17, 6, 0, tzinfo=timezone.utc)
        )

        self.assertEqual(rendered, "17 Jul 2026, 11:30 AM IST")

    def test_naive_database_time_is_treated_as_utc(self) -> None:
        rendered = PassengerService._format_sms_datetime(
            datetime(2026, 7, 17, 6, 0)
        )

        self.assertEqual(rendered, "17 Jul 2026, 11:30 AM IST")


class TravellerConfirmationAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_email_contains_invoice_and_boarding_qr(self) -> None:
        db = MagicMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        email_sender = MagicMock()
        email_sender.send_email = AsyncMock(return_value="email:message-1")
        service = TravellerContactDeliveryService(
            db,
            email_sender=email_sender,
        )
        invoice_attachment = MailAttachmentSchema(
            filename="INV-BOOKING-1.pdf",
            content=b"%PDF-1.4 invoice",
            content_type="application/pdf",
        )
        qr_attachment = MailAttachmentSchema(
            filename="boarding-qr-booking-1.png",
            content=b"\x89PNG\r\n\x1a\nimage",
            content_type="image/png",
            content_id="traveller-booking-qr",
            inline=True,
        )
        service._build_confirmation_attachments = AsyncMock(
            return_value=[invoice_attachment, qr_attachment]
        )
        notification = SimpleNamespace(
            booking_id="booking-1",
            event_type="traveller_seat_confirmed",
            traveller_email_snapshot="traveller@example.com",
            title="Shuttle seat confirmed",
            message=(
                "Your seat is confirmed.\n"
                "Route: Office Route\n"
                "Pickup: First Stop\n"
                "Drop: Last Stop\n"
                "Seat: 3\n"
                "Vehicle: WB01AB1234"
            ),
            status=TravellerContactNotificationStatus.PENDING,
            delivered_channel=None,
            provider_message_id=None,
            failure_reason=None,
            delivery_retry_after=None,
            sent_at=None,
        )

        await service._process_email(notification)

        call = email_sender.send_email.await_args
        self.assertEqual(
            call.kwargs["to_email"],
            "traveller@example.com",
        )
        self.assertEqual(
            call.kwargs["attachments"],
            [invoice_attachment, qr_attachment],
        )
        self.assertIn(
            "invoice/payment receipt for this seat is attached",
            call.kwargs["html_body"],
        )
        self.assertIn(
            'src="cid:traveller-booking-qr"',
            call.kwargs["html_body"],
        )
        self.assertIn(
            "boarding QR image",
            call.kwargs["body"],
        )
        self.assertEqual(
            notification.status,
            TravellerContactNotificationStatus.SENT,
        )

    def test_qr_png_contains_valid_png_signature(self) -> None:
        png = generate_booking_qr_png(
            "eyJib29raW5nX2lkIjoiYm9va2luZy0xIn0.signature"
        )

        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(png), 100)

    def test_emailed_qr_remains_valid_through_trip_end(self) -> None:
        service = PassengerService(MagicMock())
        planned_end_at = datetime.now(timezone.utc) + timedelta(days=3)
        booking = SimpleNamespace(
            id="booking-1",
            scheduled_trip=SimpleNamespace(
                planned_end_at=planned_end_at
            ),
        )

        with patch.dict(
            os.environ,
            {"PASSENGER_QR_SECRET": "test-secret"},
        ):
            _token, payload = service._build_qr_token(booking)

        self.assertEqual(
            payload["expires_at"],
            int((planned_end_at + timedelta(hours=12)).timestamp()),
        )

    async def test_smtp_sender_marks_qr_as_inline_related_image(self) -> None:
        attachment = MailAttachmentSchema(
            filename="boarding-qr.png",
            content=b"\x89PNG\r\n\x1a\nimage",
            content_type="image/png",
            content_id="traveller-booking-qr",
            inline=True,
        )
        env = {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_FROM_EMAIL": "shuttle@example.com",
            "SMTP_USE_TLS": "false",
            "SMTP_USE_SSL": "false",
        }

        with (
            patch.dict(os.environ, env, clear=False),
            patch(
                "app.notifications.traveller_contact_delivery.asyncio.to_thread",
                new_callable=AsyncMock,
            ) as to_thread,
        ):
            await SMTPEmailSender().send_email(
                to_email="traveller@example.com",
                subject="Confirmed",
                body="Plain text",
                html_body=(
                    '<html><img src="cid:traveller-booking-qr"></html>'
                ),
                attachments=[attachment],
            )

        message = to_thread.await_args.args[2]
        qr_parts = [
            part
            for part in message.walk()
            if part.get_content_type() == "image/png"
        ]
        self.assertEqual(len(qr_parts), 1)
        self.assertEqual(
            qr_parts[0]["Content-ID"],
            "<traveller-booking-qr>",
        )
        self.assertEqual(
            qr_parts[0].get_content_disposition(),
            "inline",
        )


if __name__ == "__main__":
    unittest.main()
