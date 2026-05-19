from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ============================================================
# shared helpers
# ============================================================


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _clean_required_text(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Value cannot be empty.")
    return cleaned


# ============================================================
# RFID devices
# ============================================================


class RFIDDeviceCreateRequest(BaseModel):
    serial_number: str = Field(..., min_length=1, max_length=120)
    vehicle_id: str = Field(..., min_length=1, max_length=36)
    is_active: bool = True
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("serial_number", "vehicle_id")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        return _clean_required_text(value)

    @field_validator("notes")
    @classmethod
    def validate_notes(cls, value: str | None) -> str | None:
        return _clean_optional_text(value)


class RFIDDeviceUpdateRequest(BaseModel):
    serial_number: str | None = Field(default=None, min_length=1, max_length=120)
    vehicle_id: str | None = Field(default=None, min_length=1, max_length=36)
    is_active: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("serial_number", "vehicle_id")
    @classmethod
    def validate_optional_required_text(cls, value: str | None) -> str | None:
        return _clean_optional_text(value)

    @field_validator("notes")
    @classmethod
    def validate_notes(cls, value: str | None) -> str | None:
        return _clean_optional_text(value)


class RFIDDeviceResponse(BaseModel):
    id: str
    serial_number: str
    vehicle_id: str
    is_active: bool
    decommissioned_at: datetime | None
    last_seen_at: datetime | None
    last_seen_lat: Decimal | None
    last_seen_lng: Decimal | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class RFIDDeviceListResponse(BaseModel):
    items: list[RFIDDeviceResponse]
    count: int


class RFIDDeviceMutationResponse(BaseModel):
    message: str
    device: RFIDDeviceResponse


# ============================================================
# RFID cards
# ============================================================


class RFIDCardRegisterRequest(BaseModel):
    card_uid: str = Field(..., min_length=1, max_length=255)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("card_uid")
    @classmethod
    def validate_card_uid(cls, value: str) -> str:
        return _clean_required_text(value)

    @field_validator("notes")
    @classmethod
    def validate_notes(cls, value: str | None) -> str | None:
        return _clean_optional_text(value)


class RFIDCardBulkRegisterRequest(BaseModel):
    card_uids: list[str] = Field(..., min_length=1, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("card_uids")
    @classmethod
    def validate_card_uids(cls, values: list[str]) -> list[str]:
        cleaned_values: list[str] = []
        seen: set[str] = set()

        for value in values:
            cleaned = _clean_required_text(value)
            if cleaned in seen:
                continue
            seen.add(cleaned)
            cleaned_values.append(cleaned)

        if not cleaned_values:
            raise ValueError("At least one RFID card UID is required.")

        return cleaned_values

    @field_validator("notes")
    @classmethod
    def validate_notes(cls, value: str | None) -> str | None:
        return _clean_optional_text(value)


class RFIDCardAssignRequest(BaseModel):
    passenger_user_id: str = Field(..., min_length=1, max_length=36)
    reason: str | None = Field(default=None, max_length=2000)

    @field_validator("passenger_user_id")
    @classmethod
    def validate_passenger_user_id(cls, value: str) -> str:
        return _clean_required_text(value)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        return _clean_optional_text(value)


class RFIDCardUnassignRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        return _clean_optional_text(value)


class RFIDCardBlockRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        return _clean_optional_text(value)


class RFIDCardReturnRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)
    sweep_remaining_balance: bool = True

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        return _clean_optional_text(value)


class RFIDCardDecommissionRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)
    sweep_remaining_balance: bool = True

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        return _clean_optional_text(value)


class RFIDCardResponse(BaseModel):
    id: str
    card_uid_masked: str | None
    inventory_status: str
    authorization_status: str
    assigned_passenger_user_id: str | None
    assigned_at: datetime | None
    returned_at: datetime | None
    decommissioned_at: datetime | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class RFIDCardAccountResponse(BaseModel):
    id: str
    card_id: str
    current_balance: Decimal
    held_balance: Decimal
    available_balance: Decimal
    currency: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class RFIDCardAssignmentResponse(BaseModel):
    id: str
    card_id: str
    passenger_user_id: str
    assigned_by_admin_id: str
    assigned_at: datetime
    unassigned_by_admin_id: str | None
    unassigned_at: datetime | None
    reason: str | None
    created_at: datetime
    updated_at: datetime


