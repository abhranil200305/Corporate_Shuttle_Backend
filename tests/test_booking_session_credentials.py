from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException

from app.db.schema import BookingSessionStatus, BookingStatus
from app.driver.scan_events.booking_credential_scan import (
    resolve_otp_bookings_for_update,
)
from app.passenger.service import PassengerService


class _ScalarResult:
    def __init__(self, values: list) -> None:
        self._values = values

    def all(self) -> list:
        return self._values

    def unique(self) -> _ScalarResult:
        return self


class _ExecuteResult:
    def __init__(self, values: list) -> None:
        self._values = values

    def scalars(self) -> _ScalarResult:
        return _ScalarResult(self._values)


class BookingSessionQRTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_secret = os.environ.get("PASSENGER_QR_SECRET")
        os.environ["PASSENGER_QR_SECRET"] = "test-secret"
        self.service = PassengerService(db=AsyncMock())

    def tearDown(self) -> None:
        if self.previous_secret is None:
            os.environ.pop("PASSENGER_QR_SECRET", None)
        else:
            os.environ["PASSENGER_QR_SECRET"] = self.previous_secret

    def test_session_booking_qr_is_scoped_to_booking_session(self) -> None:
        booking = SimpleNamespace(
            id="booking-1",
            booking_session_id="session-1",
            scheduled_trip_id="trip-1",
        )

        token, payload = self.service._build_qr_token(booking)

        self.assertTrue(token)
        self.assertEqual(payload["credential_scope"], "booking_session")
        self.assertEqual(payload["booking_session_id"], "session-1")
        self.assertEqual(payload["scheduled_trip_id"], "trip-1")
        self.assertNotIn("booking_id", payload)

    def test_legacy_booking_qr_stays_booking_scoped(self) -> None:
        booking = SimpleNamespace(
            id="booking-1",
            booking_session_id=None,
            scheduled_trip_id="trip-1",
        )

        _, payload = self.service._build_qr_token(booking)

        self.assertEqual(payload["credential_scope"], "booking")
        self.assertEqual(payload["booking_id"], "booking-1")
        self.assertNotIn("booking_session_id", payload)

    def test_session_otp_is_hidden_for_terminal_session(self) -> None:
        session = SimpleNamespace(
            otp="123456",
            status=BookingSessionStatus.CANCELLED,
        )

        self.assertIsNone(self.service._serialize_booking_session_otp(session))


class BookingSessionOTPResolveTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_otp_rejects_multiple_active_groups(self) -> None:
        db = AsyncMock()
        db.execute.side_effect = [
            _ExecuteResult(["session-1"]),
            _ExecuteResult(
                [
                    SimpleNamespace(
                        id="booking-2",
                        booking_session_id="session-2",
                        booking_status=BookingStatus.BOOKED,
                        seat_number=2,
                        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
                    )
                ]
            ),
        ]

        with self.assertRaises(HTTPException) as ctx:
            await resolve_otp_bookings_for_update(
                db,
                trip_id="trip-1",
                otp_code="123456",
            )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(
            ctx.exception.detail["error"],
            "ambiguous_booking_otp",
        )

    async def test_session_otp_expands_to_active_session_bookings(
        self,
    ) -> None:
        first_booking = SimpleNamespace(
            id="booking-2",
            booking_session_id="session-1",
            booking_status=BookingStatus.BOOKED,
            seat_number=2,
            created_at=datetime(2026, 7, 7, 0, 1, tzinfo=timezone.utc),
        )
        second_booking = SimpleNamespace(
            id="booking-1",
            booking_session_id="session-1",
            booking_status=BookingStatus.BOOKED,
            seat_number=1,
            created_at=datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc),
        )
        db = AsyncMock()
        db.execute.side_effect = [
            _ExecuteResult(["session-1"]),
            _ExecuteResult([]),
            _ExecuteResult([first_booking, second_booking]),
        ]

        bookings = await resolve_otp_bookings_for_update(
            db,
            trip_id="trip-1",
            otp_code="123456",
        )

        self.assertEqual(
            [booking.id for booking in bookings],
            ["booking-1", "booking-2"],
        )
