from __future__ import annotations

import hashlib
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.rfid_schemas import (
    RFIDCardAssignRequest,
    RFIDCardBlockRequest,
    RFIDCardBulkRegisterRequest,
    RFIDCardDecommissionRequest,
    RFIDCardRegisterRequest,
    RFIDCardReturnRequest,
    RFIDCardUnassignRequest,
    RFIDDeviceCreateRequest,
    RFIDDeviceUpdateRequest,
    RFIDRechargeCreateRequest,
)
from app.db import schema


class AdminRFIDService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ============================================================
    # card UID helpers
    # ============================================================

    @staticmethod
    def _clean_required_text(value: str, *, field_name: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{field_name} cannot be empty.")
        return cleaned

    @staticmethod
    def hash_card_uid(card_uid: str) -> str:
        cleaned = card_uid.strip()
        return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()

    @staticmethod
    def mask_card_uid(card_uid: str) -> str:
        cleaned = card_uid.strip()

        if len(cleaned) <= 4:
            return f"****{cleaned}"

        return f"****{cleaned[-4:]}"

    @staticmethod
    def _available_balance(account: schema.RFIDCardAccount) -> Decimal:
        return Decimal(account.current_balance or 0) - Decimal(account.held_balance or 0)
    
    # ============================================================
    # shared DB guards
    # ============================================================

    async def _get_vehicle_or_404(self, vehicle_id: str) -> schema.Vehicle:
        stmt = select(schema.Vehicle).where(schema.Vehicle.id == vehicle_id)
        result = await self.db.execute(stmt)
        vehicle = result.scalar_one_or_none()

        if vehicle is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "vehicle_not_found",
                    "message": "Vehicle not found.",
                },
            )

        return vehicle

    async def _get_device_or_404(self, device_id: str) -> schema.RFIDDevice:
        stmt = select(schema.RFIDDevice).where(schema.RFIDDevice.id == device_id)
        result = await self.db.execute(stmt)
        device = result.scalar_one_or_none()

        if device is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "rfid_device_not_found",
                    "message": "RFID device not found.",
                },
            )

        return device

    async def _ensure_vehicle_has_no_running_trip(self, vehicle_id: str) -> None:
        stmt = (
            select(func.count(schema.ScheduledTrip.id))
            .where(
                schema.ScheduledTrip.vehicle_id == vehicle_id,
                schema.ScheduledTrip.status == schema.ScheduledTripStatus.IN_PROGRESS,
            )
        )
        result = await self.db.execute(stmt)
        running_trip_count = int(result.scalar_one() or 0)

        if running_trip_count > 0:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "vehicle_has_running_trip",
                    "message": "RFID device changes are not allowed while the vehicle has a running scheduled trip.",
                },
            )

    async def _flush_or_conflict(self, *, conflict_message: str) -> None:
        try:
            await self.db.flush()
        except IntegrityError as exc:
            await self.db.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_device_conflict",
                    "message": conflict_message,
                },
            ) from exc

    # ============================================================
    # RFID device admin operations
    # ============================================================

    async def create_device(
        self,
        payload: RFIDDeviceCreateRequest,
    ) -> schema.RFIDDevice:
        await self._get_vehicle_or_404(payload.vehicle_id)
        await self._ensure_vehicle_has_no_running_trip(payload.vehicle_id)

        device = schema.RFIDDevice(
            serial_number=payload.serial_number,
            vehicle_id=payload.vehicle_id,
            is_active=payload.is_active,
            notes=payload.notes,
        )

        self.db.add(device)
        await self._flush_or_conflict(
            conflict_message="An RFID device with this serial number already exists.",
        )

        return device

    async def list_devices(
        self,
        *,
        page: int,
        page_size: int,
        vehicle_id: str | None = None,
        is_active: bool | None = None,
    ) -> tuple[list[schema.RFIDDevice], int]:
        filters = []

        if vehicle_id is not None:
            filters.append(schema.RFIDDevice.vehicle_id == vehicle_id)

        if is_active is not None:
            filters.append(schema.RFIDDevice.is_active.is_(is_active))

        count_stmt = select(func.count(schema.RFIDDevice.id))
        list_stmt = select(schema.RFIDDevice)

        if filters:
            count_stmt = count_stmt.where(*filters)
            list_stmt = list_stmt.where(*filters)

        list_stmt = (
            list_stmt
            .order_by(schema.RFIDDevice.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        count_result = await self.db.execute(count_stmt)
        list_result = await self.db.execute(list_stmt)

        return list(list_result.scalars().all()), int(count_result.scalar_one() or 0)

    async def update_device(
        self,
        *,
        device_id: str,
        payload: RFIDDeviceUpdateRequest,
    ) -> schema.RFIDDevice:
        device = await self._get_device_or_404(device_id)

        await self._ensure_vehicle_has_no_running_trip(device.vehicle_id)

        if (
            "vehicle_id" in payload.model_fields_set
            and payload.vehicle_id is not None
            and payload.vehicle_id != device.vehicle_id
        ):
            await self._get_vehicle_or_404(payload.vehicle_id)
            await self._ensure_vehicle_has_no_running_trip(payload.vehicle_id)
            device.vehicle_id = payload.vehicle_id

        if (
            "serial_number" in payload.model_fields_set
            and payload.serial_number is not None
        ):
            device.serial_number = payload.serial_number

        if "is_active" in payload.model_fields_set and payload.is_active is not None:
            device.is_active = payload.is_active

        if "notes" in payload.model_fields_set:
            device.notes = payload.notes

        self.db.add(device)
        await self._flush_or_conflict(
            conflict_message="An RFID device with this serial number already exists.",
        )

        return device

    async def set_device_active(
        self,
        *,
        device_id: str,
        is_active: bool,
    ) -> schema.RFIDDevice:
        device = await self._get_device_or_404(device_id)

        await self._ensure_vehicle_has_no_running_trip(device.vehicle_id)

        if device.decommissioned_at is not None and is_active:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_device_decommissioned",
                    "message": "A decommissioned RFID device cannot be activated.",
                },
            )

        device.is_active = is_active
        self.db.add(device)
        await self.db.flush()

        return device

    async def decommission_device(self, device_id: str) -> schema.RFIDDevice:
        device = await self._get_device_or_404(device_id)

        await self._ensure_vehicle_has_no_running_trip(device.vehicle_id)

        device.is_active = False
        device.decommissioned_at = schema.utcnow()

        self.db.add(device)
        await self.db.flush()

        return device
    
    async def list_device_vehicle_options(
        self,
        *,
        page: int,
        page_size: int,
        q: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        filters = [
            schema.User.role == schema.UserRole.DRIVER,
        ]

        cleaned_q = q.strip() if q is not None else None

        if cleaned_q:
            search = f"%{cleaned_q}%"
            filters.append(
                or_(
                    schema.DriverProfile.full_name.ilike(search),
                    schema.Vehicle.registration_number.ilike(search),
                    schema.Vehicle.driver_user_id.ilike(search),
                )
            )

        count_stmt = (
            select(func.count(schema.Vehicle.id))
            .select_from(schema.Vehicle)
            .join(schema.User, schema.User.id == schema.Vehicle.driver_user_id)
            .outerjoin(
                schema.DriverProfile,
                schema.DriverProfile.user_id == schema.User.id,
            )
            .where(*filters)
        )

        list_stmt = (
            select(
                schema.Vehicle.id.label("vehicle_id"),
                schema.Vehicle.driver_user_id.label("driver_user_id"),
                schema.DriverProfile.full_name.label("driver_name"),
                schema.Vehicle.registration_number.label("vehicle_license_plate"),
            )
            .select_from(schema.Vehicle)
            .join(schema.User, schema.User.id == schema.Vehicle.driver_user_id)
            .outerjoin(
                schema.DriverProfile,
                schema.DriverProfile.user_id == schema.User.id,
            )
            .where(*filters)
            .order_by(
                schema.DriverProfile.full_name.asc().nulls_last(),
                schema.Vehicle.registration_number.asc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        count_result = await self.db.execute(count_stmt)
        list_result = await self.db.execute(list_stmt)

        rows = list_result.mappings().all()

        return [dict(row) for row in rows], int(count_result.scalar_one() or 0)
    
    async def list_card_options(
        self,
        *,
        page: int,
        page_size: int,
        q: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        cleaned_q = q.strip() if q is not None else None

        filters = []

        if cleaned_q:
            search = f"%{cleaned_q}%"
            filters.append(
                or_(
                    schema.RFIDCard.card_uid_masked.ilike(search),
                    schema.RFIDCard.assigned_passenger_user_id.ilike(search),
                    schema.PassengerProfile.full_name.ilike(search),
                )
            )

        count_stmt = (
            select(func.count(schema.RFIDCard.id))
            .select_from(schema.RFIDCard)
            .outerjoin(
                schema.PassengerProfile,
                schema.PassengerProfile.user_id
                == schema.RFIDCard.assigned_passenger_user_id,
            )
        )

        list_stmt = (
            select(
                schema.RFIDCard.id.label("card_id"),
                schema.RFIDCard.card_uid_masked.label("card_uid_masked"),
                schema.RFIDCard.assigned_passenger_user_id.label(
                    "assigned_passenger_user_id"
                ),
                schema.PassengerProfile.full_name.label(
                    "assigned_passenger_name"
                ),
            )
            .select_from(schema.RFIDCard)
            .outerjoin(
                schema.PassengerProfile,
                schema.PassengerProfile.user_id
                == schema.RFIDCard.assigned_passenger_user_id,
            )
            .order_by(
                schema.PassengerProfile.full_name.asc().nulls_last(),
                schema.RFIDCard.card_uid_masked.asc().nulls_last(),
                schema.RFIDCard.created_at.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        if filters:
            count_stmt = count_stmt.where(*filters)
            list_stmt = list_stmt.where(*filters)

        count_result = await self.db.execute(count_stmt)
        list_result = await self.db.execute(list_stmt)

        rows = list_result.mappings().all()

        return [dict(row) for row in rows], int(count_result.scalar_one() or 0)
    
    # ============================================================
    # RFID card admin operations
    # ============================================================

    async def register_card(
        self,
        payload: RFIDCardRegisterRequest,
    ) -> schema.RFIDCard:
        card_uid_hash = self.hash_card_uid(payload.card_uid)
        card_uid_masked = self.mask_card_uid(payload.card_uid)

        existing_stmt = (
            select(schema.RFIDCard)
            .where(schema.RFIDCard.card_uid_hash == card_uid_hash)
            .limit(1)
        )
        existing_result = await self.db.execute(existing_stmt)
        existing_card = existing_result.scalar_one_or_none()

        if existing_card is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_already_exists",
                    "message": "An RFID card with this UID already exists.",
                },
            )

        card = schema.RFIDCard(
            id=schema.new_id(),
            card_uid_hash=card_uid_hash,
            card_uid_masked=card_uid_masked,
            inventory_status=schema.RFIDCardInventoryStatus.INVENTORY,
            authorization_status=schema.RFIDCardAuthorizationStatus.ALLOWED,
            notes=payload.notes,
        )

        account = schema.RFIDCardAccount(
            id=schema.new_id(),
            card_id=card.id,
            current_balance=Decimal("0.00"),
            held_balance=Decimal("0.00"),
            currency="INR",
            is_active=True,
        )

        self.db.add(card)
        self.db.add(account)

        try:
            await self.db.flush()
        except IntegrityError as exc:
            await self.db.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_conflict",
                    "message": "An RFID card with this UID already exists.",
                },
            ) from exc

        return card

    async def bulk_register_cards(
        self,
        payload: RFIDCardBulkRegisterRequest,
    ) -> tuple[list[dict[str, Any]], int, int]:
        uid_rows: list[tuple[str, str, str]] = []

        for card_uid in payload.card_uids:
            uid_rows.append(
                (
                    card_uid,
                    self.hash_card_uid(card_uid),
                    self.mask_card_uid(card_uid),
                )
            )

        hashes = [item[1] for item in uid_rows]

        existing_stmt = select(schema.RFIDCard.card_uid_hash).where(
            schema.RFIDCard.card_uid_hash.in_(hashes)
        )
        existing_result = await self.db.execute(existing_stmt)
        existing_hashes = set(existing_result.scalars().all())

        created_cards_by_hash: dict[str, schema.RFIDCard] = {}
        skipped_items: list[dict[str, Any]] = []

        for card_uid, card_uid_hash, card_uid_masked in uid_rows:
            if card_uid_hash in existing_hashes:
                skipped_items.append(
                    {
                        "card_uid_masked": card_uid_masked,
                        "status": "skipped",
                        "card": None,
                        "error": "RFID card already exists.",
                    }
                )
                continue

            card = schema.RFIDCard(
                id=schema.new_id(),
                card_uid_hash=card_uid_hash,
                card_uid_masked=card_uid_masked,
                inventory_status=schema.RFIDCardInventoryStatus.INVENTORY,
                authorization_status=schema.RFIDCardAuthorizationStatus.ALLOWED,
                notes=payload.notes,
            )

            account = schema.RFIDCardAccount(
                id=schema.new_id(),
                card_id=card.id,
                current_balance=Decimal("0.00"),
                held_balance=Decimal("0.00"),
                currency="INR",
                is_active=True,
            )

            self.db.add(card)
            self.db.add(account)
            created_cards_by_hash[card_uid_hash] = card

        try:
            await self.db.flush()
        except IntegrityError as exc:
            await self.db.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_bulk_conflict",
                    "message": "One or more RFID cards already exist.",
                },
            ) from exc

        items: list[dict[str, Any]] = []
        created_count = 0
        skipped_count = 0

        for _card_uid, card_uid_hash, card_uid_masked in uid_rows:
            card = created_cards_by_hash.get(card_uid_hash)

            if card is None:
                skipped_count += 1
                items.append(
                    {
                        "card_uid_masked": card_uid_masked,
                        "status": "skipped",
                        "card": None,
                        "error": "RFID card already exists.",
                    }
                )
                continue

            created_count += 1
            items.append(
                {
                    "card_uid_masked": card_uid_masked,
                    "status": "created",
                    "card": self.serialize_card(card),
                    "error": None,
                }
            )

        return items, created_count, skipped_count

    async def list_cards(
        self,
        *,
        page: int,
        page_size: int,
        inventory_status: schema.RFIDCardInventoryStatus | None = None,
        authorization_status: schema.RFIDCardAuthorizationStatus | None = None,
        assigned_passenger_user_id: str | None = None,
    ) -> tuple[list[schema.RFIDCard], int]:
        filters = []

        if inventory_status is not None:
            filters.append(schema.RFIDCard.inventory_status == inventory_status)

        if authorization_status is not None:
            filters.append(schema.RFIDCard.authorization_status == authorization_status)

        if assigned_passenger_user_id is not None:
            filters.append(
                schema.RFIDCard.assigned_passenger_user_id
                == assigned_passenger_user_id
            )

        count_stmt = select(func.count(schema.RFIDCard.id))
        list_stmt = select(schema.RFIDCard)

        if filters:
            count_stmt = count_stmt.where(*filters)
            list_stmt = list_stmt.where(*filters)

        list_stmt = (
            list_stmt
            .order_by(schema.RFIDCard.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        count_result = await self.db.execute(count_stmt)
        list_result = await self.db.execute(list_stmt)

        return list(list_result.scalars().all()), int(count_result.scalar_one() or 0)

    async def _get_card_or_404(self, card_id: str) -> schema.RFIDCard:
        stmt = select(schema.RFIDCard).where(schema.RFIDCard.id == card_id)
        result = await self.db.execute(stmt)
        card = result.scalar_one_or_none()

        if card is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "rfid_card_not_found",
                    "message": "RFID card not found.",
                },
            )

        return card

    async def _get_card_account_or_404(
        self,
        card_id: str,
    ) -> schema.RFIDCardAccount:
        stmt = select(schema.RFIDCardAccount).where(
            schema.RFIDCardAccount.card_id == card_id
        )
        result = await self.db.execute(stmt)
        account = result.scalar_one_or_none()

        if account is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "rfid_card_account_not_found",
                    "message": "RFID card account not found.",
                },
            )

        return account

    async def _get_passenger_or_404(self, passenger_user_id: str) -> schema.User:
        stmt = select(schema.User).where(
            schema.User.id == passenger_user_id,
            schema.User.role == schema.UserRole.PASSENGER,
        )
        result = await self.db.execute(stmt)
        passenger = result.scalar_one_or_none()

        if passenger is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "passenger_not_found",
                    "message": "Passenger user not found.",
                },
            )

        return passenger

    async def get_current_card_assignment(
        self,
        card_id: str,
    ) -> schema.RFIDCardAssignment | None:
        stmt = (
            select(schema.RFIDCardAssignment)
            .where(
                schema.RFIDCardAssignment.card_id == card_id,
                schema.RFIDCardAssignment.unassigned_at.is_(None),
            )
            .order_by(schema.RFIDCardAssignment.assigned_at.desc())
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_card_detail(self, card_id: str) -> dict[str, Any]:
        card = await self._get_card_or_404(card_id)
        account = await self._get_card_account_or_404(card_id)
        current_assignment = await self.get_current_card_assignment(card_id)

        return {
            "card": self.serialize_card(card),
            "account": self.serialize_account(account),
            "current_assignment": None
            if current_assignment is None
            else self.serialize_assignment(current_assignment),
        }

    async def assign_card(
        self,
        *,
        card_id: str,
        payload: RFIDCardAssignRequest,
        admin_user_id: str,
    ) -> schema.RFIDCard:
        card = await self._get_card_or_404(card_id)
        await self._get_card_account_or_404(card_id)
        await self._get_passenger_or_404(payload.passenger_user_id)

        if card.inventory_status == schema.RFIDCardInventoryStatus.DECOMMISSIONED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_decommissioned",
                    "message": "A decommissioned RFID card cannot be assigned.",
                },
            )

        if card.inventory_status == schema.RFIDCardInventoryStatus.LOST:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_lost",
                    "message": "A lost RFID card cannot be assigned.",
                },
            )

        if card.authorization_status != schema.RFIDCardAuthorizationStatus.ALLOWED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_blocked",
                    "message": "A blocked RFID card cannot be assigned.",
                },
            )

        current_assignment = await self.get_current_card_assignment(card.id)

        if current_assignment is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_already_assigned",
                    "message": "RFID card is already assigned. Unassign it before assigning again.",
                },
            )

        now = schema.utcnow()

        assignment = schema.RFIDCardAssignment(
            card_id=card.id,
            passenger_user_id=payload.passenger_user_id,
            assigned_by_admin_id=admin_user_id,
            assigned_at=now,
            reason=payload.reason,
        )

        card.assigned_passenger_user_id = payload.passenger_user_id
        card.assigned_at = now
        card.inventory_status = schema.RFIDCardInventoryStatus.ASSIGNED

        self.db.add(assignment)
        self.db.add(card)
        await self.db.flush()

        return card

    async def unassign_card(
        self,
        *,
        card_id: str,
        payload: RFIDCardUnassignRequest,
        admin_user_id: str,
    ) -> schema.RFIDCard:
        card = await self._get_card_or_404(card_id)
        current_assignment = await self.get_current_card_assignment(card.id)

        if current_assignment is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_not_assigned",
                    "message": "RFID card is not currently assigned.",
                },
            )

        now = schema.utcnow()

        current_assignment.unassigned_by_admin_id = admin_user_id
        current_assignment.unassigned_at = now

        if payload.reason is not None:
            current_assignment.reason = payload.reason

        card.assigned_passenger_user_id = None
        card.assigned_at = None
        card.inventory_status = schema.RFIDCardInventoryStatus.INVENTORY

        self.db.add(current_assignment)
        self.db.add(card)
        await self.db.flush()

        return card
    
    @staticmethod
    def _append_card_admin_note(
        *,
        existing_note: str | None,
        action: str,
        admin_user_id: str,
        reason: str | None,
    ) -> str:
        now = schema.utcnow().isoformat()

        line = f"[{now}] {action} by admin {admin_user_id}"

        if reason is not None:
            line = f"{line}: {reason}"

        if existing_note is None or not existing_note.strip():
            return line

        return f"{existing_note.rstrip()}\n{line}"

    async def block_card(
        self,
        *,
        card_id: str,
        payload: RFIDCardBlockRequest,
        admin_user_id: str,
    ) -> schema.RFIDCard:
        card = await self._get_card_or_404(card_id)

        if card.inventory_status == schema.RFIDCardInventoryStatus.DECOMMISSIONED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_decommissioned",
                    "message": "A decommissioned RFID card cannot be blocked.",
                },
            )

        card.authorization_status = schema.RFIDCardAuthorizationStatus.BLOCKED
        card.notes = self._append_card_admin_note(
            existing_note=card.notes,
            action="RFID card blocked",
            admin_user_id=admin_user_id,
            reason=payload.reason,
        )

        self.db.add(card)
        await self.db.flush()

        return card

    async def unblock_card(
        self,
        *,
        card_id: str,
        payload: RFIDCardBlockRequest,
        admin_user_id: str,
    ) -> schema.RFIDCard:
        card = await self._get_card_or_404(card_id)

        if card.inventory_status == schema.RFIDCardInventoryStatus.DECOMMISSIONED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_decommissioned",
                    "message": "A decommissioned RFID card cannot be unblocked.",
                },
            )

        card.authorization_status = schema.RFIDCardAuthorizationStatus.ALLOWED
        card.notes = self._append_card_admin_note(
            existing_note=card.notes,
            action="RFID card unblocked",
            admin_user_id=admin_user_id,
            reason=payload.reason,
        )

        self.db.add(card)
        await self.db.flush()

        return card
    
    async def _ensure_card_has_no_open_rfid_ride(self, card_id: str) -> None:
        stmt = (
            select(func.count(schema.RFIDTripRide.id))
            .where(
                schema.RFIDTripRide.card_id == card_id,
                schema.RFIDTripRide.status == schema.RFIDRideStatus.BOARDED,
            )
        )
        result = await self.db.execute(stmt)
        open_ride_count = int(result.scalar_one() or 0)

        if open_ride_count > 0:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_has_open_ride",
                    "message": "RFID card has an open boarded ride. Complete or cancel the ride before returning/decommissioning the card.",
                },
            )

    def _ensure_account_has_no_held_balance(
        self,
        account: schema.RFIDCardAccount,
    ) -> None:
        held_balance = self._normalize_money(Decimal(account.held_balance or 0))

        if held_balance > Decimal("0.00"):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_has_held_balance",
                    "message": "RFID card has held balance. Release or settle holds before returning/decommissioning the card.",
                },
            )

    async def _sweep_card_available_balance(
        self,
        *,
        card: schema.RFIDCard,
        account: schema.RFIDCardAccount,
        entry_type: schema.RFIDLedgerEntryType,
        admin_user_id: str,
        note: str,
    ) -> schema.RFIDLedgerEntry | None:
        current_balance = self._normalize_money(
            Decimal(account.current_balance or 0)
        )
        held_balance = self._normalize_money(
            Decimal(account.held_balance or 0)
        )
        available_balance = self._normalize_money(current_balance - held_balance)

        if available_balance < Decimal("0.00"):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_account_balance_invalid",
                    "message": "RFID account balance is invalid: held balance is greater than current balance.",
                },
            )

        if available_balance <= Decimal("0.00"):
            return None

        balance_after = self._normalize_money(current_balance - available_balance)
        held_balance_after = held_balance

        ledger_entry = schema.RFIDLedgerEntry(
            id=schema.new_id(),
            account_id=account.id,
            card_id=card.id,
            passenger_user_id=card.assigned_passenger_user_id,
            entry_type=entry_type,
            amount_delta=-available_balance,
            held_delta=Decimal("0.00"),
            balance_after=balance_after,
            held_balance_after=held_balance_after,
            created_by_admin_id=admin_user_id,
            note=note,
            created_at=schema.utcnow(),
        )

        account.current_balance = balance_after
        account.held_balance = held_balance_after

        self.db.add(ledger_entry)
        self.db.add(account)

        return ledger_entry

    async def return_card(
        self,
        *,
        card_id: str,
        payload: RFIDCardReturnRequest,
        admin_user_id: str,
    ) -> schema.RFIDCard:
        card = await self._get_card_or_404(card_id)
        account = await self._get_card_account_for_update_or_404(card_id)

        if card.inventory_status == schema.RFIDCardInventoryStatus.DECOMMISSIONED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_decommissioned",
                    "message": "A decommissioned RFID card cannot be returned.",
                },
            )

        await self._ensure_card_has_no_open_rfid_ride(card.id)
        self._ensure_account_has_no_held_balance(account)

        now = schema.utcnow()
        current_assignment = await self.get_current_card_assignment(card.id)

        if current_assignment is not None:
            current_assignment.unassigned_by_admin_id = admin_user_id
            current_assignment.unassigned_at = now
            current_assignment.reason = payload.reason or "RFID card returned."
            self.db.add(current_assignment)

        if payload.sweep_remaining_balance:
            await self._sweep_card_available_balance(
                card=card,
                account=account,
                entry_type=schema.RFIDLedgerEntryType.CARD_RETURN_SWEEP,
                admin_user_id=admin_user_id,
                note=payload.reason
                or "RFID card returned; remaining available balance swept.",
            )

        card.assigned_passenger_user_id = None
        card.assigned_at = None
        card.returned_at = now
        card.inventory_status = schema.RFIDCardInventoryStatus.INVENTORY
        card.authorization_status = schema.RFIDCardAuthorizationStatus.ALLOWED
        card.notes = self._append_card_admin_note(
            existing_note=card.notes,
            action="RFID card returned",
            admin_user_id=admin_user_id,
            reason=payload.reason,
        )

        account.is_active = True

        self.db.add(card)
        self.db.add(account)
        await self.db.flush()

        return card

    async def decommission_card(
        self,
        *,
        card_id: str,
        payload: RFIDCardDecommissionRequest,
        admin_user_id: str,
    ) -> schema.RFIDCard:
        card = await self._get_card_or_404(card_id)
        account = await self._get_card_account_for_update_or_404(card_id)

        if card.inventory_status == schema.RFIDCardInventoryStatus.DECOMMISSIONED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_already_decommissioned",
                    "message": "RFID card is already decommissioned.",
                },
            )

        await self._ensure_card_has_no_open_rfid_ride(card.id)
        self._ensure_account_has_no_held_balance(account)

        now = schema.utcnow()
        current_assignment = await self.get_current_card_assignment(card.id)

        if current_assignment is not None:
            current_assignment.unassigned_by_admin_id = admin_user_id
            current_assignment.unassigned_at = now
            current_assignment.reason = payload.reason or "RFID card decommissioned."
            self.db.add(current_assignment)

        if payload.sweep_remaining_balance:
            await self._sweep_card_available_balance(
                card=card,
                account=account,
                entry_type=schema.RFIDLedgerEntryType.CARD_DECOMMISSION_SWEEP,
                admin_user_id=admin_user_id,
                note=payload.reason
                or "RFID card decommissioned; remaining available balance swept.",
            )

        card.assigned_passenger_user_id = None
        card.assigned_at = None
        card.decommissioned_at = now
        card.inventory_status = schema.RFIDCardInventoryStatus.DECOMMISSIONED
        card.authorization_status = schema.RFIDCardAuthorizationStatus.BLOCKED
        card.notes = self._append_card_admin_note(
            existing_note=card.notes,
            action="RFID card decommissioned",
            admin_user_id=admin_user_id,
            reason=payload.reason,
        )

        account.is_active = False

        self.db.add(card)
        self.db.add(account)
        await self.db.flush()

        return card
    
    @staticmethod
    def _normalize_money(value: Decimal) -> Decimal:
        return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    async def _get_card_account_for_update_or_404(
        self,
        card_id: str,
    ) -> schema.RFIDCardAccount:
        stmt = (
            select(schema.RFIDCardAccount)
            .where(schema.RFIDCardAccount.card_id == card_id)
            .with_for_update()
        )
        result = await self.db.execute(stmt)
        account = result.scalar_one_or_none()

        if account is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "rfid_card_account_not_found",
                    "message": "RFID card account not found.",
                },
            )

        return account

    async def create_manual_recharge(
        self,
        *,
        payload: RFIDRechargeCreateRequest,
        admin_user_id: str,
    ) -> tuple[schema.RFIDRecharge, schema.RFIDCardAccount]:
        card = await self._get_card_or_404(payload.card_id)
        account = await self._get_card_account_for_update_or_404(payload.card_id)

        if card.inventory_status == schema.RFIDCardInventoryStatus.DECOMMISSIONED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_decommissioned",
                    "message": "A decommissioned RFID card cannot be recharged.",
                },
            )

        if card.inventory_status == schema.RFIDCardInventoryStatus.LOST:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_lost",
                    "message": "A lost RFID card cannot be recharged.",
                },
            )

        if account.is_active is False:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_account_inactive",
                    "message": "RFID card account is inactive.",
                },
            )

        amount = self._normalize_money(payload.amount)

        if amount <= Decimal("0.00"):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_recharge_amount",
                    "message": "Recharge amount must be at least 0.01 after rounding.",
                },
            )

        now = schema.utcnow()
        balance_after = self._normalize_money(
            Decimal(account.current_balance or 0) + amount
        )
        held_balance_after = self._normalize_money(
            Decimal(account.held_balance or 0)
        )

        recharge = schema.RFIDRecharge(
            id=schema.new_id(),
            account_id=account.id,
            card_id=card.id,
            passenger_user_id=card.assigned_passenger_user_id,
            amount=amount,
            status=schema.RFIDRechargeStatus.CREDITED,
            source_type=schema.RFIDRechargeSourceType.ADMIN_MANUAL,
            razorpay_order_id=payload.razorpay_order_id,
            razorpay_payment_id=payload.razorpay_payment_id,
            razorpay_status="admin_recorded",
            razorpay_amount=amount,
            created_by_admin_id=admin_user_id,
            verified_by_admin_id=admin_user_id,
            paid_at=now,
            credited_at=now,
        )

        funding_lot = schema.RFIDFundingLot(
            id=schema.new_id(),
            recharge_id=recharge.id,
            account_id=account.id,
            card_id=card.id,
            source_amount=amount,
            remaining_amount=amount,
            razorpay_payment_id=payload.razorpay_payment_id,
            source_type=(
                schema.RFIDFundingLotSourceType.RAZORPAY_PAYMENT
                if payload.razorpay_order_id or payload.razorpay_payment_id
                else schema.RFIDFundingLotSourceType.ADMIN_MANUAL_POOL
            ),
            status=schema.RFIDFundingLotStatus.AVAILABLE,
        )

        ledger_entry = schema.RFIDLedgerEntry(
            id=schema.new_id(),
            account_id=account.id,
            card_id=card.id,
            passenger_user_id=card.assigned_passenger_user_id,
            entry_type=schema.RFIDLedgerEntryType.RECHARGE_CREDIT,
            amount_delta=amount,
            held_delta=Decimal("0.00"),
            balance_after=balance_after,
            held_balance_after=held_balance_after,
            source_recharge_id=recharge.id,
            razorpay_order_id=payload.razorpay_order_id,
            razorpay_payment_id=payload.razorpay_payment_id,
            created_by_admin_id=admin_user_id,
            note=payload.note,
            created_at=now,
        )

        account.current_balance = balance_after
        account.held_balance = held_balance_after

        try:
            self.db.add(recharge)
            await self.db.flush()

            self.db.add(funding_lot)
            self.db.add(ledger_entry)
            self.db.add(account)
            await self.db.flush()

            recharge.credited_ledger_entry_id = ledger_entry.id
            self.db.add(recharge)
            await self.db.flush()
        except IntegrityError as exc:
            await self.db.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_recharge_conflict",
                    "message": "RFID recharge could not be recorded.",
                },
            ) from exc

        return recharge, account
    
    async def list_card_ledger_entries(
        self,
        *,
        card_id: str,
        page: int,
        page_size: int,
    ) -> tuple[list[schema.RFIDLedgerEntry], int]:
        await self._get_card_or_404(card_id)

        filters = [schema.RFIDLedgerEntry.card_id == card_id]

        count_stmt = select(func.count(schema.RFIDLedgerEntry.id)).where(*filters)

        list_stmt = (
            select(schema.RFIDLedgerEntry)
            .where(*filters)
            .order_by(schema.RFIDLedgerEntry.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        count_result = await self.db.execute(count_stmt)
        list_result = await self.db.execute(list_stmt)

        return list(list_result.scalars().all()), int(count_result.scalar_one() or 0)

    async def list_card_recharges(
        self,
        *,
        card_id: str,
        page: int,
        page_size: int,
    ) -> tuple[list[schema.RFIDRecharge], int]:
        await self._get_card_or_404(card_id)

        filters = [schema.RFIDRecharge.card_id == card_id]

        count_stmt = select(func.count(schema.RFIDRecharge.id)).where(*filters)

        list_stmt = (
            select(schema.RFIDRecharge)
            .where(*filters)
            .order_by(schema.RFIDRecharge.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        count_result = await self.db.execute(count_stmt)
        list_result = await self.db.execute(list_stmt)

        return list(list_result.scalars().all()), int(count_result.scalar_one() or 0)
    

    # ============================================================
    # serializers
    # ============================================================

    def serialize_device(self, device: schema.RFIDDevice) -> dict[str, Any]:
        return {
            "id": device.id,
            "serial_number": device.serial_number,
            "vehicle_id": device.vehicle_id,
            "is_active": device.is_active,
            "decommissioned_at": device.decommissioned_at,
            "last_seen_at": device.last_seen_at,
            "last_seen_lat": device.last_seen_lat,
            "last_seen_lng": device.last_seen_lng,
            "notes": device.notes,
            "created_at": device.created_at,
            "updated_at": device.updated_at,
        }

    def serialize_card(self, card: schema.RFIDCard) -> dict[str, Any]:
        return {
            "id": card.id,
            "card_uid_masked": card.card_uid_masked,
            "inventory_status": card.inventory_status,
            "authorization_status": card.authorization_status,
            "assigned_passenger_user_id": card.assigned_passenger_user_id,
            "assigned_at": card.assigned_at,
            "returned_at": card.returned_at,
            "decommissioned_at": card.decommissioned_at,
            "notes": card.notes,
            "created_at": card.created_at,
            "updated_at": card.updated_at,
        }

    def serialize_account(self, account: schema.RFIDCardAccount) -> dict[str, Any]:
        return {
            "id": account.id,
            "card_id": account.card_id,
            "current_balance": account.current_balance,
            "held_balance": account.held_balance,
            "available_balance": self._available_balance(account),
            "currency": account.currency,
            "is_active": account.is_active,
            "created_at": account.created_at,
            "updated_at": account.updated_at,
        }

    def serialize_assignment(
        self,
        assignment: schema.RFIDCardAssignment,
    ) -> dict[str, Any]:
        return {
            "id": assignment.id,
            "card_id": assignment.card_id,
            "passenger_user_id": assignment.passenger_user_id,
            "assigned_by_admin_id": assignment.assigned_by_admin_id,
            "assigned_at": assignment.assigned_at,
            "unassigned_by_admin_id": assignment.unassigned_by_admin_id,
            "unassigned_at": assignment.unassigned_at,
            "reason": assignment.reason,
            "created_at": assignment.created_at,
            "updated_at": assignment.updated_at,
        }

    def serialize_recharge(self, recharge: schema.RFIDRecharge) -> dict[str, Any]:
        return {
            "id": recharge.id,
            "account_id": recharge.account_id,
            "card_id": recharge.card_id,
            "passenger_user_id": recharge.passenger_user_id,
            "amount": recharge.amount,
            "status": recharge.status,
            "source_type": recharge.source_type,
            "razorpay_order_id": recharge.razorpay_order_id,
            "razorpay_payment_id": recharge.razorpay_payment_id,
            "razorpay_status": recharge.razorpay_status,
            "razorpay_amount": recharge.razorpay_amount,
            "created_by_admin_id": recharge.created_by_admin_id,
            "verified_by_admin_id": recharge.verified_by_admin_id,
            "credited_ledger_entry_id": recharge.credited_ledger_entry_id,
            "paid_at": recharge.paid_at,
            "credited_at": recharge.credited_at,
            "created_at": recharge.created_at,
            "updated_at": recharge.updated_at,
        }

    def serialize_ledger_entry(
        self,
        entry: schema.RFIDLedgerEntry,
    ) -> dict[str, Any]:
        return {
            "id": entry.id,
            "account_id": entry.account_id,
            "card_id": entry.card_id,
            "passenger_user_id": entry.passenger_user_id,
            "entry_type": entry.entry_type,
            "amount_delta": entry.amount_delta,
            "held_delta": entry.held_delta,
            "balance_after": entry.balance_after,
            "held_balance_after": entry.held_balance_after,
            "source_recharge_id": entry.source_recharge_id,
            "scheduled_trip_id": entry.scheduled_trip_id,
            "rfid_ride_id": entry.rfid_ride_id,
            "stop_id": entry.stop_id,
            "razorpay_order_id": entry.razorpay_order_id,
            "razorpay_payment_id": entry.razorpay_payment_id,
            "reverses_ledger_entry_id": entry.reverses_ledger_entry_id,
            "reversed_by_ledger_entry_id": entry.reversed_by_ledger_entry_id,
            "created_by_admin_id": entry.created_by_admin_id,
            "note": entry.note,
            "created_at": entry.created_at,
        }

    def serialize_scan_event(
        self,
        event: schema.RFIDScanEvent,
    ) -> dict[str, Any]:
        return {
            "id": event.id,
            "scan_type": event.scan_type,
            "device_id": event.device_id,
            "device_serial_snapshot": event.device_serial_snapshot,
            "card_id": event.card_id,
            "passenger_user_id": event.passenger_user_id,
            "rfid_ride_id": event.rfid_ride_id,
            "scheduled_trip_id": event.scheduled_trip_id,
            "route_id": event.route_id,
            "vehicle_id": event.vehicle_id,
            "driver_user_id": event.driver_user_id,
            "matched_stop_id": event.matched_stop_id,
            "matched_route_stop_id": event.matched_route_stop_id,
            "matched_sequence_no": event.matched_sequence_no,
            "active_trip_event_id": event.active_trip_event_id,
            "active_stop_arrival_time_snapshot": event.active_stop_arrival_time_snapshot,
            "active_stop_departure_time_snapshot": event.active_stop_departure_time_snapshot,
            "scan_lat": event.scan_lat,
            "scan_lng": event.scan_lng,
            "within_radius": event.within_radius,
            "distance_from_stop_meters": event.distance_from_stop_meters,
            "accepted": event.accepted,
            "rejection_reason": event.rejection_reason,
            "created_at": event.created_at,
        }

    def serialize_ride(self, ride: schema.RFIDTripRide) -> dict[str, Any]:
        return {
            "id": ride.id,
            "card_id": ride.card_id,
            "account_id": ride.account_id,
            "passenger_user_id": ride.passenger_user_id,
            "scheduled_trip_id": ride.scheduled_trip_id,
            "route_id": ride.route_id,
            "vehicle_id": ride.vehicle_id,
            "driver_user_id": ride.driver_user_id,
            "pickup_stop_id": ride.pickup_stop_id,
            "pickup_sequence_no": ride.pickup_sequence_no,
            "board_rfid_scan_event_id": ride.board_rfid_scan_event_id,
            "boarded_at": ride.boarded_at,
            "board_lat": ride.board_lat,
            "board_lng": ride.board_lng,
            "dropoff_stop_id": ride.dropoff_stop_id,
            "dropoff_sequence_no": ride.dropoff_sequence_no,
            "drop_rfid_scan_event_id": ride.drop_rfid_scan_event_id,
            "dropped_at": ride.dropped_at,
            "drop_lat": ride.drop_lat,
            "drop_lng": ride.drop_lng,
            "status": ride.status,
            "hold_amount": ride.hold_amount,
            "fare_amount": ride.fare_amount,
            "fare_reversed_amount": ride.fare_reversed_amount,
            "fare_net_amount": self._normalize_money(
                Decimal(ride.fare_amount or 0)
                - Decimal(ride.fare_reversed_amount or 0)
            ),
            "commission_percent_snapshot": ride.commission_percent_snapshot,
            "commission_amount": ride.commission_amount,
            "driver_payout_amount": ride.driver_payout_amount,
            "driver_payout_reversed_amount": ride.driver_payout_reversed_amount,
            "driver_payout_net_amount": self._normalize_money(
                Decimal(ride.driver_payout_amount or 0)
                - Decimal(ride.driver_payout_reversed_amount or 0)
            ),
            "platform_amount": ride.platform_amount,
            "platform_amount_reversed": ride.platform_amount_reversed,
            "platform_net_amount": self._normalize_money(
                Decimal(ride.platform_amount or 0)
                - Decimal(ride.platform_amount_reversed or 0)
            ),
            "transfer_status": ride.transfer_status,
            "transfer_ready_at": ride.transfer_ready_at,
            "transfer_processed_at": ride.transfer_processed_at,
            "created_at": ride.created_at,
            "updated_at": ride.updated_at,
        }
    
    async def list_rfid_payout_transfers(
        self,
        *,
        page: int,
        page_size: int,
        status: schema.RFIDPayoutTransferStatus | None = None,
        driver_user_id: str | None = None,
        scheduled_trip_id: str | None = None,
        rfid_ride_id: str | None = None,
    ) -> tuple[list[schema.RFIDPayoutTransfer], int]:
        filters = []

        if status is not None:
            filters.append(schema.RFIDPayoutTransfer.status == status)

        if driver_user_id is not None:
            filters.append(schema.RFIDPayoutTransfer.driver_user_id == driver_user_id)

        if scheduled_trip_id is not None:
            filters.append(
                schema.RFIDPayoutTransfer.scheduled_trip_id == scheduled_trip_id
            )

        if rfid_ride_id is not None:
            filters.append(schema.RFIDPayoutTransfer.rfid_ride_id == rfid_ride_id)

        count_stmt = select(func.count(schema.RFIDPayoutTransfer.id))
        list_stmt = select(schema.RFIDPayoutTransfer)

        if filters:
            count_stmt = count_stmt.where(*filters)
            list_stmt = list_stmt.where(*filters)

        list_stmt = (
            list_stmt
            .order_by(schema.RFIDPayoutTransfer.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        count_result = await self.db.execute(count_stmt)
        list_result = await self.db.execute(list_stmt)

        transfers = list(list_result.scalars().all())
        await self._attach_payout_transfer_reversal_summary(transfers)
        return (
            transfers,
            int(count_result.scalar_one() or 0),
        )
    
    @staticmethod
    def serialize_trip_ride(ride: schema.RFIDTripRide) -> dict[str, Any]:
        return {
            "id": ride.id,
            "card_id": ride.card_id,
            "account_id": ride.account_id,
            "passenger_user_id": ride.passenger_user_id,
            "scheduled_trip_id": ride.scheduled_trip_id,
            "route_id": ride.route_id,
            "vehicle_id": ride.vehicle_id,
            "driver_user_id": ride.driver_user_id,
            "pickup_stop_id": ride.pickup_stop_id,
            "pickup_sequence_no": ride.pickup_sequence_no,
            "board_rfid_scan_event_id": ride.board_rfid_scan_event_id,
            "boarded_at": ride.boarded_at,
            "board_lat": ride.board_lat,
            "board_lng": ride.board_lng,
            "dropoff_stop_id": ride.dropoff_stop_id,
            "dropoff_sequence_no": ride.dropoff_sequence_no,
            "drop_rfid_scan_event_id": ride.drop_rfid_scan_event_id,
            "dropped_at": ride.dropped_at,
            "drop_lat": ride.drop_lat,
            "drop_lng": ride.drop_lng,
            "status": ride.status,
            "hold_amount": ride.hold_amount,
            "fare_amount": ride.fare_amount,
            "fare_reversed_amount": ride.fare_reversed_amount,
            "fare_net_amount": AdminRFIDService._normalize_money(
                Decimal(ride.fare_amount or 0)
                - Decimal(ride.fare_reversed_amount or 0)
            ),
            "commission_percent_snapshot": ride.commission_percent_snapshot,
            "commission_amount": ride.commission_amount,
            "driver_payout_amount": ride.driver_payout_amount,
            "driver_payout_reversed_amount": ride.driver_payout_reversed_amount,
            "driver_payout_net_amount": AdminRFIDService._normalize_money(
                Decimal(ride.driver_payout_amount or 0)
                - Decimal(ride.driver_payout_reversed_amount or 0)
            ),
            "platform_amount": ride.platform_amount,
            "platform_amount_reversed": ride.platform_amount_reversed,
            "platform_net_amount": AdminRFIDService._normalize_money(
                Decimal(ride.platform_amount or 0)
                - Decimal(ride.platform_amount_reversed or 0)
            ),
            "transfer_status": ride.transfer_status,
            "transfer_ready_at": ride.transfer_ready_at,
            "transfer_processed_at": ride.transfer_processed_at,
            "created_at": ride.created_at,
            "updated_at": ride.updated_at,
        }
    
    @staticmethod
    def serialize_payout_transfer(
        transfer: schema.RFIDPayoutTransfer,
    ) -> dict[str, Any]:
        driver_payout_amount = AdminRFIDService._normalize_money(
            Decimal(transfer.amount or 0)
        )
        driver_payout_reversed_amount = AdminRFIDService._normalize_money(
            Decimal(transfer.reversed_amount or 0)
        )
        driver_payout_payable_amount = AdminRFIDService._normalize_money(
            driver_payout_amount - driver_payout_reversed_amount
        )
        provider_reversed_amount = AdminRFIDService._normalize_money(
            Decimal(
                getattr(
                    transfer,
                    "_rfid_provider_reversed_amount",
                    Decimal("0.00"),
                )
                or 0
            )
        )
        reversal_count = int(
            getattr(transfer, "_rfid_reversal_count", 0) or 0
        )

        return {
            "id": transfer.id,
            "rfid_ride_id": transfer.rfid_ride_id,
            "driver_user_id": transfer.driver_user_id,
            "scheduled_trip_id": transfer.scheduled_trip_id,
            "route_id": transfer.route_id,
            "vehicle_id": transfer.vehicle_id,
            "source_recharge_id": transfer.source_recharge_id,
            "source_funding_allocation_id": transfer.source_funding_allocation_id,
            "source_razorpay_payment_id": transfer.source_razorpay_payment_id,
            "linked_account_id": transfer.linked_account_id,

            # Backward-compatible field. This is the driver payout transfer amount,
            # not the passenger-facing RFID fare.
            "amount": driver_payout_amount,

            # Explicit money meaning after RFID commission split.
            "driver_payout_amount": driver_payout_amount,
            "driver_payout_reversed_amount": driver_payout_reversed_amount,
            "driver_payout_payable_amount": driver_payout_payable_amount,

            # Backward-compatible aliases.
            "reversed_amount": driver_payout_reversed_amount,
            "payable_amount": driver_payout_payable_amount,

            "provider_reversed_amount": provider_reversed_amount,
            "has_reversals": reversal_count > 0,
            "reversal_count": reversal_count,
            "status": transfer.status,
            "razorpay_transfer_id": transfer.razorpay_transfer_id,
            "failure_reason": transfer.failure_reason,
            "processed_at": transfer.processed_at,
            "reversed_at": transfer.reversed_at,
            "created_at": transfer.created_at,
            "updated_at": transfer.updated_at,
        }

    async def _get_rfid_ride_for_update_or_404(
        self,
        rfid_ride_id: str,
    ) -> schema.RFIDTripRide:
        stmt = (
            select(schema.RFIDTripRide)
            .where(schema.RFIDTripRide.id == rfid_ride_id)
            .with_for_update()
            .limit(1)
        )
        result = await self.db.execute(stmt)
        ride = result.scalar_one_or_none()

        if ride is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "rfid_ride_not_found",
                    "message": "RFID ride not found.",
                },
            )

        return ride

    async def _get_rfid_payout_transfers_for_ride_for_update(
        self,
        rfid_ride_id: str,
    ) -> list[schema.RFIDPayoutTransfer]:
        stmt = (
            select(schema.RFIDPayoutTransfer)
            .where(schema.RFIDPayoutTransfer.rfid_ride_id == rfid_ride_id)
            .order_by(schema.RFIDPayoutTransfer.created_at.asc())
            .with_for_update()
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    def _remaining_payout_transfer_amount(
        transfer: schema.RFIDPayoutTransfer,
    ) -> Decimal:
        return AdminRFIDService._normalize_money(
            Decimal(transfer.amount or 0)
            - Decimal(transfer.reversed_amount or 0)
        )

    @staticmethod
    def _is_rfid_payout_transfer_locally_reversible(
        transfer: schema.RFIDPayoutTransfer,
    ) -> bool:
        return transfer.status in {
            schema.RFIDPayoutTransferStatus.READY,
            schema.RFIDPayoutTransferStatus.WITHHELD,
            schema.RFIDPayoutTransferStatus.FAILED,
        }

    @staticmethod
    def _remaining_funding_allocation_amount(
        allocation: schema.RFIDRechargeFundingAllocation,
    ) -> Decimal:
        return AdminRFIDService._normalize_money(
            Decimal(allocation.amount or 0)
            - Decimal(allocation.reversed_amount or 0)
        )

    async def _get_rfid_funding_allocation_for_update_or_404(
        self,
        allocation_id: str,
    ) -> schema.RFIDRechargeFundingAllocation:
        stmt = (
            select(schema.RFIDRechargeFundingAllocation)
            .where(schema.RFIDRechargeFundingAllocation.id == allocation_id)
            .with_for_update()
            .limit(1)
        )
        result = await self.db.execute(stmt)
        allocation = result.scalar_one_or_none()

        if allocation is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "rfid_funding_allocation_not_found",
                    "message": "RFID funding allocation not found.",
                },
            )

        return allocation

    async def _get_rfid_funding_lot_for_update_or_404(
        self,
        funding_lot_id: str,
    ) -> schema.RFIDFundingLot:
        stmt = (
            select(schema.RFIDFundingLot)
            .where(schema.RFIDFundingLot.id == funding_lot_id)
            .with_for_update()
            .limit(1)
        )
        result = await self.db.execute(stmt)
        funding_lot = result.scalar_one_or_none()

        if funding_lot is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "rfid_funding_lot_not_found",
                    "message": "RFID funding lot not found.",
                },
            )

        return funding_lot

    async def _restore_rfid_funding_allocation_amount(
        self,
        *,
        allocation: schema.RFIDRechargeFundingAllocation,
        amount: Decimal,
    ) -> schema.RFIDFundingLot:
        amount_to_restore = self._normalize_money(amount)

        if amount_to_restore <= Decimal("0.00"):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "non_positive_funding_restore_amount",
                    "message": "Funding restore amount must be greater than zero.",
                },
            )

        remaining_allocation_amount = self._remaining_funding_allocation_amount(
            allocation
        )

        if amount_to_restore > remaining_allocation_amount:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_funding_restore_exceeds_allocation",
                    "message": "Cannot restore more than the unreversed funding allocation amount.",
                    "allocation_id": allocation.id,
                    "amount_to_restore": str(amount_to_restore),
                    "remaining_allocation_amount": str(remaining_allocation_amount),
                },
            )

        funding_lot = await self._get_rfid_funding_lot_for_update_or_404(
            allocation.funding_lot_id
        )

        lot_remaining_before = self._normalize_money(
            Decimal(funding_lot.remaining_amount or 0)
        )
        lot_source_amount = self._normalize_money(
            Decimal(funding_lot.source_amount or 0)
        )
        lot_remaining_after = self._normalize_money(
            lot_remaining_before + amount_to_restore
        )

        if lot_remaining_after > lot_source_amount:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_funding_lot_restore_exceeds_source",
                    "message": "Cannot restore funding lot above its original source amount.",
                    "funding_lot_id": funding_lot.id,
                    "amount_to_restore": str(amount_to_restore),
                    "lot_remaining_before": str(lot_remaining_before),
                    "lot_source_amount": str(lot_source_amount),
                },
            )

        allocation.reversed_amount = self._normalize_money(
            Decimal(allocation.reversed_amount or 0) + amount_to_restore
        )
        allocation.reversed_at = schema.utcnow()

        funding_lot.remaining_amount = lot_remaining_after

        if lot_remaining_after > Decimal("0.00"):
            funding_lot.status = schema.RFIDFundingLotStatus.AVAILABLE

        self.db.add(allocation)
        self.db.add(funding_lot)

        return funding_lot

    async def _refresh_rfid_ride_transfer_status_from_rows(
        self,
        ride: schema.RFIDTripRide,
        transfers: list[schema.RFIDPayoutTransfer],
    ) -> None:
        if not transfers:
            return

        statuses = [transfer.status for transfer in transfers]
        now = schema.utcnow()

        if all(
            status == schema.RFIDPayoutTransferStatus.REVERSED
            for status in statuses
        ):
            ride.transfer_status = schema.TransferStatus.REVERSED
            ride.transfer_ready_at = None

        elif any(
            status == schema.RFIDPayoutTransferStatus.FAILED
            for status in statuses
        ):
            ride.transfer_status = schema.TransferStatus.FAILED

        elif any(
            status == schema.RFIDPayoutTransferStatus.WITHHELD
            for status in statuses
        ):
            ride.transfer_status = schema.TransferStatus.WITHHELD
            ride.transfer_ready_at = None

        elif any(
            status in {
                schema.RFIDPayoutTransferStatus.READY,
                schema.RFIDPayoutTransferStatus.CREATED,
            }
            for status in statuses
        ):
            ride.transfer_status = schema.TransferStatus.READY
            ride.transfer_ready_at = ride.transfer_ready_at or now

        elif any(
            status == schema.RFIDPayoutTransferStatus.PROCESSED
            for status in statuses
        ):
            ride.transfer_status = schema.TransferStatus.TRANSFERRED
            ride.transfer_processed_at = ride.transfer_processed_at or now

        self.db.add(ride)

    async def reverse_rfid_ride_deduction(
        self,
        *,
        rfid_ride_id: str,
        amount: Decimal,
        reason: str,
        admin_user_id: str,
        admin_note: str | None = None,
    ) -> dict[str, Any]:
        reversal_amount = self._normalize_money(amount)

        if reversal_amount <= Decimal("0.00"):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "non_positive_rfid_reversal_amount",
                    "message": "RFID deduction reversal amount must be greater than zero.",
                },
            )

        ride = await self._get_rfid_ride_for_update_or_404(rfid_ride_id)

        if ride.status != schema.RFIDRideStatus.COMPLETED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_ride_not_completed",
                    "message": "Only completed RFID rides can have fare deductions reversed.",
                    "ride_status": ride.status.value,
                },
            )

        fare_amount = self._normalize_money(Decimal(ride.fare_amount or 0))
        already_reversed_amount = self._normalize_money(
            Decimal(ride.fare_reversed_amount or 0)
        )
        remaining_reversible_fare = self._normalize_money(
            fare_amount - already_reversed_amount
        )

        if reversal_amount > remaining_reversible_fare:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_reversal_exceeds_remaining_fare",
                    "message": "Cannot reverse more than the remaining unreversed RFID fare amount.",
                    "fare_amount": str(fare_amount),
                    "already_reversed_amount": str(already_reversed_amount),
                    "remaining_reversible_fare": str(remaining_reversible_fare),
                    "requested_reversal_amount": str(reversal_amount),
                },
            )

        remaining_driver_payout_amount = self._normalize_money(
            Decimal(ride.driver_payout_amount or 0)
            - Decimal(ride.driver_payout_reversed_amount or 0)
        )
        remaining_platform_amount = self._normalize_money(
            Decimal(ride.platform_amount or 0)
            - Decimal(ride.platform_amount_reversed or 0)
        )
        remaining_snapshot_amount = self._normalize_money(
            max(remaining_driver_payout_amount, Decimal("0.00"))
            + max(remaining_platform_amount, Decimal("0.00"))
        )

        if reversal_amount > remaining_snapshot_amount:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_reversal_exceeds_remaining_snapshot_amount",
                    "message": "Cannot reverse more than the remaining unreversed driver/platform snapshot amount.",
                    "requested_reversal_amount": str(reversal_amount),
                    "remaining_snapshot_amount": str(remaining_snapshot_amount),
                    "remaining_driver_payout_amount": str(remaining_driver_payout_amount),
                    "remaining_platform_amount": str(remaining_platform_amount),
                },
            )

        driver_reversal_amount, platform_reversal_amount = (
            self._split_rfid_fare_reversal_by_snapshot(
                ride=ride,
                reversal_amount=reversal_amount,
            )
        )

        account = await self._get_card_account_for_update_or_404(ride.card_id)

        allocations = await self._get_rfid_funding_allocations_for_ride_for_update(
            ride.id
        )

        if not allocations:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_funding_allocations_not_found",
                    "message": "RFID funding allocation rows were not found for this ride, so reversal cannot safely restore funding lineage.",
                    "rfid_ride_id": ride.id,
                },
            )

        total_available_for_funding_restore = self._normalize_money(
            sum(
                self._remaining_funding_allocation_amount(allocation)
                for allocation in allocations
            )
        )

        if reversal_amount > total_available_for_funding_restore:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_reversal_exceeds_available_funding_allocations",
                    "message": "Cannot reverse more than the remaining unreversed RFID funding allocations.",
                    "requested_reversal_amount": str(reversal_amount),
                    "available_for_funding_restore": str(
                        total_available_for_funding_restore
                    ),
                },
            )

        transfers = await self._get_rfid_payout_transfers_for_ride_for_update(
            ride.id
        )

        allocation_by_id = {allocation.id: allocation for allocation in allocations}
        driver_sources: list[dict[str, Any]] = []
        blocked_provider_transfers: list[dict[str, Any]] = []
        total_available_for_driver_reversal = Decimal("0.00")

        if driver_reversal_amount > Decimal("0.00"):
            if not transfers:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "rfid_payout_transfers_not_found",
                        "message": "RFID payout transfer rows were not found for this ride, but this reversal needs driver payout reversal.",
                        "rfid_ride_id": ride.id,
                        "driver_reversal_amount": str(driver_reversal_amount),
                    },
                )

            for transfer in transfers:
                if transfer.source_funding_allocation_id is None:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": "rfid_transfer_missing_funding_allocation",
                            "message": "RFID payout transfer is missing its funding allocation link.",
                            "transfer_id": transfer.id,
                        },
                    )

                allocation = allocation_by_id.get(transfer.source_funding_allocation_id)

                if allocation is None:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": "rfid_transfer_funding_allocation_not_found",
                            "message": "RFID payout transfer references a funding allocation that was not found for this ride.",
                            "transfer_id": transfer.id,
                            "funding_allocation_id": transfer.source_funding_allocation_id,
                        },
                    )

                remaining_allocation_amount = self._remaining_funding_allocation_amount(
                    allocation
                )

                if remaining_allocation_amount <= Decimal("0.00"):
                    continue

                allocation_remaining_driver_component = (
                    self._rfid_driver_payout_component_for_fare_amount(
                        ride=ride,
                        fare_amount=remaining_allocation_amount,
                    )
                )

                if allocation_remaining_driver_component <= Decimal("0.00"):
                    continue

                remaining_transfer_amount = self._remaining_payout_transfer_amount(
                    transfer
                )

                if self._is_rfid_payout_transfer_locally_reversible(transfer):
                    available_driver_amount = self._normalize_money(
                        min(
                            remaining_transfer_amount,
                            allocation_remaining_driver_component,
                        )
                    )

                    if available_driver_amount > Decimal("0.00"):
                        driver_sources.append(
                            {
                                "mode": "local",
                                "transfer": transfer,
                                "allocation": allocation,
                                "available_driver_amount": available_driver_amount,
                            }
                        )
                        total_available_for_driver_reversal = self._normalize_money(
                            total_available_for_driver_reversal
                            + available_driver_amount
                        )

                    continue

                if self._is_rfid_payout_transfer_provider_side_state(transfer):
                    available_driver_amount = (
                        self._provider_reversed_driver_amount_available_for_fare_reversal(
                            ride=ride,
                            transfer=transfer,
                            allocation=allocation,
                        )
                    )

                    available_driver_amount = self._normalize_money(
                        min(
                            available_driver_amount,
                            allocation_remaining_driver_component,
                        )
                    )

                    if available_driver_amount > Decimal("0.00"):
                        driver_sources.append(
                            {
                                "mode": "provider_reversed",
                                "transfer": transfer,
                                "allocation": allocation,
                                "available_driver_amount": available_driver_amount,
                            }
                        )
                        total_available_for_driver_reversal = self._normalize_money(
                            total_available_for_driver_reversal
                            + available_driver_amount
                        )
                    else:
                        blocked_provider_transfers.append(
                            {
                                "transfer_id": transfer.id,
                                "status": transfer.status.value,
                                "remaining_payable_amount": str(
                                    remaining_transfer_amount
                                ),
                                "transfer_reversed_amount": str(
                                    self._normalize_money(
                                        Decimal(transfer.reversed_amount or 0)
                                    )
                                ),
                                "allocation_reversed_amount": str(
                                    self._normalize_money(
                                        Decimal(allocation.reversed_amount or 0)
                                    )
                                ),
                                "razorpay_transfer_id": transfer.razorpay_transfer_id,
                            }
                        )

            if driver_reversal_amount > total_available_for_driver_reversal:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "rfid_driver_reversal_exceeds_available_sources",
                        "message": "Cannot reverse this fare amount yet. Reverse provider-side payout transfers first, or choose an amount covered by local/provider-reversed driver payout sources.",
                        "requested_driver_reversal_amount": str(
                            driver_reversal_amount
                        ),
                        "available_for_driver_reversal": str(
                            total_available_for_driver_reversal
                        ),
                        "requested_fare_reversal_amount": str(reversal_amount),
                        "platform_reversal_amount": str(platform_reversal_amount),
                        "blocked_provider_transfers": blocked_provider_transfers,
                    },
                )

        remaining_funding_to_restore = reversal_amount
        funding_restore_items: list[dict[str, Any]] = []

        for allocation in allocations:
            if remaining_funding_to_restore <= Decimal("0.00"):
                break

            allocation_available_amount = self._remaining_funding_allocation_amount(
                allocation
            )

            if allocation_available_amount <= Decimal("0.00"):
                continue

            amount_from_allocation = self._normalize_money(
                min(allocation_available_amount, remaining_funding_to_restore)
            )

            if amount_from_allocation <= Decimal("0.00"):
                continue

            funding_lot = await self._restore_rfid_funding_allocation_amount(
                allocation=allocation,
                amount=amount_from_allocation,
            )

            funding_restore_items.append(
                {
                    "funding_allocation_id": allocation.id,
                    "funding_lot_id": funding_lot.id,
                    "restored_amount": amount_from_allocation,
                }
            )

            remaining_funding_to_restore = self._normalize_money(
                remaining_funding_to_restore - amount_from_allocation
            )

        if remaining_funding_to_restore > Decimal("0.00"):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_funding_restore_incomplete",
                    "message": "RFID reversal could not be fully restored across funding allocations.",
                    "remaining_funding_to_restore": str(remaining_funding_to_restore),
                },
            )

        remaining_driver_to_reverse = driver_reversal_amount
        transfer_reversal_items: list[dict[str, Any]] = []
        now = schema.utcnow()

        for source in driver_sources:
            if remaining_driver_to_reverse <= Decimal("0.00"):
                break

            source_available_amount = self._normalize_money(
                source["available_driver_amount"]
            )

            driver_amount_from_source = self._normalize_money(
                min(source_available_amount, remaining_driver_to_reverse)
            )

            if driver_amount_from_source <= Decimal("0.00"):
                continue

            mode = source["mode"]
            transfer = source["transfer"]
            allocation = source["allocation"]

            old_transfer_status = transfer.status

            if mode == "local":
                transfer.reversed_amount = self._normalize_money(
                    Decimal(transfer.reversed_amount or 0) + driver_amount_from_source
                )

            remaining_after_transfer_reversal = (
                self._remaining_payout_transfer_amount(transfer)
            )

            if remaining_after_transfer_reversal <= Decimal("0.00"):
                transfer.reversed_amount = self._normalize_money(
                    Decimal(transfer.amount or 0)
                )
                transfer.status = schema.RFIDPayoutTransferStatus.REVERSED
                transfer.reversed_at = transfer.reversed_at or now
                transfer.failure_reason = None

            self.db.add(transfer)

            transfer_reversal_items.append(
                {
                    "transfer_id": transfer.id,
                    "mode": mode,
                    "old_status": old_transfer_status,
                    "new_status": transfer.status,
                    "driver_payout_reversed_amount": driver_amount_from_source,
                    "remaining_payable_amount": remaining_after_transfer_reversal,
                    "funding_allocation_id": allocation.id,
                    "funding_lot_id": allocation.funding_lot_id,
                }
            )

            remaining_driver_to_reverse = self._normalize_money(
                remaining_driver_to_reverse - driver_amount_from_source
            )

        if remaining_driver_to_reverse > Decimal("0.00"):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_driver_reversal_incomplete",
                    "message": "RFID driver payout reversal could not be fully allocated across available payout sources.",
                    "remaining_driver_to_reverse": str(remaining_driver_to_reverse),
                },
            )

        current_balance_before = self._normalize_money(
            Decimal(account.current_balance or 0)
        )
        held_balance = self._normalize_money(Decimal(account.held_balance or 0))
        current_balance_after = self._normalize_money(
            current_balance_before + reversal_amount
        )

        account.current_balance = current_balance_after

        ride.fare_reversed_amount = self._normalize_money(
            already_reversed_amount + reversal_amount
        )
        ride.driver_payout_reversed_amount = self._normalize_money(
            Decimal(ride.driver_payout_reversed_amount or 0)
            + driver_reversal_amount
        )
        ride.platform_amount_reversed = self._normalize_money(
            Decimal(ride.platform_amount_reversed or 0)
            + platform_reversal_amount
        )

        ledger_note_parts = [
            f"RFID fare deduction reversed by admin. Reason: {reason}"
        ]

        if admin_note:
            ledger_note_parts.append(f"Admin note: {admin_note}")

        ledger_entry = schema.RFIDLedgerEntry(
            id=schema.new_id(),
            account_id=account.id,
            card_id=ride.card_id,
            passenger_user_id=ride.passenger_user_id,
            entry_type=schema.RFIDLedgerEntryType.FARE_REVERSAL_CREDIT,
            amount_delta=reversal_amount,
            held_delta=Decimal("0.00"),
            balance_after=current_balance_after,
            held_balance_after=held_balance,
            scheduled_trip_id=ride.scheduled_trip_id,
            rfid_ride_id=ride.id,
            stop_id=ride.dropoff_stop_id,
            created_by_admin_id=admin_user_id,
            note=" | ".join(ledger_note_parts),
            created_at=now,
        )

        if transfers:
            await self._refresh_rfid_ride_transfer_status_from_rows(ride, transfers)

        self.db.add(account)
        self.db.add(ride)
        self.db.add(ledger_entry)
        await self.db.flush()

        return {
            "message": "RFID fare deduction reversed successfully.",
            "rfid_ride_id": ride.id,
            "card_id": ride.card_id,
            "account_id": account.id,
            "passenger_user_id": ride.passenger_user_id,
            "reversal_amount": reversal_amount,
            "driver_payout_reversal_amount": driver_reversal_amount,
            "platform_reversal_amount": platform_reversal_amount,
            "fare_amount": fare_amount,
            "fare_reversed_amount": ride.fare_reversed_amount,
            "driver_payout_reversed_amount": ride.driver_payout_reversed_amount,
            "platform_amount_reversed": ride.platform_amount_reversed,
            "balance_before": current_balance_before,
            "balance_after": current_balance_after,
            "ledger_entry_id": ledger_entry.id,
            "funding_restores": funding_restore_items,
            "transfer_reversals": transfer_reversal_items,
            "ride_transfer_status": ride.transfer_status,
        }
    
    @staticmethod
    def serialize_payout_transfer_reversal(
        reversal: schema.RFIDPayoutTransferReversal,
    ) -> dict[str, Any]:
        return {
            "id": reversal.id,
            "rfid_payout_transfer_id": reversal.rfid_payout_transfer_id,
            "rfid_ride_id": reversal.rfid_ride_id,
            "driver_user_id": reversal.driver_user_id,
            "scheduled_trip_id": reversal.scheduled_trip_id,
            "route_id": reversal.route_id,
            "vehicle_id": reversal.vehicle_id,
            "amount": reversal.amount,
            "status": reversal.status,
            "razorpay_reversal_id": reversal.razorpay_reversal_id,
            "failure_reason": reversal.failure_reason,
            "requested_by_admin_id": reversal.requested_by_admin_id,
            "reason": reversal.reason,
            "admin_note": reversal.admin_note,
            "processed_at": reversal.processed_at,
            "created_at": reversal.created_at,
            "updated_at": reversal.updated_at,
        }

    async def list_rfid_payout_transfer_reversals(
        self,
        *,
        page: int,
        page_size: int,
        status: schema.RFIDPayoutTransferReversalStatus | None = None,
        rfid_payout_transfer_id: str | None = None,
        rfid_ride_id: str | None = None,
        driver_user_id: str | None = None,
        scheduled_trip_id: str | None = None,
    ) -> tuple[list[schema.RFIDPayoutTransferReversal], int]:
        filters = []

        if status is not None:
            filters.append(schema.RFIDPayoutTransferReversal.status == status)

        if rfid_payout_transfer_id is not None:
            filters.append(
                schema.RFIDPayoutTransferReversal.rfid_payout_transfer_id
                == rfid_payout_transfer_id
            )

        if rfid_ride_id is not None:
            filters.append(
                schema.RFIDPayoutTransferReversal.rfid_ride_id == rfid_ride_id
            )

        if driver_user_id is not None:
            filters.append(
                schema.RFIDPayoutTransferReversal.driver_user_id == driver_user_id
            )

        if scheduled_trip_id is not None:
            filters.append(
                schema.RFIDPayoutTransferReversal.scheduled_trip_id
                == scheduled_trip_id
            )

        count_stmt = select(func.count(schema.RFIDPayoutTransferReversal.id))
        list_stmt = select(schema.RFIDPayoutTransferReversal)

        if filters:
            count_stmt = count_stmt.where(*filters)
            list_stmt = list_stmt.where(*filters)

        list_stmt = (
            list_stmt
            .order_by(schema.RFIDPayoutTransferReversal.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        count_result = await self.db.execute(count_stmt)
        list_result = await self.db.execute(list_stmt)

        return (
            list(list_result.scalars().all()),
            int(count_result.scalar_one() or 0),
        )
    
    async def _attach_payout_transfer_reversal_summary(
        self,
        transfers: list[schema.RFIDPayoutTransfer],
    ) -> None:
        transfer_ids = [transfer.id for transfer in transfers]

        if not transfer_ids:
            return

        stmt = (
            select(
                schema.RFIDPayoutTransferReversal.rfid_payout_transfer_id,
                func.count(schema.RFIDPayoutTransferReversal.id),
                func.coalesce(
                    func.sum(
                        schema.RFIDPayoutTransferReversal.amount,
                    ),
                    Decimal("0.00"),
                ),
            )
            .where(
                schema.RFIDPayoutTransferReversal.rfid_payout_transfer_id.in_(
                    transfer_ids
                ),
                schema.RFIDPayoutTransferReversal.status
                == schema.RFIDPayoutTransferReversalStatus.PROCESSED,
            )
            .group_by(schema.RFIDPayoutTransferReversal.rfid_payout_transfer_id)
        )

        result = await self.db.execute(stmt)

        summary_by_transfer_id = {
            transfer_id: {
                "count": int(count or 0),
                "provider_reversed_amount": self._normalize_money(
                    Decimal(provider_reversed_amount or 0)
                ),
            }
            for transfer_id, count, provider_reversed_amount in result.all()
        }

        for transfer in transfers:
            summary = summary_by_transfer_id.get(
                transfer.id,
                {
                    "count": 0,
                    "provider_reversed_amount": Decimal("0.00"),
                },
            )
            transfer._rfid_reversal_count = summary["count"]
            transfer._rfid_provider_reversed_amount = summary[
                "provider_reversed_amount"
            ]
    
    @staticmethod
    def _rfid_driver_payout_component_for_fare_amount(
        *,
        ride: schema.RFIDTripRide,
        fare_amount: Decimal,
    ) -> Decimal:
        normalized_fare_amount = AdminRFIDService._normalize_money(fare_amount)
        ride_fare_amount = AdminRFIDService._normalize_money(
            Decimal(ride.fare_amount or 0)
        )
        ride_driver_payout_amount = AdminRFIDService._normalize_money(
            Decimal(ride.driver_payout_amount or 0)
        )

        if normalized_fare_amount <= Decimal("0.00"):
            return Decimal("0.00")

        if ride_fare_amount <= Decimal("0.00"):
            return Decimal("0.00")

        if ride_driver_payout_amount <= Decimal("0.00"):
            return Decimal("0.00")

        driver_component = AdminRFIDService._normalize_money(
            (normalized_fare_amount * ride_driver_payout_amount)
            / ride_fare_amount
        )

        if driver_component > ride_driver_payout_amount:
            return ride_driver_payout_amount

        return driver_component

    @staticmethod
    def _split_rfid_fare_reversal_by_snapshot(
        *,
        ride: schema.RFIDTripRide,
        reversal_amount: Decimal,
    ) -> tuple[Decimal, Decimal]:
        normalized_reversal_amount = AdminRFIDService._normalize_money(
            reversal_amount
        )

        remaining_driver_payout_amount = AdminRFIDService._normalize_money(
            Decimal(ride.driver_payout_amount or 0)
            - Decimal(ride.driver_payout_reversed_amount or 0)
        )
        remaining_platform_amount = AdminRFIDService._normalize_money(
            Decimal(ride.platform_amount or 0)
            - Decimal(ride.platform_amount_reversed or 0)
        )

        if remaining_driver_payout_amount < Decimal("0.00"):
            remaining_driver_payout_amount = Decimal("0.00")

        if remaining_platform_amount < Decimal("0.00"):
            remaining_platform_amount = Decimal("0.00")

        if normalized_reversal_amount <= Decimal("0.00"):
            return Decimal("0.00"), Decimal("0.00")

        if remaining_driver_payout_amount <= Decimal("0.00"):
            return Decimal("0.00"), normalized_reversal_amount

        if remaining_platform_amount <= Decimal("0.00"):
            return normalized_reversal_amount, Decimal("0.00")

        driver_reversal_amount = (
            AdminRFIDService._rfid_driver_payout_component_for_fare_amount(
                ride=ride,
                fare_amount=normalized_reversal_amount,
            )
        )

        if driver_reversal_amount > remaining_driver_payout_amount:
            driver_reversal_amount = remaining_driver_payout_amount

        platform_reversal_amount = AdminRFIDService._normalize_money(
            normalized_reversal_amount - driver_reversal_amount
        )

        if platform_reversal_amount > remaining_platform_amount:
            platform_reversal_amount = remaining_platform_amount
            driver_reversal_amount = AdminRFIDService._normalize_money(
                normalized_reversal_amount - platform_reversal_amount
            )

        return driver_reversal_amount, platform_reversal_amount

    async def _get_rfid_funding_allocations_for_ride_for_update(
        self,
        rfid_ride_id: str,
    ) -> list[schema.RFIDRechargeFundingAllocation]:
        stmt = (
            select(schema.RFIDRechargeFundingAllocation)
            .where(schema.RFIDRechargeFundingAllocation.rfid_ride_id == rfid_ride_id)
            .order_by(schema.RFIDRechargeFundingAllocation.allocated_at.asc())
            .with_for_update()
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    def _provider_reversed_driver_amount_available_for_fare_reversal(
        *,
        ride: schema.RFIDTripRide,
        transfer: schema.RFIDPayoutTransfer,
        allocation: schema.RFIDRechargeFundingAllocation,
    ) -> Decimal:
        transfer_reversed_amount = AdminRFIDService._normalize_money(
            Decimal(transfer.reversed_amount or 0)
        )

        allocation_driver_component_already_restored = (
            AdminRFIDService._rfid_driver_payout_component_for_fare_amount(
                ride=ride,
                fare_amount=Decimal(allocation.reversed_amount or 0),
            )
        )

        available_driver_amount = AdminRFIDService._normalize_money(
            transfer_reversed_amount - allocation_driver_component_already_restored
        )

        if available_driver_amount <= Decimal("0.00"):
            return Decimal("0.00")

        return available_driver_amount

    @staticmethod
    def _is_rfid_payout_transfer_provider_side_state(
        transfer: schema.RFIDPayoutTransfer,
    ) -> bool:
        return transfer.status in {
            schema.RFIDPayoutTransferStatus.CREATED,
            schema.RFIDPayoutTransferStatus.PROCESSED,
            schema.RFIDPayoutTransferStatus.REVERSED,
        }

    async def list_payout_ready_rfid_transfers(
        self,
        *,
        page: int,
        page_size: int,
        driver_user_id: str | None = None,
        scheduled_trip_id: str | None = None,
    ) -> tuple[list[schema.RFIDPayoutTransfer], int]:
        filters = [
            schema.RFIDPayoutTransfer.status
            == schema.RFIDPayoutTransferStatus.READY,
            schema.RFIDPayoutTransfer.amount > Decimal("0.00"),
        ]

        if driver_user_id is not None:
            filters.append(schema.RFIDPayoutTransfer.driver_user_id == driver_user_id)

        if scheduled_trip_id is not None:
            filters.append(
                schema.RFIDPayoutTransfer.scheduled_trip_id == scheduled_trip_id
            )

        count_stmt = select(func.count(schema.RFIDPayoutTransfer.id)).where(*filters)

        list_stmt = (
            select(schema.RFIDPayoutTransfer)
            .where(*filters)
            .order_by(schema.RFIDPayoutTransfer.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        count_result = await self.db.execute(count_stmt)
        list_result = await self.db.execute(list_stmt)

        transfers = list(list_result.scalars().all())
        await self._attach_payout_transfer_reversal_summary(transfers)
        return (
            transfers,
            int(count_result.scalar_one() or 0),
        )
    
    @staticmethod
    def serialize_recharge_funding_allocation(
        allocation: schema.RFIDRechargeFundingAllocation,
    ) -> dict[str, Any]:
        return {
            "id": allocation.id,
            "funding_lot_id": allocation.funding_lot_id,
            "recharge_id": allocation.recharge_id,
            "account_id": allocation.account_id,
            "card_id": allocation.card_id,
            "passenger_user_id": allocation.passenger_user_id,
            "rfid_ride_id": allocation.rfid_ride_id,
            "scheduled_trip_id": allocation.scheduled_trip_id,
            "route_id": allocation.route_id,
            "vehicle_id": allocation.vehicle_id,
            "driver_user_id": allocation.driver_user_id,
            "source_razorpay_payment_id": allocation.source_razorpay_payment_id,
            "amount": allocation.amount,
            "reversed_amount": allocation.reversed_amount,
            "allocated_at": allocation.allocated_at,
            "reversed_at": allocation.reversed_at,
            "created_at": allocation.created_at,
            "updated_at": allocation.updated_at,
        }

    @staticmethod
    def serialize_funding_lot(
        funding_lot: schema.RFIDFundingLot,
    ) -> dict[str, Any]:
        return {
            "id": funding_lot.id,
            "recharge_id": funding_lot.recharge_id,
            "account_id": funding_lot.account_id,
            "card_id": funding_lot.card_id,
            "source_amount": funding_lot.source_amount,
            "remaining_amount": funding_lot.remaining_amount,
            "razorpay_payment_id": funding_lot.razorpay_payment_id,
            "source_type": funding_lot.source_type,
            "status": funding_lot.status,
            "created_at": funding_lot.created_at,
            "updated_at": funding_lot.updated_at,
        }

    async def get_rfid_payout_transfer_detail(
        self,
        transfer_id: str,
    ) -> dict[str, Any]:
        transfer_stmt = (
            select(schema.RFIDPayoutTransfer)
            .where(schema.RFIDPayoutTransfer.id == transfer_id)
            .limit(1)
        )
        transfer_result = await self.db.execute(transfer_stmt)
        transfer = transfer_result.scalar_one_or_none()

        if transfer is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "rfid_payout_transfer_not_found",
                    "message": "RFID payout transfer not found.",
                },
            )

        await self._attach_payout_transfer_reversal_summary([transfer])

        allocation = None
        funding_lot = None
        source_recharge = None

        if transfer.source_funding_allocation_id is not None:
            allocation_stmt = (
                select(schema.RFIDRechargeFundingAllocation)
                .where(
                    schema.RFIDRechargeFundingAllocation.id
                    == transfer.source_funding_allocation_id
                )
                .limit(1)
            )
            allocation_result = await self.db.execute(allocation_stmt)
            allocation = allocation_result.scalar_one_or_none()

        if allocation is not None:
            funding_lot_stmt = (
                select(schema.RFIDFundingLot)
                .where(schema.RFIDFundingLot.id == allocation.funding_lot_id)
                .limit(1)
            )
            funding_lot_result = await self.db.execute(funding_lot_stmt)
            funding_lot = funding_lot_result.scalar_one_or_none()

        source_recharge_id = transfer.source_recharge_id

        if source_recharge_id is None and allocation is not None:
            source_recharge_id = allocation.recharge_id

        if source_recharge_id is not None:
            recharge_stmt = (
                select(schema.RFIDRecharge)
                .where(schema.RFIDRecharge.id == source_recharge_id)
                .limit(1)
            )
            recharge_result = await self.db.execute(recharge_stmt)
            source_recharge = recharge_result.scalar_one_or_none()

        reversals_stmt = (
            select(schema.RFIDPayoutTransferReversal)
            .where(
                schema.RFIDPayoutTransferReversal.rfid_payout_transfer_id
                == transfer.id
            )
            .order_by(schema.RFIDPayoutTransferReversal.created_at.desc())
        )
        reversals_result = await self.db.execute(reversals_stmt)
        reversals = list(reversals_result.scalars().all())

        return {
            "transfer": self.serialize_payout_transfer(transfer),
            "funding_allocation": None
            if allocation is None
            else self.serialize_recharge_funding_allocation(allocation),
            "funding_lot": None
            if funding_lot is None
            else self.serialize_funding_lot(funding_lot),
            "source_recharge": None
            if source_recharge is None
            else self.serialize_recharge(source_recharge),
            "reversals": [
                self.serialize_payout_transfer_reversal(reversal)
                for reversal in reversals
            ],
            "reversal_count": len(reversals),
        }
    
    async def get_rfid_ride_money_detail(
        self,
        rfid_ride_id: str,
    ) -> dict[str, Any]:
        ride_stmt = (
            select(schema.RFIDTripRide)
            .where(schema.RFIDTripRide.id == rfid_ride_id)
            .limit(1)
        )
        ride_result = await self.db.execute(ride_stmt)
        ride = ride_result.scalar_one_or_none()

        if ride is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "rfid_ride_not_found",
                    "message": "RFID ride not found.",
                },
            )

        ledger_stmt = (
            select(schema.RFIDLedgerEntry)
            .where(schema.RFIDLedgerEntry.rfid_ride_id == ride.id)
            .order_by(schema.RFIDLedgerEntry.created_at.asc())
        )
        ledger_result = await self.db.execute(ledger_stmt)
        ledger_entries = list(ledger_result.scalars().all())

        allocation_stmt = (
            select(schema.RFIDRechargeFundingAllocation)
            .where(schema.RFIDRechargeFundingAllocation.rfid_ride_id == ride.id)
            .order_by(schema.RFIDRechargeFundingAllocation.created_at.asc())
        )
        allocation_result = await self.db.execute(allocation_stmt)
        funding_allocations = list(allocation_result.scalars().all())

        transfer_stmt = (
            select(schema.RFIDPayoutTransfer)
            .where(schema.RFIDPayoutTransfer.rfid_ride_id == ride.id)
            .order_by(schema.RFIDPayoutTransfer.created_at.asc())
        )
        transfer_result = await self.db.execute(transfer_stmt)
        payout_transfers = list(transfer_result.scalars().all())

        await self._attach_payout_transfer_reversal_summary(payout_transfers)

        reversal_stmt = (
            select(schema.RFIDPayoutTransferReversal)
            .where(schema.RFIDPayoutTransferReversal.rfid_ride_id == ride.id)
            .order_by(schema.RFIDPayoutTransferReversal.created_at.asc())
        )
        reversal_result = await self.db.execute(reversal_stmt)
        payout_transfer_reversals = list(reversal_result.scalars().all())

        return {
            "ride": self.serialize_trip_ride(ride),
            "ledger_entries": [
                self.serialize_ledger_entry(ledger_entry)
                for ledger_entry in ledger_entries
            ],
            "funding_allocations": [
                self.serialize_recharge_funding_allocation(allocation)
                for allocation in funding_allocations
            ],
            "payout_transfers": [
                self.serialize_payout_transfer(transfer)
                for transfer in payout_transfers
            ],
            "payout_transfer_reversals": [
                self.serialize_payout_transfer_reversal(reversal)
                for reversal in payout_transfer_reversals
            ],
            "ledger_entry_count": len(ledger_entries),
            "funding_allocation_count": len(funding_allocations),
            "payout_transfer_count": len(payout_transfers),
            "payout_transfer_reversal_count": len(payout_transfer_reversals),
        }
    
    async def get_rfid_payout_operations_summary(
        self,
        *,
        driver_user_id: str | None = None,
        scheduled_trip_id: str | None = None,
    ) -> dict[str, Any]:
        transfer_filters = []
        reversal_filters = []

        if driver_user_id is not None:
            transfer_filters.append(
                schema.RFIDPayoutTransfer.driver_user_id == driver_user_id
            )
            reversal_filters.append(
                schema.RFIDPayoutTransferReversal.driver_user_id == driver_user_id
            )

        if scheduled_trip_id is not None:
            transfer_filters.append(
                schema.RFIDPayoutTransfer.scheduled_trip_id == scheduled_trip_id
            )
            reversal_filters.append(
                schema.RFIDPayoutTransferReversal.scheduled_trip_id
                == scheduled_trip_id
            )

        transfer_stmt = select(
            schema.RFIDPayoutTransfer.status,
            func.count(schema.RFIDPayoutTransfer.id),
            func.coalesce(
                func.sum(schema.RFIDPayoutTransfer.amount),
                Decimal("0.00"),
            ),
            func.coalesce(
                func.sum(schema.RFIDPayoutTransfer.reversed_amount),
                Decimal("0.00"),
            ),
        )

        if transfer_filters:
            transfer_stmt = transfer_stmt.where(*transfer_filters)

        transfer_stmt = transfer_stmt.group_by(schema.RFIDPayoutTransfer.status)

        transfer_result = await self.db.execute(transfer_stmt)

        payout_transfer_counts_by_status: dict[str, int] = {}
        payout_transfer_amount_by_status: dict[str, Decimal] = {}
        payout_transfer_reversed_amount_by_status: dict[str, Decimal] = {}
        payout_transfer_payable_amount_by_status: dict[str, Decimal] = {}

        for status, count, amount, reversed_amount in transfer_result.all():
            status_key = status.value
            normalized_amount = self._normalize_money(Decimal(amount or 0))
            normalized_reversed_amount = self._normalize_money(
                Decimal(reversed_amount or 0)
            )
            payable_amount = self._normalize_money(
                normalized_amount - normalized_reversed_amount
            )

            payout_transfer_counts_by_status[status_key] = int(count or 0)
            payout_transfer_amount_by_status[status_key] = normalized_amount
            payout_transfer_reversed_amount_by_status[
                status_key
            ] = normalized_reversed_amount
            payout_transfer_payable_amount_by_status[status_key] = payable_amount

        for status in schema.RFIDPayoutTransferStatus:
            payout_transfer_counts_by_status.setdefault(status.value, 0)
            payout_transfer_amount_by_status.setdefault(
                status.value,
                Decimal("0.00"),
            )
            payout_transfer_reversed_amount_by_status.setdefault(
                status.value,
                Decimal("0.00"),
            )
            payout_transfer_payable_amount_by_status.setdefault(
                status.value,
                Decimal("0.00"),
            )

        reversal_stmt = select(
            schema.RFIDPayoutTransferReversal.status,
            func.count(schema.RFIDPayoutTransferReversal.id),
            func.coalesce(
                func.sum(schema.RFIDPayoutTransferReversal.amount),
                Decimal("0.00"),
            ),
        )

        if reversal_filters:
            reversal_stmt = reversal_stmt.where(*reversal_filters)

        reversal_stmt = reversal_stmt.group_by(
            schema.RFIDPayoutTransferReversal.status
        )

        reversal_result = await self.db.execute(reversal_stmt)

        provider_reversal_counts_by_status: dict[str, int] = {}
        provider_reversal_amount_by_status: dict[str, Decimal] = {}

        for status, count, amount in reversal_result.all():
            status_key = status.value
            provider_reversal_counts_by_status[status_key] = int(count or 0)
            provider_reversal_amount_by_status[status_key] = self._normalize_money(
                Decimal(amount or 0)
            )

        for status in schema.RFIDPayoutTransferReversalStatus:
            provider_reversal_counts_by_status.setdefault(status.value, 0)
            provider_reversal_amount_by_status.setdefault(
                status.value,
                Decimal("0.00"),
            )

        return {
            "payout_transfer_total": sum(
                payout_transfer_counts_by_status.values()
            ),
            "payout_transfer_counts_by_status": payout_transfer_counts_by_status,
            "payout_transfer_amount_by_status": payout_transfer_amount_by_status,
            "payout_transfer_reversed_amount_by_status": (
                payout_transfer_reversed_amount_by_status
            ),
            "payout_transfer_payable_amount_by_status": (
                payout_transfer_payable_amount_by_status
            ),
            "provider_reversal_total": sum(
                provider_reversal_counts_by_status.values()
            ),
            "provider_reversal_counts_by_status": (
                provider_reversal_counts_by_status
            ),
            "provider_reversal_amount_by_status": (
                provider_reversal_amount_by_status
            ),
            "ready_transfer_count": payout_transfer_counts_by_status[
                schema.RFIDPayoutTransferStatus.READY.value
            ],
            "created_transfer_count": payout_transfer_counts_by_status[
                schema.RFIDPayoutTransferStatus.CREATED.value
            ],
            "processed_transfer_count": payout_transfer_counts_by_status[
                schema.RFIDPayoutTransferStatus.PROCESSED.value
            ],
            "failed_transfer_count": payout_transfer_counts_by_status[
                schema.RFIDPayoutTransferStatus.FAILED.value
            ],
            "withheld_transfer_count": payout_transfer_counts_by_status[
                schema.RFIDPayoutTransferStatus.WITHHELD.value
            ],
            "reversed_transfer_count": payout_transfer_counts_by_status[
                schema.RFIDPayoutTransferStatus.REVERSED.value
            ],
            "failed_provider_reversal_count": provider_reversal_counts_by_status[
                schema.RFIDPayoutTransferReversalStatus.FAILED.value
            ],
            "processed_provider_reversal_count": (
                provider_reversal_counts_by_status[
                    schema.RFIDPayoutTransferReversalStatus.PROCESSED.value
                ]
            ),
        }

    async def list_payout_ready_rfid_rides(
        self,
        *,
        page: int,
        page_size: int,
        driver_user_id: str | None = None,
        scheduled_trip_id: str | None = None,
    ) -> tuple[list[schema.RFIDTripRide], int]:
        filters = [
            schema.RFIDTripRide.status == schema.RFIDRideStatus.COMPLETED,
            schema.RFIDTripRide.transfer_status == schema.RFIDPayoutTransferStatus.READY,
            schema.RFIDTripRide.driver_payout_amount > Decimal("0.00"),
        ]

        if driver_user_id is not None:
            filters.append(schema.RFIDTripRide.driver_user_id == driver_user_id)

        if scheduled_trip_id is not None:
            filters.append(schema.RFIDTripRide.scheduled_trip_id == scheduled_trip_id)

        count_stmt = select(func.count(schema.RFIDTripRide.id)).where(*filters)

        list_stmt = (
            select(schema.RFIDTripRide)
            .where(*filters)
            .order_by(schema.RFIDTripRide.transfer_ready_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        count_result = await self.db.execute(count_stmt)
        list_result = await self.db.execute(list_stmt)

        return (
            list(list_result.scalars().all()),
            int(count_result.scalar_one() or 0),
        )
    
    async def _get_or_create_platform_settings_for_update(
        self,
    ) -> schema.PlatformSettings:
        stmt = (
            select(schema.PlatformSettings)
            .where(schema.PlatformSettings.settings_key == "default")
            .with_for_update()
            .limit(1)
        )
        result = await self.db.execute(stmt)
        settings = result.scalar_one_or_none()

        if settings is not None:
            return settings

        settings = schema.PlatformSettings(
            id=schema.new_id(),
            settings_key="default",
            allow_driver_rfid_seat_reservation=True,
        )

        self.db.add(settings)
        await self.db.flush()

        return settings

    async def get_rfid_seat_policy(self) -> dict[str, Any]:
        stmt = (
            select(schema.PlatformSettings)
            .where(schema.PlatformSettings.settings_key == "default")
            .limit(1)
        )
        result = await self.db.execute(stmt)
        settings = result.scalar_one_or_none()

        return {
            "allow_driver_rfid_seat_reservation": True
            if settings is None
            else bool(settings.allow_driver_rfid_seat_reservation),
        }

    async def update_rfid_seat_policy(
        self,
        *,
        allow_driver_rfid_seat_reservation: bool,
    ) -> dict[str, Any]:
        settings = await self._get_or_create_platform_settings_for_update()

        settings.allow_driver_rfid_seat_reservation = (
            allow_driver_rfid_seat_reservation
        )

        self.db.add(settings)
        await self.db.flush()

        return {
            "allow_driver_rfid_seat_reservation": bool(
                settings.allow_driver_rfid_seat_reservation
            ),
        }
    