class RFIDCardDetailResponse(BaseModel):
    card: RFIDCardResponse
    account: RFIDCardAccountResponse | None
    current_assignment: RFIDCardAssignmentResponse | None = None


class RFIDCardListResponse(BaseModel):
    items: list[RFIDCardResponse]
    count: int


class RFIDCardMutationResponse(BaseModel):
    message: str
    card: RFIDCardResponse


class RFIDCardBulkRegisterItemResponse(BaseModel):
    card_uid_masked: str | None
    status: str
    card: RFIDCardResponse | None = None
    error: str | None = None


class RFIDCardBulkRegisterResponse(BaseModel):
    message: str
    created_count: int
    skipped_count: int
    items: list[RFIDCardBulkRegisterItemResponse]


# ============================================================
# RFID recharge / ledger
# ============================================================


class RFIDRechargeCreateRequest(BaseModel):
    card_id: str = Field(..., min_length=1, max_length=36)
    amount: Decimal = Field(..., gt=Decimal("0.00"))
    razorpay_order_id: str | None = Field(default=None, max_length=64)
    razorpay_payment_id: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("card_id")
    @classmethod
    def validate_card_id(cls, value: str) -> str:
        return _clean_required_text(value)

    @field_validator("razorpay_order_id", "razorpay_payment_id")
    @classmethod
    def validate_optional_payment_refs(cls, value: str | None) -> str | None:
        return _clean_optional_text(value)

    @field_validator("note")
    @classmethod
    def validate_note(cls, value: str | None) -> str | None:
        return _clean_optional_text(value)


class RFIDRechargeResponse(BaseModel):
    id: str
    account_id: str
    card_id: str
    passenger_user_id: str | None
    amount: Decimal
    status: str
    source_type: str
    razorpay_order_id: str | None
    razorpay_payment_id: str | None
    razorpay_status: str | None
    razorpay_amount: Decimal | None
    created_by_admin_id: str | None
    verified_by_admin_id: str | None
    credited_ledger_entry_id: str | None
    paid_at: datetime | None
    credited_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RFIDRechargeMutationResponse(BaseModel):
    message: str
    recharge: RFIDRechargeResponse
    account: RFIDCardAccountResponse


class RFIDRechargeListResponse(BaseModel):
    items: list[RFIDRechargeResponse]
    count: int


class RFIDLedgerEntryResponse(BaseModel):
    id: str
    account_id: str
    card_id: str
    passenger_user_id: str | None
    entry_type: str
    amount_delta: Decimal
    held_delta: Decimal
    balance_after: Decimal
    held_balance_after: Decimal
    source_recharge_id: str | None
    scheduled_trip_id: str | None
    rfid_ride_id: str | None
    stop_id: str | None
    razorpay_order_id: str | None
    razorpay_payment_id: str | None
    reverses_ledger_entry_id: str | None
    reversed_by_ledger_entry_id: str | None
    created_by_admin_id: str | None
    note: str | None
    created_at: datetime


class RFIDLedgerEntryListResponse(BaseModel):
    items: list[RFIDLedgerEntryResponse]
    count: int


# ============================================================
# RFID rides / scans / payout admin surfaces
# ============================================================


class RFIDScanEventResponse(BaseModel):
    id: str
    scan_type: str
    device_id: str | None
    device_serial_snapshot: str
    card_id: str | None
    passenger_user_id: str | None
    rfid_ride_id: str | None
    scheduled_trip_id: str | None
    route_id: str | None
    vehicle_id: str | None
    driver_user_id: str | None
    matched_stop_id: str | None
    matched_route_stop_id: str | None
    matched_sequence_no: int | None
    active_trip_event_id: str | None
    active_stop_arrival_time_snapshot: datetime | None
    active_stop_departure_time_snapshot: datetime | None
    scan_lat: Decimal | None
    scan_lng: Decimal | None
    within_radius: bool
    distance_from_stop_meters: Decimal | None
    accepted: bool
    rejection_reason: str | None
    created_at: datetime


class RFIDScanEventListResponse(BaseModel):
    items: list[RFIDScanEventResponse]
    count: int


