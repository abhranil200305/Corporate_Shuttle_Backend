from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

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