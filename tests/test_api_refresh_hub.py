from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.admin.endpoints.router import router as admin_router
from app.db.schema import ScheduledTripStatus, UserRole
from app.realtime.admin_middleware import admin_event_for_mutation
from app.realtime.catalog import EVENT_CATALOG
from app.realtime.events import _departure_allowed
from app.realtime.hub import APIRefreshHub, utcnow


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.close_codes: list[int] = []

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)

    async def close(self, code: int) -> None:
        self.close_codes.append(code)


class APIRefreshHubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.hub = APIRefreshHub()

    async def asyncTearDown(self) -> None:
        await self.hub.shutdown()

    async def test_role_broadcast_is_isolated(self) -> None:
        passenger_socket = FakeWebSocket()
        driver_socket = FakeWebSocket()
        await self.hub.register(
            UserRole.PASSENGER,
            "passenger-1",
            passenger_socket,
        )
        await self.hub.register(
            UserRole.DRIVER,
            "driver-1",
            driver_socket,
        )

        sent = await self.hub.publish(
            UserRole.PASSENGER,
            event="route.created",
            data={"route_id": "route-1"},
        )

        self.assertEqual(sent, 1)
        self.assertEqual(len(driver_socket.messages), 0)
        self.assertEqual(passenger_socket.messages[0]["type"], "api.refresh")
        self.assertEqual(
            passenger_socket.messages[0]["event"],
            "route.created",
        )
        self.assertIn("routes", passenger_socket.messages[0]["resources"])

    async def test_targeted_event_reaches_all_user_devices_only(self) -> None:
        first_device = FakeWebSocket()
        second_device = FakeWebSocket()
        other_user = FakeWebSocket()
        await self.hub.register(UserRole.DRIVER, "driver-1", first_device)
        await self.hub.register(UserRole.DRIVER, "driver-1", second_device)
        await self.hub.register(UserRole.DRIVER, "driver-2", other_user)

        sent = await self.hub.publish(
            UserRole.DRIVER,
            event="trip.start_allowed",
            data={"trip_id": "trip-1"},
            user_ids=["driver-1"],
        )

        self.assertEqual(sent, 2)
        self.assertEqual(len(first_device.messages), 1)
        self.assertEqual(len(second_device.messages), 1)
        self.assertEqual(len(other_user.messages), 0)

    async def test_admin_broadcast_is_isolated_to_admin_connections(
        self,
    ) -> None:
        admin_socket = FakeWebSocket()
        passenger_socket = FakeWebSocket()
        driver_socket = FakeWebSocket()
        await self.hub.register(UserRole.ADMIN, "admin-1", admin_socket)
        await self.hub.register(
            UserRole.PASSENGER,
            "passenger-1",
            passenger_socket,
        )
        await self.hub.register(UserRole.DRIVER, "driver-1", driver_socket)

        sent = await self.hub.publish(
            UserRole.ADMIN,
            event="admin.drivers_changed",
            data={"user_id": "driver-1", "reason": "profile_updated"},
        )

        self.assertEqual(sent, 1)
        self.assertEqual(len(admin_socket.messages), 1)
        self.assertEqual(len(passenger_socket.messages), 0)
        self.assertEqual(len(driver_socket.messages), 0)
        self.assertIn("drivers", admin_socket.messages[0]["resources"])

    async def test_route_event_can_be_published_to_admin(self) -> None:
        admin_socket = FakeWebSocket()
        await self.hub.register(UserRole.ADMIN, "admin-1", admin_socket)

        sent = await self.hub.publish(
            UserRole.ADMIN,
            event="route.updated",
            data={"route_id": "route-1"},
        )

        self.assertEqual(sent, 1)
        self.assertIn("routes", admin_socket.messages[0]["resources"])

    async def test_scheduled_callback_runs(self) -> None:
        callback_ran = asyncio.Event()

        async def callback() -> None:
            callback_ran.set()

        await self.hub.schedule_callback(
            "test-callback",
            utcnow() + timedelta(milliseconds=10),
            callback,
        )

        await asyncio.wait_for(callback_ran.wait(), timeout=1)
        self.assertTrue(callback_ran.is_set())

    async def test_first_stop_is_immediately_departure_eligible(self) -> None:
        db = AsyncMock()
        pending_result = MagicMock()
        pending_result.scalars.return_value.all.return_value = []
        db.execute.return_value = pending_result

        trip = SimpleNamespace(
            id="trip-1",
            status=ScheduledTripStatus.IN_PROGRESS,
        )
        route_stop = SimpleNamespace(
            sequence_no=1,
            stop_id="stop-1",
            assume_time_diff_minutes=0,
            boarding_allowed=True,
            deboarding_allowed=False,
        )
        event = SimpleNamespace(arrival_time=utcnow(), departure_time=None)

        allowed = await _departure_allowed(
            db,
            trip=trip,
            route_stop=route_stop,
            event=event,
        )

        self.assertTrue(allowed)
        db.execute.assert_awaited_once()

    async def test_later_stop_still_respects_assumed_travel_time(self) -> None:
        db = AsyncMock()
        previous_stop_result = MagicMock()
        previous_stop_result.scalar_one_or_none.return_value = SimpleNamespace(
            stop_id="previous-stop"
        )
        previous_event_result = MagicMock()
        previous_event_result.scalar_one_or_none.return_value = SimpleNamespace(
            departure_time=utcnow()
        )
        db.execute.side_effect = [
            previous_stop_result,
            previous_event_result,
        ]

        trip = SimpleNamespace(
            id="trip-1",
            route_id="route-1",
            status=ScheduledTripStatus.IN_PROGRESS,
        )
        route_stop = SimpleNamespace(
            sequence_no=2,
            stop_id="stop-2",
            assume_time_diff_minutes=5,
        )
        event = SimpleNamespace(arrival_time=utcnow(), departure_time=None)

        allowed = await _departure_allowed(
            db,
            trip=trip,
            route_stop=route_stop,
            event=event,
        )

        self.assertFalse(allowed)
        self.assertEqual(db.execute.await_count, 2)


