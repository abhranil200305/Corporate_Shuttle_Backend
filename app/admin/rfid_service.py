from __future__ import annotations

import hashlib
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
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

        recharge.credited_ledger_entry_id = ledger_entry.id

        account.current_balance = balance_after
        account.held_balance = held_balance_after

        self.db.add(recharge)
        self.db.add(funding_lot)
        self.db.add(ledger_entry)
        self.db.add(account)

        try:
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
            "commission_percent_snapshot": ride.commission_percent_snapshot,
            "commission_amount": ride.commission_amount,
            "driver_payout_amount": ride.driver_payout_amount,
            "driver_payout_reversed_amount": ride.driver_payout_reversed_amount,
            "platform_amount": ride.platform_amount,
            "platform_amount_reversed": ride.platform_amount_reversed,
            "transfer_status": ride.transfer_status,
            "transfer_ready_at": ride.transfer_ready_at,
            "transfer_processed_at": ride.transfer_processed_at,
            "created_at": ride.created_at,
            "updated_at": ride.updated_at,
        }

    def serialize_payout_transfer(
        self,
        transfer: schema.RFIDPayoutTransfer,
    ) -> dict[str, Any]:
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
            "amount": transfer.amount,
            "status": transfer.status,
            "razorpay_transfer_id": transfer.razorpay_transfer_id,
            "failure_reason": transfer.failure_reason,
            "processed_at": transfer.processed_at,
            "reversed_at": transfer.reversed_at,
            "created_at": transfer.created_at,
            "updated_at": transfer.updated_at,
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

        return (
            list(list_result.scalars().all()),
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
            "commission_percent_snapshot": ride.commission_percent_snapshot,
            "commission_amount": ride.commission_amount,
            "driver_payout_amount": ride.driver_payout_amount,
            "driver_payout_reversed_amount": ride.driver_payout_reversed_amount,
            "platform_amount": ride.platform_amount,
            "platform_amount_reversed": ride.platform_amount_reversed,
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
            "amount": transfer.amount,
            "reversed_amount": transfer.reversed_amount,
            "payable_amount": AdminRFIDService._normalize_money(
                Decimal(transfer.amount or 0) - Decimal(transfer.reversed_amount or 0)
            ),
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
            ride.transfer_status = schema.RFIDPayoutTransferStatus.REVERSED
            ride.transfer_ready_at = None

        elif all(
            status == schema.RFIDPayoutTransferStatus.PROCESSED
            for status in statuses
        ):
            ride.transfer_status = schema.RFIDPayoutTransferStatus.PROCESSED
            ride.transfer_processed_at = ride.transfer_processed_at or now

        elif any(
            status == schema.RFIDPayoutTransferStatus.FAILED
            for status in statuses
        ):
            ride.transfer_status = schema.RFIDPayoutTransferStatus.FAILED

        elif any(
            status == schema.RFIDPayoutTransferStatus.CREATED
            for status in statuses
        ):
            ride.transfer_status = schema.RFIDPayoutTransferStatus.CREATED

        elif any(
            status == schema.RFIDPayoutTransferStatus.READY
            for status in statuses
        ):
            ride.transfer_status = schema.RFIDPayoutTransferStatus.READY
            ride.transfer_ready_at = ride.transfer_ready_at or now

        elif any(
            status == schema.RFIDPayoutTransferStatus.WITHHELD
            for status in statuses
        ):
            ride.transfer_status = schema.RFIDPayoutTransferStatus.WITHHELD
            ride.transfer_ready_at = None

        self.db.add(ride)

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

        return (
            list(list_result.scalars().all()),
            int(count_result.scalar_one() or 0),
        )

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
    