class RFIDTripRideResponse(BaseModel):
    id: str
    card_id: str
    account_id: str
    passenger_user_id: str
    scheduled_trip_id: str
    route_id: str
    vehicle_id: str
    driver_user_id: str

    pickup_stop_id: str
    pickup_sequence_no: int
    board_rfid_scan_event_id: str | None
    boarded_at: datetime
    board_lat: Decimal | None
    board_lng: Decimal | None

    dropoff_stop_id: str | None
    dropoff_sequence_no: int | None
    drop_rfid_scan_event_id: str | None
    dropped_at: datetime | None
    drop_lat: Decimal | None
    drop_lng: Decimal | None

    status: str
    hold_amount: Decimal
    fare_amount: Decimal
    fare_reversed_amount: Decimal
    commission_percent_snapshot: Decimal
    commission_amount: Decimal
    driver_payout_amount: Decimal
    driver_payout_reversed_amount: Decimal
    platform_amount: Decimal
    platform_amount_reversed: Decimal
    transfer_status: str
    transfer_ready_at: datetime | None
    transfer_processed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RFIDTripRideListResponse(BaseModel):
    items: list[RFIDTripRideResponse]
    count: int


class RFIDPayoutTransferResponse(BaseModel):
    id: str
    rfid_ride_id: str
    driver_user_id: str
    scheduled_trip_id: str
    route_id: str
    vehicle_id: str
    source_recharge_id: str | None
    source_funding_allocation_id: str | None
    source_razorpay_payment_id: str | None
    linked_account_id: str | None
    amount: Decimal
    reversed_amount: Decimal
    payable_amount: Decimal
    provider_reversed_amount: Decimal = Decimal("0.00")
    has_reversals: bool = False
    reversal_count: int = 0
    status: str
    razorpay_transfer_id: str | None
    failure_reason: str | None
    processed_at: datetime | None
    reversed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RFIDPayoutTransferListResponse(BaseModel):
    items: list[RFIDPayoutTransferResponse]
    count: int

