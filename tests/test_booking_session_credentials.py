from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException

from app.db.schema import (
    BookingSessionStatus,
    BookingStatus,
    ScanType,
    ScheduledTripStatus,
)
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
        self.assertEqual(payload["booking_id"], "booking-1")
        self.assertEqual(payload["scheduled_trip_id"], "trip-1")

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

    def test_in_progress_trip_remains_current_after_planned_end(self) -> None:
        now = datetime(2026, 7, 7, 10, 0, tzinfo=timezone.utc)
        trip = SimpleNamespace(
            status=ScheduledTripStatus.IN_PROGRESS,
            planned_start_at=now - timedelta(hours=2),
            planned_end_at=now - timedelta(minutes=10),
            actual_start_at=now - timedelta(hours=2),
            actual_end_at=None,
        )

        self.assertTrue(
            self.service._is_current_trip_for_passenger(trip, now)
        )

    def test_completed_trip_is_not_current_after_planned_end(self) -> None:
        now = datetime(2026, 7, 7, 10, 0, tzinfo=timezone.utc)
        trip = SimpleNamespace(
            status=ScheduledTripStatus.COMPLETED,
            planned_start_at=now - timedelta(hours=2),
            planned_end_at=now - timedelta(minutes=10),
            actual_start_at=now - timedelta(hours=2),
            actual_end_at=now - timedelta(minutes=5),
        )

        self.assertFalse(
            self.service._is_current_trip_for_passenger(trip, now)
        )

    def test_segment_stops_mark_actual_early_drop_stop(self) -> None:
        planned_start = datetime(2026, 7, 7, 10, 0, tzinfo=timezone.utc)

        def stop(stop_id: str, name: str) -> SimpleNamespace:
            return SimpleNamespace(
                id=stop_id,
                name=name,
                lat=0,
                lng=0,
                radius_meters=100,
                is_active=True,
            )

        stop_a = stop("stop-a", "Pickup")
        stop_b = stop("stop-b", "Early drop")
        stop_c = stop("stop-c", "Booked drop")
        route_stops = [
            SimpleNamespace(
                id="rs-a",
                stop_id="stop-a",
                sequence_no=1,
                assume_time_diff_minutes=0,
                stop=stop_a,
            ),
            SimpleNamespace(
                id="rs-b",
                stop_id="stop-b",
                sequence_no=2,
                assume_time_diff_minutes=10,
                stop=stop_b,
            ),
            SimpleNamespace(
                id="rs-c",
                stop_id="stop-c",
                sequence_no=3,
                assume_time_diff_minutes=10,
                stop=stop_c,
            ),
        ]
        trip = SimpleNamespace(
            route=SimpleNamespace(route_stops=route_stops),
            trip_events=[],
            planned_start_at=planned_start,
            status=ScheduledTripStatus.COMPLETED,
        )
        booking = SimpleNamespace(
            scheduled_trip=trip,
            pickup_sequence_no_snapshot=1,
            dropoff_sequence_no_snapshot=3,
            pickup_stop_id="stop-a",
            dropoff_stop_id="stop-c",
            completed_near_stop_id="stop-b",
            boarded_at=planned_start,
            completed_at=planned_start + timedelta(minutes=12),
            booking_status=BookingStatus.COMPLETED,
            scan_events=[
                SimpleNamespace(
                    scan_type=ScanType.DROP,
                    within_radius=True,
                    matched_stop_id="stop-b",
                )
            ],
        )

        segment_stops = self.service._serialize_segment_stops(booking)

        self.assertEqual(
            [
                (item["stop"]["id"], item["stop_status"])
                for item in segment_stops
            ],
            [
                ("stop-a", "passed"),
                ("stop-b", "dropped_here"),
                ("stop-c", "upcoming"),
            ],
        )
        self.assertTrue(segment_stops[1]["is_actual_drop_stop"])
        self.assertFalse(segment_stops[2]["is_actual_drop_stop"])


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

    async def test_session_otp_requires_seat_selection_for_multiple_seats(
        self,
    ) -> None:
        first_booking = SimpleNamespace(
            id="booking-2",
            booking_session_id="session-1",
            booking_status=BookingStatus.BOOKED,
            seat_number=2,
            traveller_profile_id=None,
            traveller_name_snapshot="Second",
            traveller_phone_snapshot=None,
            traveller_relationship_label_snapshot=None,
            created_at=datetime(2026, 7, 7, 0, 1, tzinfo=timezone.utc),
        )
        second_booking = SimpleNamespace(
            id="booking-1",
            booking_session_id="session-1",
            booking_status=BookingStatus.BOOKED,
            seat_number=1,
            traveller_profile_id=None,
            traveller_name_snapshot="First",
            traveller_phone_snapshot=None,
            traveller_relationship_label_snapshot=None,
            created_at=datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc),
        )
        db = AsyncMock()
        db.execute.side_effect = [
            _ExecuteResult(["session-1"]),
            _ExecuteResult([]),
            _ExecuteResult([first_booking, second_booking]),
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
            "session_credential_requires_seat_selection",
        )
        self.assertEqual(
            [
                item["seat_number"]
                for item in ctx.exception.detail["eligible_bookings"]
            ],
            [1, 2],
        )

    async def test_session_otp_target_seat_selects_one_booking(self) -> None:
        first_booking = SimpleNamespace(
            id="booking-2",
            booking_session_id="session-1",
            booking_status=BookingStatus.BOOKED,
            seat_number=2,
            traveller_profile_id=None,
            traveller_name_snapshot="Second",
            traveller_phone_snapshot=None,
            traveller_relationship_label_snapshot=None,
            created_at=datetime(2026, 7, 7, 0, 1, tzinfo=timezone.utc),
        )
        second_booking = SimpleNamespace(
            id="booking-1",
            booking_session_id="session-1",
            booking_status=BookingStatus.BOOKED,
            seat_number=1,
            traveller_profile_id=None,
            traveller_name_snapshot="First",
            traveller_phone_snapshot=None,
            traveller_relationship_label_snapshot=None,
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
            target_seat_number=2,
        )

        self.assertEqual(
            [booking.id for booking in bookings],
            ["booking-2"],
        )
