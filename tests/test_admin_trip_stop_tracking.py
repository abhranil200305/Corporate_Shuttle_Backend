from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from app.admin.logic.service import AdminService
from app.admin.structs.dto import AdminTripStopTrackingResponse
from app.db.schema import ScheduledTripStatus


def stop(stop_id: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=stop_id,
        name=name,
        lat=Decimal("22.572600"),
        lng=Decimal("88.363900"),
        radius_meters=100,
    )


def route_stop(
    route_stop_id: str,
    sequence_no: int,
    route_stop: SimpleNamespace,
    assume_minutes: int,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=route_stop_id,
        stop_id=route_stop.id,
        stop=route_stop,
        sequence_no=sequence_no,
        assume_time_diff_minutes=assume_minutes,
        boarding_allowed=True,
        deboarding_allowed=True,
    )


class AdminTripStopTrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = AdminService(MagicMock())
        self.started_at = datetime(2026, 7, 16, 3, 30, tzinfo=timezone.utc)
        first = route_stop("rs-1", 1, stop("stop-1", "First Stop"), 0)
        second = route_stop("rs-2", 2, stop("stop-2", "Second Stop"), 10)
        third = route_stop("rs-3", 3, stop("stop-3", "Third Stop"), 15)
        self.trip = SimpleNamespace(
            id="trip-1",
            status=ScheduledTripStatus.IN_PROGRESS,
            planned_start_at=self.started_at,
            planned_end_at=self.started_at + timedelta(hours=1),
            actual_start_at=self.started_at,
            actual_end_at=None,
            updated_at=self.started_at + timedelta(minutes=17),
            last_lat=Decimal("22.572601"),
            last_lng=Decimal("88.363901"),
            route=SimpleNamespace(
                id="route-1",
                name="Office Route",
                code="OR-1",
                has_ac=True,
                route_stops=[third, first, second],
            ),
            driver=SimpleNamespace(
                id="driver-1",
                email="driver@example.com",
                driver_profile=SimpleNamespace(full_name="Driver Name"),
            ),
            vehicle=SimpleNamespace(
                id="vehicle-1",
                registration_number="WB01AB1234",
                vehicle_name="Shuttle 1",
                vehicle_model="Model X",
            ),
            trip_events=[
                SimpleNamespace(
                    id="event-1",
                    stop_id="stop-1",
                    arrival_time=self.started_at + timedelta(minutes=1),
                    departure_time=self.started_at + timedelta(minutes=3),
                ),
                SimpleNamespace(
                    id="event-2",
                    stop_id="stop-2",
                    arrival_time=self.started_at + timedelta(minutes=15),
                    departure_time=None,
                ),
                SimpleNamespace(
                    id="event-3",
                    stop_id="stop-3",
                    arrival_time=None,
                    departure_time=None,
                ),
            ],
        )

    def test_in_progress_trip_exposes_current_last_next_and_timeline(self) -> None:
        payload = self.service._serialize_trip_stop_tracking(self.trip)
        parsed = AdminTripStopTrackingResponse.model_validate(payload)

        self.assertTrue(parsed.is_current_trip)
        self.assertEqual(parsed.status, "in_progress")
        self.assertEqual(parsed.progress.position_state, "at_stop")
        self.assertEqual(parsed.progress.total_stops, 3)
        self.assertEqual(parsed.progress.arrived_stops, 2)
        self.assertEqual(parsed.progress.departed_stops, 1)
        self.assertEqual(parsed.progress.remaining_stops, 1)
        self.assertAlmostEqual(parsed.progress.progress_percent, 66.67)

        self.assertEqual(parsed.current_stop.stop_id, "stop-2")
        self.assertEqual(parsed.current_stop.action, "arrived")
        self.assertEqual(parsed.last_action.stop_id, "stop-2")
        self.assertEqual(parsed.last_arrived_stop.stop_id, "stop-2")
        self.assertEqual(parsed.last_departed_stop.stop_id, "stop-1")
        self.assertEqual(parsed.next_stop.stop_id, "stop-3")

        self.assertEqual(
            [(item.action, item.stop_id) for item in parsed.actions],
            [
                ("arrived", "stop-1"),
                ("departed", "stop-1"),
                ("arrived", "stop-2"),
            ],
        )
        self.assertEqual(
            [item.state for item in parsed.stops],
            ["departed", "arrived", "upcoming"],
        )
        self.assertFalse(parsed.stops[0].is_current_stop)
        self.assertTrue(parsed.stops[1].is_current_stop)
        self.assertTrue(parsed.stops[2].is_next_stop)
        self.assertEqual(
            parsed.stops[2].planned_time_at_stop,
            self.started_at + timedelta(minutes=25),
        )

    def test_between_stops_uses_last_departure_and_next_stop(self) -> None:
        second_event = self.trip.trip_events[1]
        second_event.departure_time = self.started_at + timedelta(minutes=17)

        parsed = AdminTripStopTrackingResponse.model_validate(
            self.service._serialize_trip_stop_tracking(self.trip)
        )

        self.assertIsNone(parsed.current_stop)
        self.assertEqual(parsed.progress.position_state, "between_stops")
        self.assertEqual(parsed.last_action.action, "departed")
        self.assertEqual(parsed.last_action.stop_id, "stop-2")
        self.assertEqual(parsed.next_stop.stop_id, "stop-3")

    def test_terminal_trip_marks_unvisited_stops_and_has_no_next_stop(self) -> None:
        self.trip.status = ScheduledTripStatus.PREMATURE_END
        self.trip.actual_end_at = self.started_at + timedelta(minutes=20)

        parsed = AdminTripStopTrackingResponse.model_validate(
            self.service._serialize_trip_stop_tracking(self.trip)
        )

        self.assertFalse(parsed.is_current_trip)
        self.assertEqual(parsed.progress.position_state, "finished")
        self.assertIsNone(parsed.current_stop)
        self.assertIsNone(parsed.next_stop)
        self.assertEqual(parsed.stops[2].state, "not_visited")


if __name__ == "__main__":
    unittest.main()
