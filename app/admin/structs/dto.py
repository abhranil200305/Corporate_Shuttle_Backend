from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field

from app.db import schema


class stopCreate(BaseModel):
    name: str
    lat: Decimal
    lng: Decimal
    radius_meters: int = 100


class RouteCreateSchema(BaseModel):
    name: str
    code: str
    stop_ids: list[str]


class RouteStopInput(BaseModel):
    stop_id: str
    boarding_allowed: bool = True
    deboarding_allowed: bool = True
    assume_time_diff_minutes: int = 10  # Default estimate


class AddRouteStopsRequest(BaseModel):
    stops: List[RouteStopInput]


class VerificationUpdate(BaseModel):
    status: schema.DriverVerificationStatus
    rejection_reason: str | None = None


class VehicleVerificationUpdate(BaseModel):
    status: schema.VehicleVerificationStatus
    rejection_reason: str | None = None


class StopCreate(BaseModel):
    name: str = Field(..., example="Technopolis - Main Gate")
    latitude: float = Field(..., example=22.5815)
    longitude: float = Field(..., example=88.4355)
    radius_meters: int = Field(default=300, description="Geofence radius in meters")


class FareEntry(BaseModel):
    pickup_stop_id: str
    dropoff_stop_id: str
    amount: Decimal = Field(..., ge=0)


class RouteFareCreate(BaseModel):
    route_id: str
    fares: List[FareEntry]


class RouteStatusUpdate(BaseModel):
    is_active: bool


# This allows the "bulk" selection


class RouteCreate(BaseModel):
    name: str
    code: str
    has_ac: bool = False


class RouteStopInput(BaseModel):
    stop_id: str
    boarding_allowed: bool = True
    deboarding_allowed: bool = True
    assume_time_diff_minutes: int = 10


class BulkStopAddRequest(BaseModel):
    stops: List[RouteStopInput]


class RatingCreate(BaseModel):
    trip_rating: int = Field(ge=1, le=5)
    driver_rating: int = Field(ge=1, le=5)
    review_text: Optional[str] = None


# Implemented by Anubhab below this
from datetime import datetime


class PayoutSettingsUpdate(BaseModel):
    commission_percent: Decimal = Field(..., ge=0, le=100)


class DriverLinkedAccountUpdate(BaseModel):
    razorpay_linked_account_id: Optional[str] = Field(default=None, max_length=64)
    linked_account_status: schema.LinkedAccountStatus


class DriverPayoutEligibilityUpdate(BaseModel):
    is_payout_eligible: bool


class DriverPayoutDetailsUpsert(BaseModel):
    account_holder_name: str = Field(..., min_length=1, max_length=120)
    bank_account_number: str = Field(..., min_length=1, max_length=64)
    ifsc_code: str = Field(..., min_length=1, max_length=20)
    phone_number: str = Field(..., min_length=1, max_length=20)


class TriggerBookingPayoutRequest(BaseModel):
    linked_account_id: Optional[str] = Field(default=None, max_length=64)
    require_completed: bool = True


class TriggerDriverMonthlyPayoutRequest(BaseModel):
    month: int = Field(..., ge=1, le=12)
    year: int = Field(..., ge=2000, le=2100)
    linked_account_id: Optional[str] = Field(default=None, max_length=64)


class BulkPayoutTriggerRequest(BaseModel):
    booking_ids: List[str] = Field(default_factory=list)
    driver_user_id: Optional[str] = None
    month: Optional[int] = Field(default=None, ge=1, le=12)
    year: Optional[int] = Field(default=None, ge=2000, le=2100)
    linked_account_id: Optional[str] = Field(default=None, max_length=64)
    require_completed: bool = True
    only_ready: bool = True
    limit: int = Field(default=100, ge=1, le=500)


class PayoutDashboardResponse(BaseModel):
    commission_percent: Decimal

    ready_booking_count: int
    ready_total_amount: Decimal

    transferred_booking_count: int
    transferred_total_amount: Decimal

    failed_booking_count: int
    failed_total_amount: Decimal

    reversed_booking_count: int
    reversed_total_amount: Decimal

    refund_queue_count: int
    refund_queue_total_amount: Decimal

    drivers_missing_linked_account_count: int
    drivers_not_eligible_count: int


class TriggerPayoutResponse(BaseModel):
    message: str
    result: dict


class BulkPayoutTriggerResponse(BaseModel):
    message: str
    total_selected: int
    success_count: int
    failure_count: int
    results: List[dict]


class BookingTransferListItem(BaseModel):
    transfer_id: str
    booking_id: str
    driver_user_id: str
    source_booking_payment_id: str
    linked_account_id: str
    razorpay_transfer_id: Optional[str] = None
    amount: Decimal
    status: schema.BookingTransferStatus
    failure_reason: Optional[str] = None
    processed_at: Optional[datetime] = None
    reversed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class BookingTransferListResponse(BaseModel):
    items: List[BookingTransferListItem]
    count: int


class PayoutBookingListItem(BaseModel):
    booking_id: str
    scheduled_trip_id: str
    driver_user_id: str
    passenger_user_id: str
    booking_status: schema.BookingStatus
    fare_amount: Decimal
    commission_percent_snapshot: Decimal
    commission_amount: Decimal
    driver_payout_amount: Decimal
    transfer_status: schema.TransferStatus
    transfer_ready_at: Optional[datetime] = None
    transfer_processed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class PayoutBookingListResponse(BaseModel):
    items: List[PayoutBookingListItem]
    count: int


class RefundQueueItem(BaseModel):
    booking_id: str
    scheduled_trip_id: str
    passenger_user_id: str
    driver_user_id: str
    fare_amount: Decimal
    transfer_status: schema.TransferStatus
    refund_retry_after: Optional[datetime] = None
    refund_attempt_count: Optional[int] = None
    cancelled_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class RefundQueueResponse(BaseModel):
    items: List[RefundQueueItem]
    count: int