class AdminRefreshMiddlewareMappingTests(unittest.TestCase):
    RICH_DOMAIN_OR_NON_REFRESH_PATHS = {
        "/admin/send-notification/{user_id}",
        "/admin/stops/bulk-upload",
        "/admin/stops/add-single",
        "/admin/stops/{stop_id}",
        "/admin/routes/create",
        "/admin/routes/{route_id}/stops",
        "/admin/routes/{route_id}/toggle",
        "/admin/routes/fares/bulk-set",
        "/admin/trips/{trip_id}/cancel",
        "/admin/trips/{trip_id}/premature-end",
        "/admin/bookings/{booking_id}/noshow",
        "/admin/trips/{trip_id}/complete-manually",
    }

    def test_admin_mutation_categories(self) -> None:
        cases = [
            (
                "DELETE",
                "/admin/users/u-1/devices/s-1",
                "admin.users_changed",
            ),
            ("POST", "/admin/driver/verify/u-1", "admin.drivers_changed"),
            ("POST", "/admin/vehicle/inspect/v-1", "admin.vehicles_changed"),
            ("PATCH", "/admin/rfid/seat-policy", "admin.rfid_changed"),
            ("POST", "/admin/tickets/t-1/action", "admin.support_changed"),
            ("POST", "/admin/resolve-trip/t-1", "admin.incidents_changed"),
            ("POST", "/admin/payouts/bulk-trigger", "admin.payouts_changed"),
            ("PATCH", "/admin/device-settings", "admin.settings_changed"),
            ("PATCH", "/admin/gst/settings", "admin.settings_changed"),
            ("POST", "/admin/commercial-rules", "admin.settings_changed"),
        ]

        for method, path, expected in cases:
            with self.subTest(method=method, path=path):
                self.assertEqual(
                    admin_event_for_mutation(method, path),
                    expected,
                )

    def test_read_and_rich_domain_mutations_are_not_duplicated(self) -> None:
        self.assertIsNone(
            admin_event_for_mutation("GET", "/admin/rfid/cards")
        )
        self.assertIsNone(
            admin_event_for_mutation("POST", "/admin/routes/create")
        )
        self.assertIsNone(
            admin_event_for_mutation("PATCH", "/admin/trips/t-1/cancel")
        )
        self.assertIsNone(
            admin_event_for_mutation("PATCH", "/admin/bookings/b-1/noshow")
        )

    def test_every_admin_mutation_has_a_refresh_strategy(self) -> None:
        mutating_methods = {"POST", "PUT", "PATCH", "DELETE"}
        uncovered: list[tuple[str, str]] = []

        for route in admin_router.routes:
            methods = getattr(route, "methods", set()) & mutating_methods
            for method in methods:
                if (
                    admin_event_for_mutation(method, route.path) is None
                    and route.path not in self.RICH_DOMAIN_OR_NON_REFRESH_PATHS
                ):
                    uncovered.append((method, route.path))

        self.assertEqual(uncovered, [])

    def test_admin_catalog_covers_every_admin_get_surface(self) -> None:
        actual_get_paths = {
            route.path
            for route in admin_router.routes
            if "GET" in getattr(route, "methods", set())
        }
        catalog_paths = {
            endpoint
            for role_catalog in EVENT_CATALOG.values()
            if UserRole.ADMIN in role_catalog
            for endpoint in role_catalog[UserRole.ADMIN].endpoints
        }

        self.assertEqual(actual_get_paths - catalog_paths, set())


if __name__ == "__main__":
    unittest.main()
