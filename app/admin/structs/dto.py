from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.db import schema
from app.tax import (
	normalize_gst_postal_code,
	normalize_gst_sac_code,
	normalize_gst_state_code,
	normalize_gstin,
)


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
	stops: list[RouteStopInput]


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
	radius_meters: int = Field(
		default=300, description="Geofence radius in meters"
	)


class FareEntry(BaseModel):
	pickup_stop_id: str
	dropoff_stop_id: str
	amount: Decimal = Field(..., ge=0)


class RouteFareCreate(BaseModel):
	route_id: str
	fares: list[FareEntry]


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
	stops: list[RouteStopInput]


class RatingCreate(BaseModel):
	trip_rating: int = Field(ge=1, le=5)
	driver_rating: int = Field(ge=1, le=5)
	review_text: str | None = None


class VehicleInspectionUpdate(BaseModel):
	status: schema.VehicleInspectionStatus
	reason: str | None = None


class AdminVehicleInspectionDriverBrief(BaseModel):
	user_id: str
	email: str | None = None
	full_name: str | None = None
	phone: str | None = None


class AdminVehiclePhysicalInspectionStatus(BaseModel):
	status: schema.VehicleInspectionStatus | None = None
	reason: str | None = None
	created_at: datetime | None = None
	reviewed_at: datetime | None = None


class AdminVehicleInspectionStatusItem(BaseModel):
	vehicle_id: str
	driver: AdminVehicleInspectionDriverBrief
	registration_number: str
	registration_valid_till: datetime | None = None
	vehicle_name: str
	vehicle_model: str
	color: str
	seat_count: int
	has_ac: bool
	vehicle_verification_status: schema.VehicleVerificationStatus
	is_active: bool
	physical_inspection: AdminVehiclePhysicalInspectionStatus
	created_at: datetime
	updated_at: datetime


class AdminVehicleInspectionPagination(BaseModel):
	page: int
	page_size: int
	total: int
	total_pages: int
	has_next: bool
	has_previous: bool


class AdminVehicleInspectionStatusListResponse(BaseModel):
	items: list[AdminVehicleInspectionStatusItem]
	pagination: AdminVehicleInspectionPagination


class PassengerManifestItem(BaseModel):
	booking_id: str
	passenger_name: str
	status: str


class TripManifestResponse(BaseModel):
	trip_id: str
	total_bookings: int
	passengers: list[PassengerManifestItem]


class AdminTripTrackingStopBriefResponse(BaseModel):
	stop_id: str
	name: str
	sequence_no: int


class AdminTripTrackingProgressPointResponse(AdminTripTrackingStopBriefResponse):
	action: Literal["arrived", "departed"]
	occurred_at: datetime


class AdminTripTrackingStopResponse(AdminTripTrackingStopBriefResponse):
	route_stop_id: str
	lat: float
	lng: float
	radius_meters: int
	assume_time_diff_minutes: int | None
	boarding_allowed: bool
	deboarding_allowed: bool
	planned_time_at_stop: datetime
	arrival_time: datetime | None
	departure_time: datetime | None
	state: Literal["upcoming", "arrived", "departed", "not_visited"]
	is_current_stop: bool
	is_next_stop: bool


class AdminTripTrackingActionResponse(AdminTripTrackingProgressPointResponse):
	event_id: str


class AdminTripTrackingRouteResponse(BaseModel):
	id: str
	name: str
	code: str
	has_ac: bool | None


class AdminTripTrackingDriverResponse(BaseModel):
	id: str
	name: str | None
	email: str


class AdminTripTrackingVehicleResponse(BaseModel):
	id: str
	registration_number: str
	vehicle_name: str
	vehicle_model: str


class AdminTripTrackingLocationResponse(BaseModel):
	lat: float | None
	lng: float | None


class AdminTripTrackingProgressResponse(BaseModel):
	total_stops: int
	arrived_stops: int
	departed_stops: int
	remaining_stops: int
	progress_percent: float
	position_state: Literal[
		"not_started",
		"at_stop",
		"between_stops",
		"finished",
	]


class AdminTripStopTrackingResponse(BaseModel):
	trip_id: str
	status: str
	is_current_trip: bool
	planned_start_at: datetime
	planned_end_at: datetime
	actual_start_at: datetime | None
	actual_end_at: datetime | None
	last_updated: datetime
	route: AdminTripTrackingRouteResponse
	driver: AdminTripTrackingDriverResponse | None
	vehicle: AdminTripTrackingVehicleResponse | None
	last_known_location: AdminTripTrackingLocationResponse
	progress: AdminTripTrackingProgressResponse
	current_stop: AdminTripTrackingProgressPointResponse | None
	last_action: AdminTripTrackingProgressPointResponse | None
	last_arrived_stop: AdminTripTrackingProgressPointResponse | None
	last_departed_stop: AdminTripTrackingProgressPointResponse | None
	next_stop: AdminTripTrackingStopBriefResponse | None
	stops: list[AdminTripTrackingStopResponse]
	actions: list[AdminTripTrackingActionResponse]


