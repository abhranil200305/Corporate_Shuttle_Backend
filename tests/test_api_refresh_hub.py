from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.db.schema import ScheduledTripStatus, UserRole
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

    async def test_departure_policy_blocks_allowed_event(self) -> None:
        db = AsyncMock()
        trip = SimpleNamespace(status=ScheduledTripStatus.IN_PROGRESS)
        route_stop = SimpleNamespace(deboarding_allowed=False)
        event = SimpleNamespace(arrival_time=utcnow(), departure_time=None)

        allowed = await _departure_allowed(
            db,
            trip=trip,
            route_stop=route_stop,
            event=event,
        )

        self.assertFalse(allowed)
        db.execute.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
