from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.db.schema import ScheduledTripStatus
from app.driver.trips.cancel_trip import cancel_trip


class DriverTripCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_without_passengers_still_returns_metadata(self) -> None:
        now = datetime.now(timezone.utc)
        trip = SimpleNamespace(
            id="trip-1",
            driver_user_id="driver-1",
            route_id="route-1",
            status=ScheduledTripStatus.SCHEDULED,
            planned_start_at=now + timedelta(hours=2),
            planned_end_at=now + timedelta(hours=3),
            cancellation_reason=None,
            cancelled_at=None,
            cancellation_source=None,
            cancelled_by_user_id=None,
        )
        driver = SimpleNamespace(id="driver-1")
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(ws_hub=None))
        )

        trip_result = MagicMock()
        trip_result.scalars.return_value.first.return_value = trip
        bookings_result = MagicMock()
        bookings_result.scalars.return_value.all.return_value = []

        db = AsyncMock()
        db.execute.side_effect = [trip_result, bookings_result, MagicMock()]

        refresh_hub = MagicMock()
        refresh_hub.cancel_scheduled = AsyncMock()
        fine_service = MagicMock()
        fine_service.register_driver_trip_cancellation_fines = AsyncMock(
            return_value={"registered_count": 0}
        )

        with (
            patch(
                "app.driver.trips.cancel_trip.FineRegisterService",
                return_value=fine_service,
            ),
            patch("app.driver.trips.cancel_trip.NotificationService"),
            patch(
                "app.driver.trips.cancel_trip.get_api_refresh_hub",
                return_value=refresh_hub,
            ),
            patch(
                "app.driver.trips.cancel_trip.publish_trip_event",
                new_callable=AsyncMock,
            ) as publish_trip_event,
        ):
            response = await cancel_trip(
                request=request,
                trip_id=trip.id,
                cancellation_reason=None,
                current_driver=driver,
                db=db,
            )

        expected_metadata = {
            "cancelled_at": trip.cancelled_at.isoformat(),
            "reason": "Trip cancelled by driver.",
            "source": "driver",
            "cancelled_by_user_id": driver.id,
        }
        self.assertEqual(response["cancellation_metadata"], expected_metadata)
        self.assertEqual(response["notified_passengers"], 0)
        self.assertEqual(trip.status, ScheduledTripStatus.CANCELLED)
        db.commit.assert_awaited_once()
        publish_trip_event.assert_awaited_once()
        self.assertEqual(
            publish_trip_event.await_args.kwargs["data"][
                "cancellation_metadata"
            ],
            expected_metadata,
        )


if __name__ == "__main__":
    unittest.main()