class BookingTimeDetails(BaseModel):
	booked_at: datetime
	arrived_at_bus: datetime | None = None
	departed_bus_at: datetime | None = None


class BookingFullDetailsResponse(BaseModel):
	booking_id: str
	passenger_name: str
	status: str
	times: BookingTimeDetails
	# You can add route_details here as well


class Location(BaseModel):
	lat: float | None = None
	lng: float | None = None


class StopLocation(BaseModel):
	booked_stop_id: str | None = None
	booked_stop_name: str | None = None
	booked_location: Location


class ScanEvent(BaseModel):
	scanned_at: datetime | None = None
	scan_type: str | None = None
	scan_location: Location
	matched_stop: dict
	within_radius: bool | None = None


class PassengerDetails(BaseModel):
	name: str
	email: str
	role: str | None = None
	is_active: bool | None = None
	profile_picture: str | None = None


class BookingFullDetailsResponsee(BaseModel):
	booking_id: str
	trip_id: str
	passenger: PassengerDetails
	booking_status: str | None = None
	trip_status: str | None = None
	route: dict
	scheduled_times: dict
	pickup_location: StopLocation
	dropoff_location: StopLocation
	boarding_event: ScanEvent
	deboarding_event: ScanEvent
	timeline: dict


# Implemented by Anubhab below this


class PayoutSettingsUpdate(BaseModel):
	commission_percent: Decimal = Field(..., ge=0, le=100)


class GSTSettingsUpdate(BaseModel):
	gstin: str | None = Field(default=None, max_length=15)
	gst_legal_name: str | None = Field(default=None, max_length=200)
	gst_trade_name: str | None = Field(default=None, max_length=200)
	gst_registered_address: str | None = Field(default=None, max_length=2000)
	gst_state_name: str | None = Field(default=None, max_length=100)
	gst_state_code: str | None = Field(default=None, max_length=2)
	gst_postal_code: str | None = Field(default=None, max_length=6)
	gst_sac_code: str | None = Field(default=None, max_length=8)
	gst_service_description: str | None = Field(default=None, max_length=255)
	gst_default_place_of_supply: str | None = Field(default=None, max_length=100)
	gst_default_place_of_supply_state_code: str | None = Field(
		default=None, max_length=2
	)
	gst_reverse_charge_applicable: bool | None = None
	gst_enabled: bool | None = None
	gst_cgst_rate_percent: Decimal | None = Field(default=None, ge=0, le=100)
	gst_sgst_rate_percent: Decimal | None = Field(default=None, ge=0, le=100)
	gst_igst_rate_percent: Decimal | None = Field(default=None, ge=0, le=100)
	gst_apply_on_ac_routes_only: bool | None = None
	gst_inclusive_pricing: bool | None = None

	@field_validator("gstin", mode="before")
	@classmethod
	def validate_gstin(cls, value: Any) -> str | None:
		if value is not None and not isinstance(value, str):
			raise ValueError("GSTIN must be a string.")
		return normalize_gstin(value)

	@field_validator(
		"gst_legal_name",
		"gst_trade_name",
		"gst_registered_address",
		"gst_state_name",
		"gst_service_description",
		"gst_default_place_of_supply",
		mode="before",
	)
	@classmethod
	def clean_optional_invoice_text(cls, value: Any) -> str | None:
		if value is None:
			return None
		if not isinstance(value, str):
			raise ValueError("GST invoice setting must be a string.")
		cleaned = value.strip()
		return cleaned or None

	@field_validator("gst_state_code", "gst_default_place_of_supply_state_code", mode="before")
	@classmethod
	def validate_state_code(cls, value: Any) -> str | None:
		if value is not None and not isinstance(value, str):
			raise ValueError("GST state code must be a string.")
		return normalize_gst_state_code(value)

	@field_validator("gst_postal_code", mode="before")
	@classmethod
	def validate_postal_code(cls, value: Any) -> str | None:
		if value is not None and not isinstance(value, str):
			raise ValueError("GST postal code must be a string.")
		return normalize_gst_postal_code(value)

	@field_validator("gst_sac_code", mode="before")
	@classmethod
	def validate_sac_code(cls, value: Any) -> str | None:
		if value is not None and not isinstance(value, str):
			raise ValueError("GST SAC code must be a string.")
		return normalize_gst_sac_code(value)