class RFIDPayoutTransferBulkTriggerRequest(BaseModel):
    transfer_ids: list[str] | None = Field(default=None, max_length=100)
    driver_user_id: str | None = Field(default=None, min_length=1, max_length=36)
    scheduled_trip_id: str | None = Field(default=None, min_length=1, max_length=36)
    limit: int = Field(default=25, ge=1, le=100)

    @field_validator("transfer_ids")
    @classmethod
    def validate_transfer_ids(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None

        cleaned_values: list[str] = []
        seen: set[str] = set()

        for value in values:
            cleaned = _clean_required_text(value)
            if cleaned in seen:
                continue
            seen.add(cleaned)
            cleaned_values.append(cleaned)

        if not cleaned_values:
            raise ValueError("At least one RFID payout transfer id is required.")

        return cleaned_values

    @field_validator("driver_user_id", "scheduled_trip_id")
    @classmethod
    def validate_optional_ids(cls, value: str | None) -> str | None:
        return _clean_optional_text(value)


class RFIDPayoutTransferRefreshWithheldRequest(BaseModel):
    driver_user_id: str | None = Field(default=None, min_length=1, max_length=36)
    scheduled_trip_id: str | None = Field(default=None, min_length=1, max_length=36)
    limit: int = Field(default=100, ge=1, le=100)

    @field_validator("driver_user_id", "scheduled_trip_id")
    @classmethod
    def validate_optional_ids(cls, value: str | None) -> str | None:
        return _clean_optional_text(value)
    
class RFIDPayoutTransferReconcileCreatedRequest(BaseModel):
    driver_user_id: str | None = Field(default=None, min_length=1, max_length=36)
    scheduled_trip_id: str | None = Field(default=None, min_length=1, max_length=36)
    limit: int = Field(default=100, ge=1, le=100)

    @field_validator("driver_user_id", "scheduled_trip_id")
    @classmethod
    def validate_optional_ids(cls, value: str | None) -> str | None:
        return _clean_optional_text(value)
    
class RFIDPayoutTransferReversalRequest(BaseModel):
    amount: Decimal = Field(..., gt=Decimal("0.00"))
    reason: str = Field(..., min_length=1, max_length=120)
    admin_note: str | None = Field(default=None, max_length=2000)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        return _clean_required_text(value)

    @field_validator("admin_note")
    @classmethod
    def validate_admin_note(cls, value: str | None) -> str | None:
        return _clean_optional_text(value)
    
class RFIDPayoutTransferReversalResponse(BaseModel):
    id: str
    rfid_payout_transfer_id: str
    rfid_ride_id: str
    driver_user_id: str
    scheduled_trip_id: str
    route_id: str
    vehicle_id: str
    amount: Decimal
    status: str
    razorpay_reversal_id: str | None
    failure_reason: str | None
    requested_by_admin_id: str
    reason: str
    admin_note: str | None
    processed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RFIDPayoutTransferReversalListResponse(BaseModel):
    items: list[RFIDPayoutTransferReversalResponse]
    count: int

class RFIDRechargeFundingAllocationResponse(BaseModel):
    id: str
    funding_lot_id: str
    recharge_id: str | None
    account_id: str
    card_id: str
    passenger_user_id: str | None
    rfid_ride_id: str | None
    scheduled_trip_id: str | None
    route_id: str | None
    vehicle_id: str | None
    driver_user_id: str | None
    source_razorpay_payment_id: str | None
    amount: Decimal
    reversed_amount: Decimal
    allocated_at: datetime
    reversed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RFIDFundingLotResponse(BaseModel):
    id: str
    recharge_id: str | None
    account_id: str
    card_id: str
    source_amount: Decimal
    remaining_amount: Decimal
    razorpay_payment_id: str | None
    source_type: str
    status: str
    created_at: datetime
    updated_at: datetime


class RFIDPayoutTransferDetailResponse(BaseModel):
    transfer: RFIDPayoutTransferResponse
    funding_allocation: RFIDRechargeFundingAllocationResponse | None
    funding_lot: RFIDFundingLotResponse | None
    source_recharge: RFIDRechargeResponse | None
    reversals: list[RFIDPayoutTransferReversalResponse]
    reversal_count: int

class RFIDRideMoneyDetailResponse(BaseModel):
    ride: RFIDTripRideResponse
    ledger_entries: list[RFIDLedgerEntryResponse]
    funding_allocations: list[RFIDRechargeFundingAllocationResponse]
    payout_transfers: list[RFIDPayoutTransferResponse]
    payout_transfer_reversals: list[RFIDPayoutTransferReversalResponse]

    ledger_entry_count: int
    funding_allocation_count: int
    payout_transfer_count: int
    payout_transfer_reversal_count: int

class RFIDPayoutOperationsSummaryResponse(BaseModel):
    payout_transfer_total: int
    payout_transfer_counts_by_status: dict[str, int]
    payout_transfer_amount_by_status: dict[str, Decimal]
    payout_transfer_reversed_amount_by_status: dict[str, Decimal]
    payout_transfer_payable_amount_by_status: dict[str, Decimal]

    provider_reversal_total: int
    provider_reversal_counts_by_status: dict[str, int]
    provider_reversal_amount_by_status: dict[str, Decimal]

    ready_transfer_count: int
    created_transfer_count: int
    processed_transfer_count: int
    failed_transfer_count: int
    withheld_transfer_count: int
    reversed_transfer_count: int

    failed_provider_reversal_count: int
    processed_provider_reversal_count: int

# ============================================================
# manual reversal request
# ============================================================


class RFIDRideDeductionReversalRequest(BaseModel):
    amount: Decimal = Field(..., gt=Decimal("0.00"))
    reason: str = Field(..., min_length=1, max_length=120)
    admin_note: str | None = Field(default=None, max_length=2000)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        return _clean_required_text(value)

    @field_validator("admin_note")
    @classmethod
    def validate_admin_note(cls, value: str | None) -> str | None:
        return _clean_optional_text(value)


class RFIDGenericMutationResponse(BaseModel):
    message: str
    data: dict[str, Any] | None = None

class AdminRFIDSeatPolicyResponse(BaseModel):
    allow_driver_rfid_seat_reservation: bool


class AdminRFIDSeatPolicyUpdateRequest(BaseModel):
    allow_driver_rfid_seat_reservation: bool

class RFIDDeviceVehicleOptionResponse(BaseModel):
    vehicle_id: str
    driver_user_id: str
    driver_name: str | None
    vehicle_license_plate: str


class RFIDDeviceVehicleOptionListResponse(BaseModel):
    items: list[RFIDDeviceVehicleOptionResponse]
    count: int


class RFIDCardOptionResponse(BaseModel):
    card_id: str
    card_uid_masked: str | None
    assigned_passenger_user_id: str | None
    assigned_passenger_name: str | None


class RFIDCardOptionListResponse(BaseModel):
    items: list[RFIDCardOptionResponse]
    count: int