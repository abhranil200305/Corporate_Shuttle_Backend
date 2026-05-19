from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from math import asin, cos, radians, sin, sqrt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import schema
from app.rfid.scan_schemas import RFIDScanRequest


@dataclass(frozen=True)
class ActiveTripStopContext:
    scheduled_trip: schema.ScheduledTrip
    trip_event: schema.TripEvent
    route_stop: schema.RouteStop
    stop: schema.Stop
    vehicle: schema.Vehicle


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

    async def _get_card_account_by_card_id(
        self,
        card_id: str,
    ) -> schema.RFIDCardAccount | None:
        stmt = (
            select(schema.RFIDCardAccount)
            .where(schema.RFIDCardAccount.card_id == card_id)
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_card_account_for_update_by_card_id(
        self,
        card_id: str,
    ) -> schema.RFIDCardAccount | None:
        stmt = (
            select(schema.RFIDCardAccount)
            .where(schema.RFIDCardAccount.card_id == card_id)
            .with_for_update()
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_card_account_for_update_by_account_id(
        self,
        account_id: str,
    ) -> schema.RFIDCardAccount | None:
        stmt = (
            select(schema.RFIDCardAccount)
            .where(schema.RFIDCardAccount.id == account_id)
            .with_for_update()
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    def _money(value: Decimal | int | str | None) -> Decimal:
        return Decimal(value or 0).quantize(Decimal("0.01"))

    def _available_balance(self, account: schema.RFIDCardAccount) -> Decimal:
        return self._money(account.current_balance) - self._money(account.held_balance)
    
    async def _get_max_downstream_fare_from_stop(
        self,
        *,
        route_id: str,
        pickup_stop_id: str,
        pickup_sequence_no: int,
    ) -> Decimal | None:
        stmt = (
            select(func.max(schema.RouteFare.amount))
            .join(
                schema.RouteStop,
                (
                    schema.RouteStop.route_id == schema.RouteFare.route_id
                )
                & (
                    schema.RouteStop.stop_id == schema.RouteFare.dropoff_stop_id
                ),
            )
            .where(
                schema.RouteFare.route_id == route_id,
                schema.RouteFare.pickup_stop_id == pickup_stop_id,
                schema.RouteFare.is_active.is_(True),
                schema.RouteStop.sequence_no > pickup_sequence_no,
            )
        )

        result = await self.db.execute(stmt)
        amount = result.scalar_one_or_none()

        if amount is None:
            return None

        return self._money(amount)
    
    async def _get_fare_for_stop_pair(
        self,
        *,
        route_id: str,
        pickup_stop_id: str,
        dropoff_stop_id: str,
    ) -> Decimal | None:
        stmt = (
            select(schema.RouteFare.amount)
            .where(
                schema.RouteFare.route_id == route_id,
                schema.RouteFare.pickup_stop_id == pickup_stop_id,
                schema.RouteFare.dropoff_stop_id == dropoff_stop_id,
                schema.RouteFare.is_active.is_(True),
            )
            .limit(1)
        )

        result = await self.db.execute(stmt)
        amount = result.scalar_one_or_none()

        if amount is None:
            return None

        return self._money(amount)

    async def _allocate_rfid_fare_from_funding_lots(
        self,
        *,
        account_id: str,
        card_id: str,
        passenger_user_id: str | None,
        rfid_ride_id: str,
        scheduled_trip_id: str,
        route_id: str,
        vehicle_id: str,
        driver_user_id: str,
        fare_amount: Decimal,
    ) -> list[schema.RFIDRechargeFundingAllocation] | None:
        remaining_to_allocate = self._money(fare_amount)

        if remaining_to_allocate <= Decimal("0.00"):
            return []

        stmt = (
            select(schema.RFIDFundingLot)
            .where(
                schema.RFIDFundingLot.account_id == account_id,
                schema.RFIDFundingLot.card_id == card_id,
                schema.RFIDFundingLot.status == schema.RFIDFundingLotStatus.AVAILABLE,
                schema.RFIDFundingLot.remaining_amount > Decimal("0.00"),
            )
            .order_by(schema.RFIDFundingLot.created_at.asc())
            .with_for_update()
        )

        result = await self.db.execute(stmt)
        funding_lots = list(result.scalars().all())

        total_available = self._money(
            sum(
                self._money(funding_lot.remaining_amount)
                for funding_lot in funding_lots
            )
        )

        if total_available < remaining_to_allocate:
            return None

        allocations: list[schema.RFIDRechargeFundingAllocation] = []

        for funding_lot in funding_lots:
            if remaining_to_allocate <= Decimal("0.00"):
                break

            lot_remaining_before = self._money(funding_lot.remaining_amount)
            allocation_amount = self._money(
                min(lot_remaining_before, remaining_to_allocate)
            )

            if allocation_amount <= Decimal("0.00"):
                continue

            lot_remaining_after = self._money(
                lot_remaining_before - allocation_amount
            )

            allocation = schema.RFIDRechargeFundingAllocation(
                id=schema.new_id(),
                funding_lot_id=funding_lot.id,
                recharge_id=funding_lot.recharge_id,
                account_id=account_id,
                card_id=card_id,
                passenger_user_id=passenger_user_id,
                rfid_ride_id=rfid_ride_id,
                scheduled_trip_id=scheduled_trip_id,
                route_id=route_id,
                vehicle_id=vehicle_id,
                driver_user_id=driver_user_id,
                source_razorpay_payment_id=funding_lot.razorpay_payment_id,
                amount=allocation_amount,
                reversed_amount=Decimal("0.00"),
                allocated_at=schema.utcnow(),
            )

            funding_lot.remaining_amount = lot_remaining_after

            if lot_remaining_after <= Decimal("0.00"):
                funding_lot.status = schema.RFIDFundingLotStatus.EXHAUSTED

            self.db.add(allocation)
            self.db.add(funding_lot)
            allocations.append(allocation)

            remaining_to_allocate = self._money(
                remaining_to_allocate - allocation_amount
            )

        return allocations
    
    async def _get_driver_payout_details(
        self,
        driver_user_id: str,
    ) -> schema.DriverPayoutDetails | None:
        stmt = (
            select(schema.DriverPayoutDetails)
            .where(schema.DriverPayoutDetails.driver_user_id == driver_user_id)
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()
    
    async def _create_rfid_payout_transfers_for_allocations(
        self,
        *,
        allocations: list[schema.RFIDRechargeFundingAllocation],
        ride: schema.RFIDTripRide,
    ) -> list[schema.RFIDPayoutTransfer]:
        if not allocations:
            return []

        # Payout transfers reference these allocation rows through a DB FK.
        # The allocations may have been created in this same RFID scan transaction,
        # so flush the parent rows before creating child transfer rows.
        await self.db.flush()

        payout_details = await self._get_driver_payout_details(ride.driver_user_id)

        linked_account_id: str | None = None
        linked_account_is_usable = False

        if payout_details is not None:
            linked_account_id = payout_details.razorpay_linked_account_id
            linked_account_is_usable = (
                bool(linked_account_id)
                and payout_details.linked_account_status
                == schema.LinkedAccountStatus.ACTIVE
                and payout_details.route_product_status
                == schema.RouteProductStatus.ACTIVATED
                and payout_details.is_payout_eligible is True
            )

        transfers: list[schema.RFIDPayoutTransfer] = []

        for allocation in allocations:
            has_source_payment = bool(allocation.source_razorpay_payment_id)

            transfer_status = (
                schema.RFIDPayoutTransferStatus.READY
                if has_source_payment and linked_account_is_usable
                else schema.RFIDPayoutTransferStatus.WITHHELD
            )

            failure_reason = None
            if not has_source_payment:
                failure_reason = "rfid_funding_source_not_razorpay_payment"
            elif not linked_account_is_usable:
                failure_reason = "driver_linked_account_not_ready"

            transfer = schema.RFIDPayoutTransfer(
                id=schema.new_id(),
                rfid_ride_id=ride.id,
                driver_user_id=ride.driver_user_id,
                scheduled_trip_id=ride.scheduled_trip_id,
                route_id=ride.route_id,
                vehicle_id=ride.vehicle_id,
                source_recharge_id=allocation.recharge_id,
                source_funding_allocation_id=allocation.id,
                source_razorpay_payment_id=allocation.source_razorpay_payment_id,
                linked_account_id=linked_account_id if linked_account_is_usable else None,
                amount=allocation.amount,
                status=transfer_status,
                failure_reason=failure_reason,
            )

            self.db.add(transfer)
            transfers.append(transfer)

        return transfers
    
    async def settle_unclosed_rfid_rides_for_scheduled_trip(
        self,
        *,
        scheduled_trip_id: str,
    ) -> dict[str, Any]:
        trip_stmt = (
            select(schema.ScheduledTrip)
            .where(schema.ScheduledTrip.id == scheduled_trip_id)
            .with_for_update()
            .limit(1)
        )
        trip_result = await self.db.execute(trip_stmt)
        scheduled_trip = trip_result.scalar_one_or_none()

        if scheduled_trip is None:
            raise ValueError("rfid_settlement_scheduled_trip_not_found")

        route_stops_stmt = (
            select(schema.RouteStop)
            .where(schema.RouteStop.route_id == scheduled_trip.route_id)
            .order_by(schema.RouteStop.sequence_no.asc())
        )
        route_stops_result = await self.db.execute(route_stops_stmt)
        route_stops = list(route_stops_result.scalars().all())

        if not route_stops:
            raise ValueError("rfid_settlement_route_stops_not_found")

        open_rides_stmt = (
            select(schema.RFIDTripRide)
            .where(
                schema.RFIDTripRide.scheduled_trip_id == scheduled_trip_id,
                schema.RFIDTripRide.status == schema.RFIDRideStatus.BOARDED,
            )
            .order_by(schema.RFIDTripRide.boarded_at.asc())
            .with_for_update()
        )
        open_rides_result = await self.db.execute(open_rides_stmt)
        open_rides = list(open_rides_result.scalars().all())

        if not open_rides:
            return {
                "settled_count": 0,
                "settled_amount": "0.00",
                "settled_ride_ids": [],
            }

        now = schema.utcnow()
        settled_count = 0
        settled_amount = Decimal("0.00")
        settled_ride_ids: list[str] = []

        for ride in open_rides:
            terminal_route_stop = next(
                (
                    route_stop
                    for route_stop in reversed(route_stops)
                    if route_stop.sequence_no > ride.pickup_sequence_no
                ),
                None,
            )

            if terminal_route_stop is None:
                raise ValueError(
                    "rfid_settlement_no_downstream_terminal_stop"
                )

            card_account = await self._get_card_account_for_update_by_account_id(
                ride.account_id
            )

            if card_account is None:
                raise ValueError("rfid_settlement_card_account_not_found")

            hold_amount = self._money(ride.hold_amount)

            if hold_amount <= Decimal("0.00"):
                raise ValueError("rfid_settlement_hold_amount_invalid")

            fare_amount = hold_amount
            current_balance_before = self._money(card_account.current_balance)
            held_balance_before = self._money(card_account.held_balance)

            current_balance_after = self._money(
                current_balance_before - fare_amount
            )
            held_balance_after = self._money(
                held_balance_before - hold_amount
            )

            if current_balance_after < Decimal("0.00"):
                raise ValueError(
                    "rfid_settlement_card_balance_insufficient"
                )

            if held_balance_after < Decimal("0.00"):
                raise ValueError(
                    "rfid_settlement_held_balance_invalid"
                )

            funding_allocations = (
                await self._allocate_rfid_fare_from_funding_lots(
                    account_id=card_account.id,
                    card_id=ride.card_id,
                    passenger_user_id=ride.passenger_user_id,
                    rfid_ride_id=ride.id,
                    scheduled_trip_id=ride.scheduled_trip_id,
                    route_id=ride.route_id,
                    vehicle_id=ride.vehicle_id,
                    driver_user_id=ride.driver_user_id,
                    fare_amount=fare_amount,
                )
            )

            if funding_allocations is None:
                raise ValueError(
                    "rfid_settlement_funding_lots_insufficient"
                )

            debit_entry = schema.RFIDLedgerEntry(
                id=schema.new_id(),
                account_id=card_account.id,
                card_id=ride.card_id,
                passenger_user_id=ride.passenger_user_id,
                entry_type=schema.RFIDLedgerEntryType.FARE_DEBIT,
                amount_delta=-fare_amount,
                held_delta=Decimal("0.00"),
                balance_after=current_balance_after,
                held_balance_after=held_balance_before,
                scheduled_trip_id=ride.scheduled_trip_id,
                rfid_ride_id=ride.id,
                stop_id=terminal_route_stop.stop_id,
                note=(
                    "RFID max fare auto-debited at trip end because "
                    "passenger did not scan drop."
                ),
                created_at=now,
            )

            release_entry = schema.RFIDLedgerEntry(
                id=schema.new_id(),
                account_id=card_account.id,
                card_id=ride.card_id,
                passenger_user_id=ride.passenger_user_id,
                entry_type=schema.RFIDLedgerEntryType.HOLD_RELEASE,
                amount_delta=Decimal("0.00"),
                held_delta=-hold_amount,
                balance_after=current_balance_after,
                held_balance_after=held_balance_after,
                scheduled_trip_id=ride.scheduled_trip_id,
                rfid_ride_id=ride.id,
                stop_id=terminal_route_stop.stop_id,
                note=(
                    "RFID max fare hold released by trip-end "
                    "auto settlement."
                ),
                created_at=now,
            )

            ride.dropoff_stop_id = terminal_route_stop.stop_id
            ride.dropoff_sequence_no = terminal_route_stop.sequence_no
            ride.drop_rfid_scan_event_id = None
            ride.dropped_at = now
            ride.drop_lat = None
            ride.drop_lng = None
            ride.status = schema.RFIDRideStatus.COMPLETED
            ride.fare_amount = fare_amount
            ride.commission_percent_snapshot = Decimal("0.00")
            ride.commission_amount = Decimal("0.00")
            ride.driver_payout_amount = fare_amount
            ride.platform_amount = Decimal("0.00")
            ride.transfer_status = schema.RFIDPayoutTransferStatus.READY
            ride.transfer_ready_at = now

            card_account.current_balance = current_balance_after
            card_account.held_balance = held_balance_after

            payout_transfers = (
                await self._create_rfid_payout_transfers_for_allocations(
                    allocations=funding_allocations,
                    ride=ride,
                )
            )

            has_ready_transfer = any(
                transfer.status == schema.RFIDPayoutTransferStatus.READY
                for transfer in payout_transfers
            )
            has_withheld_transfer = any(
                transfer.status == schema.RFIDPayoutTransferStatus.WITHHELD
                for transfer in payout_transfers
            )

            if has_ready_transfer:
                ride.transfer_status = schema.RFIDPayoutTransferStatus.READY
                ride.transfer_ready_at = now
            elif has_withheld_transfer:
                ride.transfer_status = schema.RFIDPayoutTransferStatus.WITHHELD
                ride.transfer_ready_at = None

            self.db.add(debit_entry)
            self.db.add(release_entry)
            self.db.add(ride)
            self.db.add(card_account)

            settled_count += 1
            settled_amount = self._money(settled_amount + fare_amount)
            settled_ride_ids.append(ride.id)

        await self.db.flush()

        return {
            "settled_count": settled_count,
            "settled_amount": str(settled_amount),
            "settled_ride_ids": settled_ride_ids,
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
            .with_for_update()
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

    async def _get_vehicle_or_none(self, vehicle_id: str) -> schema.Vehicle | None:
        stmt = select(schema.Vehicle).where(schema.Vehicle.id == vehicle_id).limit(1)
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

        vehicle = await self._get_vehicle_or_none(device.vehicle_id)

        if vehicle is None:
            return None

        return ActiveTripStopContext(
            scheduled_trip=scheduled_trip,
            trip_event=trip_event,
            route_stop=route_stop,
            stop=stop,
            vehicle=vehicle,
        )

    async def _get_open_rfid_ride_for_card_on_trip(
        self,
        *,
        card_id: str,
        scheduled_trip_id: str,
    ) -> schema.RFIDTripRide | None:
        stmt = (
            select(schema.RFIDTripRide)
            .where(
                schema.RFIDTripRide.card_id == card_id,
                schema.RFIDTripRide.scheduled_trip_id == scheduled_trip_id,
                schema.RFIDTripRide.status == schema.RFIDRideStatus.BOARDED,
            )
            .order_by(schema.RFIDTripRide.boarded_at.desc())
            .with_for_update()
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    def _get_rfid_reserved_seat_count(vehicle: schema.Vehicle) -> int:
        return max(int(vehicle.default_rfid_reserved_seat_count or 0), 0)

    async def _count_open_rfid_rides_for_trip(self, scheduled_trip_id: str) -> int:
        stmt = (
            select(schema.RFIDTripRide.id)
            .where(
                schema.RFIDTripRide.scheduled_trip_id == scheduled_trip_id,
                schema.RFIDTripRide.status == schema.RFIDRideStatus.BOARDED,
            )
        )
        result = await self.db.execute(stmt)
        return len(result.scalars().all())
    
    async def _is_driver_rfid_seat_reservation_enabled(self) -> bool:
        stmt = (
            select(schema.PlatformSettings.allow_driver_rfid_seat_reservation)
            .where(schema.PlatformSettings.settings_key == "default")
            .limit(1)
        )
        result = await self.db.execute(stmt)
        value = result.scalar_one_or_none()

        return True if value is None else bool(value)

    async def record_scan(self, payload: RFIDScanRequest) -> dict[str, Any]:
        device = await self._get_device_by_serial(payload.device_serial_number)
        card_uid_hash = self.hash_card_uid(payload.card_uid)
        card = await self._get_card_by_uid_hash(card_uid_hash)

        passenger_user_id = None
        if card is not None:
            passenger_user_id = card.assigned_passenger_user_id

        card_account: schema.RFIDCardAccount | None = None
        active_context: ActiveTripStopContext | None = None
        open_ride: schema.RFIDTripRide | None = None
        scan_type = schema.RFIDScanType.BOARD
        max_downstream_fare: Decimal | None = None
        actual_drop_fare: Decimal | None = None
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
        elif card.inventory_status == schema.RFIDCardInventoryStatus.LOST:
            rejection_reason = "rfid_card_lost"
        elif card.authorization_status != schema.RFIDCardAuthorizationStatus.ALLOWED:
            rejection_reason = "rfid_card_blocked"
        elif (
            card.inventory_status != schema.RFIDCardInventoryStatus.ASSIGNED
            or card.assigned_passenger_user_id is None
        ):
            rejection_reason = "rfid_card_not_assigned"
        else:
            card_account = await self._get_card_account_for_update_by_card_id(card.id)

            if card_account is None:
                rejection_reason = "rfid_card_account_not_found"
            elif card_account.is_active is False:
                rejection_reason = "rfid_card_account_inactive"
            elif self._available_balance(card_account) < Decimal("0.00"):
                rejection_reason = "rfid_card_account_balance_invalid"
            else:
                active_context = await self._get_active_trip_stop_context_for_device(
                    device
                )

                if active_context is None:
                    rejection_reason = "no_active_trip_or_stop"
                else:
                    open_ride = await self._get_open_rfid_ride_for_card_on_trip(
                        card_id=card.id,
                        scheduled_trip_id=active_context.scheduled_trip.id,
                    )

                    if open_ride is not None:
                        scan_type = schema.RFIDScanType.DROP

            if active_context is not None:
                if payload.scan_lat is not None and payload.scan_lng is not None:
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

                if (
                    scan_type == schema.RFIDScanType.DROP
                    and open_ride is not None
                    and active_context.route_stop.sequence_no
                    <= open_ride.pickup_sequence_no
                ):
                    rejection_reason = "drop_stop_must_be_after_board_stop"
                elif scan_type == schema.RFIDScanType.DROP and open_ride is not None:
                    actual_drop_fare = await self._get_fare_for_stop_pair(
                        route_id=active_context.scheduled_trip.route_id,
                        pickup_stop_id=open_ride.pickup_stop_id,
                        dropoff_stop_id=active_context.stop.id,
                    )

                    if actual_drop_fare is None:
                        rejection_reason = "rfid_actual_drop_fare_not_configured"
                elif scan_type == schema.RFIDScanType.BOARD:
                    use_fixed_rfid_reserved_seats = (
                        await self._is_driver_rfid_seat_reservation_enabled()
                    )

                    rfid_seat_policy_allows_backend_boarding = False

                    if use_fixed_rfid_reserved_seats:
                        reserved_seat_count = self._get_rfid_reserved_seat_count(
                            active_context.vehicle
                        )

                        if reserved_seat_count <= 0:
                            rejection_reason = "rfid_seat_pool_not_configured"
                        else:
                            open_rfid_ride_count = (
                                await self._count_open_rfid_rides_for_trip(
                                    active_context.scheduled_trip.id
                                )
                            )

                            if open_rfid_ride_count >= reserved_seat_count:
                                rejection_reason = "rfid_seat_pool_full"
                            else:
                                rfid_seat_policy_allows_backend_boarding = True
                    else:
                        # Seat permission is physically managed by onboard crew.
                        # Backend intentionally does not check fixed RFID pool
                        # or dynamic empty seats when this policy is disabled.
                        rfid_seat_policy_allows_backend_boarding = True

                    if (
                        rfid_seat_policy_allows_backend_boarding
                        and card_account is not None
                    ):
                        max_downstream_fare = (
                            await self._get_max_downstream_fare_from_stop(
                                route_id=active_context.scheduled_trip.route_id,
                                pickup_stop_id=active_context.stop.id,
                                pickup_sequence_no=active_context.route_stop.sequence_no,
                            )
                        )

                        if max_downstream_fare is None:
                            rejection_reason = "rfid_downstream_fare_not_configured"
                        elif self._available_balance(card_account) < max_downstream_fare:
                            rejection_reason = (
                                "rfid_insufficient_balance_for_max_route_fare"
                            )
        scan_accepted = False
        scan_rejection_reason = rejection_reason
        response_message = "RFID scan recorded. Boarding/deboarding is not enabled yet."

        if (
            rejection_reason == "scan_processing_not_enabled"
            and active_context is not None
            and card is not None
            and card_account is not None
        ):
            if (
                scan_type == schema.RFIDScanType.BOARD
                and max_downstream_fare is not None
            ):
                scan_accepted = True
                scan_rejection_reason = None
                response_message = "RFID board scan accepted."
            elif (
                scan_type == schema.RFIDScanType.DROP
                and open_ride is not None
                and actual_drop_fare is not None
            ):
                scan_accepted = True
                scan_rejection_reason = None
                response_message = "RFID drop scan accepted."

        scan_event = schema.RFIDScanEvent(
            id=schema.new_id(),
            scan_type=scan_type,
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
            accepted=scan_accepted,
            rejection_reason=scan_rejection_reason,
            raw_payload_json=self._raw_payload_to_json(payload.raw_payload),
        )

        self.db.add(scan_event)
        await self.db.flush()

        if (
            scan_accepted
            and scan_type == schema.RFIDScanType.BOARD
        ):
            assert active_context is not None
            assert card is not None
            assert card_account is not None
            assert max_downstream_fare is not None

            now = schema.utcnow()

            current_balance = self._money(card_account.current_balance)
            held_balance_before = self._money(card_account.held_balance)
            held_balance_after = self._money(held_balance_before + max_downstream_fare)

            ride = schema.RFIDTripRide(
                id=schema.new_id(),
                card_id=card.id,
                account_id=card_account.id,
                passenger_user_id=card.assigned_passenger_user_id,
                scheduled_trip_id=active_context.scheduled_trip.id,
                route_id=active_context.scheduled_trip.route_id,
                vehicle_id=active_context.scheduled_trip.vehicle_id,
                driver_user_id=active_context.scheduled_trip.driver_user_id,
                pickup_stop_id=active_context.stop.id,
                pickup_sequence_no=active_context.route_stop.sequence_no,
                board_rfid_scan_event_id=scan_event.id,
                boarded_at=now,
                board_lat=payload.scan_lat,
                board_lng=payload.scan_lng,
                status=schema.RFIDRideStatus.BOARDED,
                hold_amount=max_downstream_fare,
                fare_amount=Decimal("0.00"),
                fare_reversed_amount=Decimal("0.00"),
                commission_percent_snapshot=Decimal("0.00"),
                commission_amount=Decimal("0.00"),
                driver_payout_amount=Decimal("0.00"),
                driver_payout_reversed_amount=Decimal("0.00"),
                platform_amount=Decimal("0.00"),
                platform_amount_reversed=Decimal("0.00"),
                transfer_status=schema.RFIDPayoutTransferStatus.WITHHELD,
            )

            self.db.add(ride)
            await self.db.flush()

            ledger_entry = schema.RFIDLedgerEntry(
                id=schema.new_id(),
                account_id=card_account.id,
                card_id=card.id,
                passenger_user_id=card.assigned_passenger_user_id,
                entry_type=schema.RFIDLedgerEntryType.FARE_HOLD,
                amount_delta=Decimal("0.00"),
                held_delta=max_downstream_fare,
                balance_after=current_balance,
                held_balance_after=held_balance_after,
                scheduled_trip_id=active_context.scheduled_trip.id,
                rfid_ride_id=ride.id,
                stop_id=active_context.stop.id,
                note="RFID max downstream fare held on boarding.",
                created_at=now,
            )

            card_account.held_balance = held_balance_after
            scan_event.rfid_ride_id = ride.id

            self.db.add(ledger_entry)
            self.db.add(card_account)
            self.db.add(scan_event)
        if (
            scan_accepted
            and scan_type == schema.RFIDScanType.DROP
            and active_context is not None
            and card is not None
            and card_account is not None
            and open_ride is not None
            and actual_drop_fare is not None
        ):
            now = schema.utcnow()

            current_balance_before = self._money(card_account.current_balance)
            held_balance_before = self._money(card_account.held_balance)
            hold_amount = self._money(open_ride.hold_amount)
            fare_amount = self._money(actual_drop_fare)

            if fare_amount > hold_amount:
                scan_event.accepted = False
                scan_event.rejection_reason = "rfid_fare_exceeds_held_amount"
                response_message = "RFID drop scan rejected."
            else:
                current_balance_after = self._money(
                    current_balance_before - fare_amount
                )
                held_balance_after = self._money(
                    held_balance_before - hold_amount
                )

                if current_balance_after < Decimal("0.00"):
                    scan_event.accepted = False
                    scan_event.rejection_reason = "rfid_card_balance_insufficient_at_drop"
                    response_message = "RFID drop scan rejected."
                elif held_balance_after < Decimal("0.00"):
                    scan_event.accepted = False
                    scan_event.rejection_reason = "rfid_card_held_balance_invalid_at_drop"
                    response_message = "RFID drop scan rejected."
                else:
                    funding_allocations = (
                        await self._allocate_rfid_fare_from_funding_lots(
                            account_id=card_account.id,
                            card_id=card.id,
                            passenger_user_id=card.assigned_passenger_user_id,
                            rfid_ride_id=open_ride.id,
                            scheduled_trip_id=active_context.scheduled_trip.id,
                            route_id=active_context.scheduled_trip.route_id,
                            vehicle_id=active_context.scheduled_trip.vehicle_id,
                            driver_user_id=active_context.scheduled_trip.driver_user_id,
                            fare_amount=fare_amount,
                        )
                    )

                    if funding_allocations is None:
                        scan_event.accepted = False
                        scan_event.rejection_reason = (
                            "rfid_funding_lots_insufficient_for_fare"
                        )
                        response_message = "RFID drop scan rejected."
                    else:
                        debit_entry = schema.RFIDLedgerEntry(
                            id=schema.new_id(),
                            account_id=card_account.id,
                            card_id=card.id,
                            passenger_user_id=card.assigned_passenger_user_id,
                            entry_type=schema.RFIDLedgerEntryType.FARE_DEBIT,
                            amount_delta=-fare_amount,
                            held_delta=Decimal("0.00"),
                            balance_after=current_balance_after,
                            held_balance_after=held_balance_before,
                            scheduled_trip_id=active_context.scheduled_trip.id,
                            rfid_ride_id=open_ride.id,
                            stop_id=active_context.stop.id,
                            note="RFID actual fare debited on drop.",
                            created_at=now,
                        )

                        release_amount = self._money(hold_amount - fare_amount)

                        release_entry = schema.RFIDLedgerEntry(
                            id=schema.new_id(),
                            account_id=card_account.id,
                            card_id=card.id,
                            passenger_user_id=card.assigned_passenger_user_id,
                            entry_type=schema.RFIDLedgerEntryType.HOLD_RELEASE,
                            amount_delta=Decimal("0.00"),
                            held_delta=-hold_amount,
                            balance_after=current_balance_after,
                            held_balance_after=held_balance_after,
                            scheduled_trip_id=active_context.scheduled_trip.id,
                            rfid_ride_id=open_ride.id,
                            stop_id=active_context.stop.id,
                            note=(
                                "RFID fare hold released on drop."
                                if release_amount > Decimal("0.00")
                                else "RFID fare hold fully consumed on drop."
                            ),
                            created_at=now,
                        )

                        open_ride.dropoff_stop_id = active_context.stop.id
                        open_ride.dropoff_sequence_no = (
                            active_context.route_stop.sequence_no
                        )
                        open_ride.drop_rfid_scan_event_id = scan_event.id
                        open_ride.dropped_at = now
                        open_ride.drop_lat = payload.scan_lat
                        open_ride.drop_lng = payload.scan_lng
                        open_ride.status = schema.RFIDRideStatus.COMPLETED
                        open_ride.fare_amount = fare_amount
                        open_ride.commission_percent_snapshot = Decimal("0.00")
                        open_ride.commission_amount = Decimal("0.00")
                        open_ride.driver_payout_amount = fare_amount
                        open_ride.platform_amount = Decimal("0.00")
                        open_ride.transfer_status = schema.RFIDPayoutTransferStatus.READY
                        open_ride.transfer_ready_at = now

                        card_account.current_balance = current_balance_after
                        card_account.held_balance = held_balance_after
                        scan_event.rfid_ride_id = open_ride.id

                        payout_transfers = (
                            await self._create_rfid_payout_transfers_for_allocations(
                                allocations=funding_allocations,
                                ride=open_ride,
                            )
                        )

                        has_ready_transfer = any(
                            transfer.status == schema.RFIDPayoutTransferStatus.READY
                            for transfer in payout_transfers
                        )
                        has_withheld_transfer = any(
                            transfer.status == schema.RFIDPayoutTransferStatus.WITHHELD
                            for transfer in payout_transfers
                        )

                        if has_ready_transfer:
                            open_ride.transfer_status = (
                                schema.RFIDPayoutTransferStatus.READY
                            )
                            open_ride.transfer_ready_at = now
                        elif has_withheld_transfer:
                            open_ride.transfer_status = (
                                schema.RFIDPayoutTransferStatus.WITHHELD
                            )
                            open_ride.transfer_ready_at = None

                        self.db.add(debit_entry)
                        self.db.add(release_entry)
                        self.db.add(open_ride)
                        self.db.add(card_account)
                        self.db.add(scan_event)
        if device is not None:
            device.last_seen_at = schema.utcnow()
            device.last_seen_lat = payload.scan_lat
            device.last_seen_lng = payload.scan_lng
            self.db.add(device)

        await self.db.flush()
        
        return {
            "accepted": scan_event.accepted,
            "scan_event_id": scan_event.id,
            "scan_type": scan_event.scan_type,
            "rejection_reason": scan_event.rejection_reason,
            "message": response_message,
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
