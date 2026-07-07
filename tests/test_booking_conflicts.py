from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException

from app.db.schema import BookingStatus, ScheduledTripStatus
from app.passenger.booking_conflicts import (
    build_guest_traveller_identity,
    build_profile_traveller_identity,
    build_self_traveller_identity,
    journey_windows_conflict,
    normalize_phone_for_identity,
    route_segments_overlap,
)
from app.passenger.service import PassengerService


class TravellerIdentityTests(unittest.TestCase):
    def test_self_and_profile_identities_are_stable_and_distinct(self) -> None:
        self.assertEqual(build_self_traveller_identity("u-1"), "self:u-1")
        self.assertEqual(
            build_profile_traveller_identity("p-1"),
            "profile:p-1",
        )

    def test_guest_phone_formatting_does_not_change_identity(self) -> None:
        formatted = build_guest_traveller_identity(
            "u-1", "+91 98765-43210"
        )
        digits_only = build_guest_traveller_identity(
            "u-1", "919876543210"
        )

        self.assertEqual(formatted, digits_only)
        self.assertNotIn("919876543210", formatted)

    def test_guest_identity_is_scoped_to_booking_owner(self) -> None:
        first_owner = build_guest_traveller_identity("u-1", "9876543210")
        second_owner = build_guest_traveller_identity("u-2", "9876543210")

        self.assertNotEqual(first_owner, second_owner)

    def test_phone_normalization_requires_a_digit(self) -> None:
        with self.assertRaises(ValueError):
            normalize_phone_for_identity("+-()")


class RouteSegmentConflictTests(unittest.TestCase):
    def test_overlapping_segments_conflict(self) -> None:
        self.assertTrue(
            route_segments_overlap(
                existing_pickup_sequence_no=1,
                existing_dropoff_sequence_no=4,
                requested_pickup_sequence_no=3,
                requested_dropoff_sequence_no=5,
            )
        )

    def test_adjacent_segments_do_not_conflict(self) -> None:
        self.assertFalse(
            route_segments_overlap(
                existing_pickup_sequence_no=1,
                existing_dropoff_sequence_no=3,
                requested_pickup_sequence_no=3,
                requested_dropoff_sequence_no=5,
            )
        )

    def test_disjoint_segments_do_not_conflict(self) -> None:
        self.assertFalse(
            route_segments_overlap(
                existing_pickup_sequence_no=1,
                existing_dropoff_sequence_no=2,
                requested_pickup_sequence_no=4,
                requested_dropoff_sequence_no=5,
            )
        )


class JourneyWindowConflictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = datetime(2026, 7, 6, 8, 0, tzinfo=timezone.utc)

    def _conflicts(
        self,
        *,
        requested_start_minutes: int,
        requested_end_minutes: int,
        existing_dropoff_stop_id: str = "stop-b",
        requested_pickup_stop_id: str = "stop-c",
    ) -> bool:
        return journey_windows_conflict(
            existing_start=self.base,
            existing_end=self.base + timedelta(minutes=60),
            existing_pickup_stop_id="stop-a",
            existing_dropoff_stop_id=existing_dropoff_stop_id,
            requested_start=(
                self.base + timedelta(minutes=requested_start_minutes)
            ),
            requested_end=(
                self.base + timedelta(minutes=requested_end_minutes)
            ),
            requested_pickup_stop_id=requested_pickup_stop_id,
            requested_dropoff_stop_id="stop-d",
        )

    def test_overlapping_trip_times_conflict_across_routes(self) -> None:
        self.assertTrue(
            self._conflicts(
                requested_start_minutes=45,
                requested_end_minutes=90,
            )
        )

    def test_touching_times_at_same_stop_are_allowed(self) -> None:
        self.assertFalse(
            self._conflicts(
                requested_start_minutes=60,
                requested_end_minutes=90,
                existing_dropoff_stop_id="stop-b",
                requested_pickup_stop_id="stop-b",
            )
        )

    def test_touching_times_at_different_stops_conflict(self) -> None:
        self.assertTrue(
            self._conflicts(
                requested_start_minutes=60,
                requested_end_minutes=90,
            )
        )

    def test_exact_fifteen_minute_transfer_is_allowed(self) -> None:
        self.assertFalse(
            self._conflicts(
                requested_start_minutes=75,
                requested_end_minutes=105,
            )
        )

    def test_transfer_under_fifteen_minutes_conflicts(self) -> None:
        self.assertTrue(
            self._conflicts(
                requested_start_minutes=74,
                requested_end_minutes=105,
            )
        )

    def test_rule_is_symmetric_when_earlier_trip_is_booked_second(
        self,
    ) -> None:
        self.assertTrue(
            journey_windows_conflict(
                existing_start=self.base + timedelta(minutes=60),
                existing_end=self.base + timedelta(minutes=120),
                existing_pickup_stop_id="stop-c",
                existing_dropoff_stop_id="stop-d",
                requested_start=self.base,
                requested_end=self.base + timedelta(minutes=50),
                requested_pickup_stop_id="stop-a",
                requested_dropoff_stop_id="stop-b",
            )
        )


class PassengerCurrentTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PassengerService(MagicMock())
        self.now = datetime(2026, 7, 7, 10, 0, tzinfo=timezone.utc)

    def _trip(self, status: ScheduledTripStatus):
        return SimpleNamespace(
            status=status,
            planned_start_at=self.now - timedelta(hours=2),
            planned_end_at=self.now - timedelta(minutes=10),
            actual_start_at=self.now - timedelta(hours=2),
            actual_end_at=None,
        )

    def test_live_trip_remains_current_after_planned_end(self) -> None:
        trip = self._trip(ScheduledTripStatus.IN_PROGRESS)

        self.assertTrue(
            self.service._is_current_trip_for_passenger(trip, self.now)
        )

    def test_completed_passenger_booking_is_not_current_on_live_trip(
        self,
    ) -> None:
        booking = SimpleNamespace(
            booking_status=BookingStatus.COMPLETED,
            completed_at=self.now - timedelta(minutes=5),
            scheduled_trip=self._trip(ScheduledTripStatus.IN_PROGRESS),
        )

        self.assertFalse(
            self.service._is_current_booking_for_passenger(booking, self.now)
        )


class PassengerServiceConflictTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = PassengerService(MagicMock())
        self.service._lock_traveller_identity_keys = AsyncMock()
        self.service._list_active_bookings_for_traveller_identities = (
            AsyncMock(return_value=[])
        )
        self.base = datetime(2026, 7, 6, 8, 0, tzinfo=timezone.utc)

    @staticmethod
    def _trip(
        trip_id: str,
        planned_start_at: datetime,
        *,
        first_stop_id: str = "stop-a",
        second_stop_id: str = "stop-b",
        duration_minutes: int = 60,
    ):
        return SimpleNamespace(
            id=trip_id,
            planned_start_at=planned_start_at,
            route=SimpleNamespace(
                route_stops=[
                    SimpleNamespace(
                        sequence_no=1,
                        stop_id=first_stop_id,
                        assume_time_diff_minutes=0,
                    ),
                    SimpleNamespace(
                        sequence_no=2,
                        stop_id=second_stop_id,
                        assume_time_diff_minutes=duration_minutes,
                    ),
                ]
            ),
        )

    async def test_service_allows_adjacent_legs_on_same_trip(self) -> None:
        trip = self._trip("trip-1", self.base)
        booking = SimpleNamespace(
            id="booking-1",
            traveller_identity_key="profile:p-1",
            scheduled_trip_id="trip-1",
            pickup_sequence_no_snapshot=1,
            dropoff_sequence_no_snapshot=2,
        )
        self.service._list_active_bookings_for_traveller_identities = (
            AsyncMock(return_value=[booking])
        )

        await self.service._ensure_traveller_bookings_do_not_conflict(
            trip=trip,
            pickup_stop_id="stop-b",
            dropoff_stop_id="stop-c",
            pickup_sequence_no=2,
            dropoff_sequence_no=3,
            traveller_requests=[("profile:p-1", 4)],
        )

    async def test_service_rejects_overlapping_leg_on_same_trip(self) -> None:
        trip = self._trip("trip-1", self.base)
        booking = SimpleNamespace(
            id="booking-1",
            traveller_identity_key="profile:p-1",
            scheduled_trip_id="trip-1",
            pickup_sequence_no_snapshot=1,
            dropoff_sequence_no_snapshot=3,
        )
        self.service._list_active_bookings_for_traveller_identities = (
            AsyncMock(return_value=[booking])
        )

        with self.assertRaises(HTTPException) as raised:
            await self.service._ensure_traveller_bookings_do_not_conflict(
                trip=trip,
                pickup_stop_id="stop-b",
                dropoff_stop_id="stop-c",
                pickup_sequence_no=2,
                dropoff_sequence_no=4,
                traveller_requests=[("profile:p-1", 4)],
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["conflict_type"],
            "overlapping_route_segment",
        )

    async def test_service_rejects_overlapping_different_trips(self) -> None:
        existing_trip = self._trip("trip-1", self.base)
        requested_trip = self._trip(
            "trip-2",
            self.base + timedelta(minutes=45),
            first_stop_id="stop-c",
            second_stop_id="stop-d",
        )
        booking = SimpleNamespace(
            id="booking-1",
            traveller_identity_key="profile:p-1",
            scheduled_trip_id="trip-1",
            scheduled_trip=existing_trip,
            pickup_stop_id="stop-a",
            dropoff_stop_id="stop-b",
            pickup_sequence_no_snapshot=1,
            dropoff_sequence_no_snapshot=2,
            booking_status=BookingStatus.BOOKED,
        )
        self.service._list_active_bookings_for_traveller_identities = (
            AsyncMock(return_value=[booking])
        )

        with self.assertRaises(HTTPException) as raised:
            await self.service._ensure_traveller_bookings_do_not_conflict(
                trip=requested_trip,
                pickup_stop_id="stop-c",
                dropoff_stop_id="stop-d",
                pickup_sequence_no=1,
                dropoff_sequence_no=2,
                traveller_requests=[("profile:p-1", 4)],
            )

        self.assertEqual(
            raised.exception.detail["conflict_type"],
            "overlapping_trip_window",
        )

    async def test_service_rejects_same_traveller_twice_in_session(
        self,
    ) -> None:
        trip = self._trip("trip-1", self.base)

        with self.assertRaises(HTTPException) as raised:
            await self.service._ensure_traveller_bookings_do_not_conflict(
                trip=trip,
                pickup_stop_id="stop-a",
                dropoff_stop_id="stop-b",
                pickup_sequence_no=1,
                dropoff_sequence_no=2,
                traveller_requests=[
                    ("self:u-1", 1),
                    ("self:u-1", 2),
                ],
            )

        self.assertEqual(
            raised.exception.detail["error"],
            "duplicate_traveller_in_booking_session",
        )
        self.service._lock_traveller_identity_keys.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
