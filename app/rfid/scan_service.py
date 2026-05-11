from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from math import asin, cos, radians, sin, sqrt
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import schema
from app.rfid.scan_schemas import RFIDScanRequest


@dataclass(frozen=True)
class ActiveTripStopContext:
    scheduled_trip: schema.ScheduledTrip
    trip_event: schema.TripEvent
    route_stop: schema.RouteStop
    stop: schema.Stop


class RFIDScanService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    @staticmethod
    def hash_card_uid(card_uid: str) -> str:
        cleaned = card_uid.strip()
        return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()

    @staticmethod
    def _raw_payload_to_json(raw_payload: dict[str, Any] | None) -> str | None:
        if raw_payload is None:
            return None

        return json.dumps(
            raw_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    
    @staticmethod
    def _haversine_distance_meters(
        *,
        lat1: Decimal,
        lng1: Decimal,
        lat2: Decimal,
        lng2: Decimal,
    ) -> Decimal:
        earth_radius_meters = 6_371_000

        lat1_rad, lng1_rad, lat2_rad, lng2_rad = map(
            radians,
            [
                float(lat1),
                float(lng1),
                float(lat2),
                float(lng2),
            ],
        )

        delta_lat = lat2_rad - lat1_rad
        delta_lng = lng2_rad - lng1_rad

        haversine_value = (
            sin(delta_lat / 2) ** 2
            + cos(lat1_rad) * cos(lat2_rad) * sin(delta_lng / 2) ** 2
        )

        central_angle = 2 * asin(sqrt(haversine_value))
        distance = earth_radius_meters * central_angle

        return Decimal(str(round(distance, 2)))

    async def _get_device_by_serial(
        self,
        serial_number: str,
    ) -> schema.RFIDDevice | None:
        stmt = (
            select(schema.RFIDDevice)
            .where(schema.RFIDDevice.serial_number == serial_number)
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_card_by_uid_hash(
        self,
        card_uid_hash: str,
    ) -> schema.RFIDCard | None:
        stmt = (
            select(schema.RFIDCard)
            .where(schema.RFIDCard.card_uid_hash == card_uid_hash)
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()
    
    async def _get_running_trip_for_vehicle(
        self,
        vehicle_id: str,
    ) -> schema.ScheduledTrip | None:
        stmt = (
            select(schema.ScheduledTrip)
            .where(
                schema.ScheduledTrip.vehicle_id == vehicle_id,
                schema.ScheduledTrip.status == schema.ScheduledTripStatus.IN_PROGRESS,
            )
            .order_by(schema.ScheduledTrip.created_at.desc())
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_active_trip_event(
        self,
        scheduled_trip_id: str,
    ) -> schema.TripEvent | None:
        stmt = (
            select(schema.TripEvent)
            .where(
                schema.TripEvent.scheduled_trip_id == scheduled_trip_id,
                schema.TripEvent.arrival_time.is_not(None),
                schema.TripEvent.departure_time.is_(None),
            )
            .order_by(schema.TripEvent.arrival_time.desc())
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_route_stop_for_trip_event(
        self,
        *,
        route_id: str,
        stop_id: str,
    ) -> schema.RouteStop | None:
        stmt = (
            select(schema.RouteStop)
            .where(
                schema.RouteStop.route_id == route_id,
                schema.RouteStop.stop_id == stop_id,
            )
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_stop_or_none(self, stop_id: str) -> schema.Stop | None:
        stmt = select(schema.Stop).where(schema.Stop.id == stop_id).limit(1)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_active_trip_stop_context_for_device(
        self,
        device: schema.RFIDDevice,
    ) -> ActiveTripStopContext | None:
        scheduled_trip = await self._get_running_trip_for_vehicle(device.vehicle_id)

        if scheduled_trip is None:
            return None

        trip_event = await self._get_active_trip_event(scheduled_trip.id)

        if trip_event is None:
            return None

        route_stop = await self._get_route_stop_for_trip_event(
            route_id=scheduled_trip.route_id,
            stop_id=trip_event.stop_id,
        )

        if route_stop is None:
            return None

        stop = await self._get_stop_or_none(trip_event.stop_id)

        if stop is None:
            return None

        return ActiveTripStopContext(
            scheduled_trip=scheduled_trip,
            trip_event=trip_event,
            route_stop=route_stop,
            stop=stop,
        )

    async def record_scan_skeleton(self, payload: RFIDScanRequest) -> dict[str, Any]:
        device = await self._get_device_by_serial(payload.device_serial_number)
        card_uid_hash = self.hash_card_uid(payload.card_uid)
        card = await self._get_card_by_uid_hash(card_uid_hash)

        passenger_user_id = None
        if card is not None:
            passenger_user_id = card.assigned_passenger_user_id

        active_context: ActiveTripStopContext | None = None
        distance_from_stop_meters: Decimal | None = None
        within_radius = False
        rejection_reason = "scan_processing_not_enabled"
        if device is None:
            rejection_reason = "rfid_device_not_found"
        elif device.decommissioned_at is not None:
            rejection_reason = "rfid_device_decommissioned"
        elif device.is_active is False:
            rejection_reason = "rfid_device_inactive"
        elif card is None:
            rejection_reason = "rfid_card_not_found"
        elif card.inventory_status == schema.RFIDCardInventoryStatus.DECOMMISSIONED:
            rejection_reason = "rfid_card_decommissioned"
        elif card.authorization_status != schema.RFIDCardAuthorizationStatus.ALLOWED:
            rejection_reason = "rfid_card_blocked"
        else:
            active_context = await self._get_active_trip_stop_context_for_device(device)

            if active_context is None:
                rejection_reason = "no_active_trip_or_stop"
            elif payload.scan_lat is None or payload.scan_lng is None:
                rejection_reason = "scan_location_required"
            else:
                distance_from_stop_meters = self._haversine_distance_meters(
                    lat1=payload.scan_lat,
                    lng1=payload.scan_lng,
                    lat2=active_context.stop.lat,
                    lng2=active_context.stop.lng,
                )

                within_radius = (
                    distance_from_stop_meters
                    <= Decimal(active_context.stop.radius_meters or 0)
                )

                if not within_radius:
                    rejection_reason = "not_within_active_stop_radius"

        scan_event = schema.RFIDScanEvent(
            scan_type=schema.RFIDScanType.BOARD,
            device_id=None if device is None else device.id,
            device_serial_snapshot=payload.device_serial_number,
            card_id=None if card is None else card.id,
            card_uid_hash_snapshot=card_uid_hash,
            passenger_user_id=passenger_user_id,
            scheduled_trip_id=None
            if active_context is None
            else active_context.scheduled_trip.id,
            route_id=None if active_context is None else active_context.scheduled_trip.route_id,
            vehicle_id=None if device is None else device.vehicle_id,
            driver_user_id=None
            if active_context is None
            else active_context.scheduled_trip.driver_user_id,
            matched_stop_id=None if active_context is None else active_context.stop.id,
            matched_route_stop_id=None
            if active_context is None
            else active_context.route_stop.id,
            matched_sequence_no=None
            if active_context is None
            else active_context.route_stop.sequence_no,
            active_trip_event_id=None
            if active_context is None
            else active_context.trip_event.id,
            active_stop_arrival_time_snapshot=None
            if active_context is None
            else active_context.trip_event.arrival_time,
            active_stop_departure_time_snapshot=None
            if active_context is None
            else active_context.trip_event.departure_time,
            scan_lat=payload.scan_lat,
            scan_lng=payload.scan_lng,
            within_radius=within_radius,
            distance_from_stop_meters=distance_from_stop_meters,
            accepted=False,
            rejection_reason=rejection_reason,
            raw_payload_json=self._raw_payload_to_json(payload.raw_payload),
        )

        if device is not None:
            device.last_seen_at = schema.utcnow()
            device.last_seen_lat = payload.scan_lat
            device.last_seen_lng = payload.scan_lng
            self.db.add(device)

        self.db.add(scan_event)
        await self.db.flush()

        return {
            "accepted": False,
            "scan_event_id": scan_event.id,
            "scan_type": scan_event.scan_type,
            "rejection_reason": rejection_reason,
            "message": "RFID scan recorded. Boarding/deboarding is not enabled yet.",
            "device_id": scan_event.device_id,
            "card_id": scan_event.card_id,
            "passenger_user_id": scan_event.passenger_user_id,
            "scheduled_trip_id": scan_event.scheduled_trip_id,
            "route_id": scan_event.route_id,
            "vehicle_id": scan_event.vehicle_id,
            "driver_user_id": scan_event.driver_user_id,
            "matched_stop_id": scan_event.matched_stop_id,
            "matched_sequence_no": scan_event.matched_sequence_no,
        }