from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException

from app.db.schema import UserRole
from app.passenger.service import PassengerService


def stop(
    stop_id: str,
    name: str,
    lat: str,
    lng: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=stop_id,
        name=name,
        lat=Decimal(lat),
        lng=Decimal(lng),
        radius_meters=100,
        is_active=True,
    )


class StopSearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.stops = [
            stop("stop-1", "Eco Space", "22.582931", "88.475144"),
            stop("stop-2", "Karunamoyee", "22.586798", "88.414726"),
            stop("stop-3", "Airport Gate 1", "22.654681", "88.446697"),
        ]
        result = MagicMock()
        result.scalars.return_value.all.return_value = self.stops
        self.db = MagicMock()
        self.db.execute = AsyncMock(return_value=result)
        self.service = PassengerService(self.db)

    async def test_fuzzy_search_tolerates_typos_spaces_and_case(self) -> None:
        response = await self.service.search_stops(
            query="  EKO   spase ",
            lat=None,
            lng=None,
            radius_km=10,
            limit=20,
        )

        self.assertEqual(response["count"], 1)
        self.assertEqual(response["items"][0]["id"], "stop-1")
        self.assertGreater(
            response["items"][0]["name_match_score"],
            0.7,
        )
        self.assertIsNone(response["items"][0]["distance_km"])

    async def test_compact_search_ignores_name_spacing(self) -> None:
        response = await self.service.search_stops(
            query="karunamoyee",
            lat=None,
            lng=None,
            radius_km=10,
            limit=20,
        )

        self.assertEqual(response["items"][0]["id"], "stop-2")
        self.assertEqual(
            response["items"][0]["name_match_score"],
            1.0,
        )

    async def test_nearby_search_filters_and_sorts_by_distance(self) -> None:
        response = await self.service.search_stops(
            query=None,
            lat=22.583,
            lng=88.475,
            radius_km=8,
            limit=20,
        )

        self.assertEqual(
            [item["id"] for item in response["items"]],
            ["stop-1", "stop-2"],
        )
        self.assertLess(
            response["items"][0]["distance_km"],
            response["items"][1]["distance_km"],
        )

    async def test_coordinates_must_be_provided_together(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await self.service.search_stops(
                query=None,
                lat=22.583,
                lng=None,
                radius_km=10,
                limit=20,
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(
            raised.exception.detail["error"],
            "incomplete_coordinates",
        )


class RouteDiscoveryStopValidationTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_normal_discovery_rejects_same_stop(self) -> None:
        service = PassengerService(MagicMock())

        with self.assertRaises(HTTPException) as raised:
            await service.discover_route_trip_options(
                from_stop_id="stop-1",
                to_stop_id="stop-1",
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(
            raised.exception.detail["error"],
            "same_pickup_dropoff",
        )

    async def test_rfid_discovery_rejects_same_stop_before_card_check(self) -> None:
        service = PassengerService(MagicMock())
        current_user = SimpleNamespace(role=UserRole.PASSENGER)

        with self.assertRaises(HTTPException) as raised:
            await service.discover_rfid_route_trip_options(
                current_user,
                from_stop_id="stop-1",
                to_stop_id="stop-1",
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(
            raised.exception.detail["error"],
            "same_pickup_dropoff",
        )


if __name__ == "__main__":
    unittest.main()