class DriverLinkedAccountUpdate(BaseModel):
	razorpay_linked_account_id: str | None = Field(default=None, max_length=64)
	linked_account_status: schema.LinkedAccountStatus


class DriverPayoutEligibilityUpdate(BaseModel):
	is_payout_eligible: bool


class DriverPayoutDetailsUpsert(BaseModel):
	account_holder_name: str = Field(..., min_length=1, max_length=120)
	bank_account_number: str = Field(..., min_length=1, max_length=64)
	ifsc_code: str = Field(..., min_length=1, max_length=20)
	phone_number: str = Field(..., min_length=1, max_length=20)


class BookingPayoutAdjustmentAllocationInput(BaseModel):
	adjustment_id: str = Field(..., min_length=1, max_length=36)
	applied_amount: Decimal = Field(..., gt=0)


class BookingPayoutExecutionInput(BaseModel):
	booking_id: str = Field(..., min_length=1, max_length=36)
	adjustments_to_apply: list[BookingPayoutAdjustmentAllocationInput] = Field(
		default_factory=list
	)


class TriggerDriverMonthlyPayoutRequest(BaseModel):
	month: int = Field(..., ge=1, le=12)
	year: int = Field(..., ge=2000, le=2100)
	linked_account_id: str | None = Field(default=None, max_length=64)
	booking_items: list[BookingPayoutExecutionInput] = Field(
		default_factory=list
	)


class BulkPayoutTriggerRequest(BaseModel):
	booking_ids: list[str] = Field(default_factory=list)
	driver_user_id: str | None = None
	month: int | None = Field(default=None, ge=1, le=12)
	year: int | None = Field(default=None, ge=2000, le=2100)
	linked_account_id: str | None = Field(default=None, max_length=64)
	require_completed: bool = True
	only_ready: bool = True
	limit: int = Field(default=100, ge=1, le=500)
	booking_items: list[BookingPayoutExecutionInput] = Field(
		default_factory=list
	)


class PayoutDashboardResponse(BaseModel):
	commission_percent: Decimal

	ready_booking_count: int
	ready_total_amount: Decimal

	transferred_booking_count: int
	transferred_total_amount: Decimal

	withheld_booking_count: int
	withheld_total_amount: Decimal

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
	results: list[dict]


class BookingTransferListItem(BaseModel):
	transfer_id: str
	booking_id: str
	driver_user_id: str
	source_booking_payment_id: str
	linked_account_id: str
	razorpay_transfer_id: str | None = None
	amount: Decimal
	status: schema.BookingTransferStatus
	failure_reason: str | None = None
	processed_at: datetime | None = None
	reversed_at: datetime | None = None
	created_at: datetime
	updated_at: datetime


class BookingTransferListResponse(BaseModel):
	items: list[BookingTransferListItem]
	count: int


class PayoutBookingListItem(BaseModel):
	booking_id: str
	scheduled_trip_id: str
	driver_user_id: str
	passenger_user_id: str
	booking_status: schema.BookingStatus
	fare_amount: Decimal
	taxable_amount: Decimal
	cgst_rate_percent_snapshot: Decimal
	cgst_amount: Decimal
	sgst_rate_percent_snapshot: Decimal
	sgst_amount: Decimal
	igst_rate_percent_snapshot: Decimal
	igst_amount: Decimal
	total_tax_amount: Decimal
	gst_enabled_snapshot: bool
	gst_inclusive_snapshot: bool
	commission_percent_snapshot: Decimal
	commission_amount: Decimal
	driver_payout_amount: Decimal
	transfer_status: schema.TransferStatus
	effective_payout_state: str
	refund_state: str | None = None
	latest_payment_status: schema.BookingPaymentStatus | None = None
	payment_statuses: list[schema.BookingPaymentStatus] = Field(
		default_factory=list
	)
	transfer_ready_at: datetime | None = None
	transfer_processed_at: datetime | None = None
	cancelled_at: datetime | None = None
	cancellation_metadata: dict[str, Any] | None = None
	completed_at: datetime | None = None
	created_at: datetime
	updated_at: datetime
	applied_adjustment_amount: Decimal = Decimal("0.00")
	net_payout_amount: Decimal = Decimal("0.00")
	withheld_at: datetime | None = None


class PayoutBookingListResponse(BaseModel):
	items: list[PayoutBookingListItem]
	count: int


