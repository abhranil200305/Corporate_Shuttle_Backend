from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.auth.schemas import MailAttachmentSchema
from app.db.schema import (
    TravellerContactNotificationStatus,
)
from app.notifications.traveller_contact_delivery import (
    TravellerContactDeliveryService,
)
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


class TravellerInvoiceAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_email_contains_own_invoice_attachment(self) -> None:
        db = MagicMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        email_sender = MagicMock()
        email_sender.send_email = AsyncMock(return_value="email:message-1")
        service = TravellerContactDeliveryService(
            db,
            email_sender=email_sender,
        )
        attachment = MailAttachmentSchema(
            filename="INV-BOOKING-1.pdf",
            content=b"%PDF-1.4 invoice",
            content_type="application/pdf",
        )
        service._build_invoice_attachment = AsyncMock(
            return_value=attachment
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
        self.assertEqual(call.kwargs["attachments"], [attachment])
        self.assertIn(
            "invoice/payment receipt for this seat is attached",
            call.kwargs["html_body"],
        )
        self.assertEqual(
            notification.status,
            TravellerContactNotificationStatus.SENT,
        )


if __name__ == "__main__":
    unittest.main()
