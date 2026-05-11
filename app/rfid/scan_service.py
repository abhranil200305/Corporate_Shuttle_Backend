from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import schema
from app.rfid.scan_schemas import RFIDScanRequest


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

    async def record_scan_skeleton(self, payload: RFIDScanRequest) -> dict[str, Any]:
        device = await self._get_device_by_serial(payload.device_serial_number)
        card_uid_hash = self.hash_card_uid(payload.card_uid)
        card = await self._get_card_by_uid_hash(card_uid_hash)

        passenger_user_id = None
        if card is not None:
            passenger_user_id = card.assigned_passenger_user_id

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

        scan_event = schema.RFIDScanEvent(
            scan_type=schema.RFIDScanType.BOARD,
            device_id=None if device is None else device.id,
            device_serial_snapshot=payload.device_serial_number,
            card_id=None if card is None else card.id,
            card_uid_hash_snapshot=card_uid_hash,
            passenger_user_id=passenger_user_id,
            vehicle_id=None if device is None else device.vehicle_id,
            scan_lat=payload.scan_lat,
            scan_lng=payload.scan_lng,
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