class RefundQueueItem(BaseModel):
	booking_id: str
	scheduled_trip_id: str
	passenger_user_id: str
	driver_user_id: str
	fare_amount: Decimal
	taxable_amount: Decimal
	cgst_rate_percent_snapshot: Decimal
	cgst_amount: Decimal
	sgst_rate_percent_snapshot: Decimal
	sgst_amount: Decimal
	igst_rate_percent_snapshot: Decimal
	igst_amount: Decimal
	total_tax_amount: Decimal
	gst_enabled_snapshot: bool
	gst_inclusive_snapshot: bool
	transfer_status: schema.TransferStatus
	refund_state: str | None = None
	latest_payment_status: schema.BookingPaymentStatus | None = None
	refund_retry_after: datetime | None = None
	refund_attempt_count: int | None = None
	cancelled_at: datetime | None = None
	cancellation_metadata: dict[str, Any] | None = None
	created_at: datetime
	updated_at: datetime


class RefundQueueResponse(BaseModel):
	items: list[RefundQueueItem]
	count: int


class PayoutAdjustmentCreateRequest(BaseModel):
	adjustment_type: schema.PayoutAdjustmentType
	amount: Decimal = Field(..., gt=0)
	reason_code: str | None = Field(default=None, max_length=64)
	reason_text: str = Field(..., min_length=1)
	admin_note: str | None = None


class PayoutAdjustmentDecisionRequest(BaseModel):
	decision_status: schema.PayoutAdjustmentDecision
	admin_note: str | None = None


class PayoutAdjustmentAllocationInput(BaseModel):
	adjustment_id: str = Field(..., min_length=1, max_length=36)
	applied_amount: Decimal = Field(..., gt=0)


class PayoutAdjustmentApplicationItem(BaseModel):
	id: str
	payout_adjustment_id: str
	applied_on_booking_id: str
	booking_transfer_id: str | None = None
	applied_by_admin_id: str
	applied_amount: Decimal
	applied_at: datetime
	created_at: datetime
	updated_at: datetime


class PayoutAdjustmentItem(BaseModel):
	id: str
	origin_booking_id: str
	origin_driver_user_id: str | None = None
	adjustment_type: schema.PayoutAdjustmentType
	amount: Decimal
	applied_total: Decimal
	remaining_amount: Decimal
	reason_code: str | None = None
	reason_text: str
	admin_note: str | None = None
	decision_status: schema.PayoutAdjustmentDecision
	created_by_admin_id: str
	decided_by_admin_id: str | None = None
	decided_at: datetime | None = None
	created_at: datetime
	updated_at: datetime
	applications: list[PayoutAdjustmentApplicationItem] = Field(
		default_factory=list
	)


class PayoutAdjustmentListResponse(BaseModel):
	items: list[PayoutAdjustmentItem]
	count: int


class TriggerBookingPayoutRequest(BaseModel):
	linked_account_id: str | None = Field(default=None, max_length=64)
	require_completed: bool = True
	adjustments_to_apply: list[PayoutAdjustmentAllocationInput] = Field(
		default_factory=list
	)


CommercialRuleType = Literal["driver_trip_cancel", "trip_latency"]
CommercialFineMode = Literal["flat_per_booking", "percent_of_fare"]


class CommercialRuleConfig(BaseModel):
	min_minutes_before: int | None = None
	max_minutes_before: int | None = None
	min_minutes_late: int | None = None
	max_minutes_late: int | None = None
	grace_minutes: int | None = None
	allowed: bool | None = None
	fine_mode: CommercialFineMode | None = None
	fine_value: Decimal | None = Field(default=None, ge=0)


class CommercialRuleCreateRequest(BaseModel):
	rule_type: CommercialRuleType
	code: str = Field(..., min_length=1, max_length=64)
	title: str = Field(..., min_length=1, max_length=160)
	description: str | None = Field(default=None, max_length=1000)
	priority: int = Field(default=100, ge=0, le=100000)
	is_active: bool = True
	config: CommercialRuleConfig

	@field_validator("code")
	@classmethod
	def normalize_code(cls, value: str) -> str:
		cleaned = value.strip().lower()
		if not cleaned:
			raise ValueError("code cannot be empty")
		return cleaned


class CommercialRuleUpdateRequest(BaseModel):
	title: str | None = Field(default=None, min_length=1, max_length=160)
	description: str | None = Field(default=None, max_length=1000)
	priority: int | None = Field(default=None, ge=0, le=100000)
	config: CommercialRuleConfig | None = None


class CommercialRuleStatusUpdateRequest(BaseModel):
	is_active: bool


class CommercialRuleResponse(BaseModel):
	id: str
	rule_type: CommercialRuleType
	code: str
	title: str
	description: str | None
	priority: int
	is_active: bool
	config: dict[str, Any]
	created_at: datetime
	updated_at: datetime


class CommercialRuleListResponse(BaseModel):
	items: list[CommercialRuleResponse]
	count: int
