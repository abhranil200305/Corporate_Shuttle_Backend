from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, Field, field_validator, model_validator


TRAVELLER_PHONE_SEPARATORS = re.compile(r"[\s().-]+")
INDIAN_MOBILE_NUMBER = re.compile(r"^[6-9][0-9]{9}$")
E164_NUMBER = re.compile(r"^[1-9][0-9]{7,14}$")


def normalize_traveller_phone(value: str) -> str:
    cleaned = value.strip()
    compact = TRAVELLER_PHONE_SEPARATORS.sub("", cleaned)

    if compact.startswith("+91"):
        national_number = compact[3:]
        if INDIAN_MOBILE_NUMBER.fullmatch(national_number):
            return f"+91{national_number}"
        raise ValueError(
            "Indian traveller phone must contain a valid 10-digit mobile "
            "number starting with 6, 7, 8, or 9."
        )

    if compact.startswith("+"):
        international_number = compact[1:]
        if E164_NUMBER.fullmatch(international_number):
            return f"+{international_number}"
        raise ValueError(
            "Traveller phone must be a valid international number with "
            "8 to 15 digits after the country code."
        )

    if compact.startswith("0"):
        compact = compact[1:]
    elif compact.startswith("91") and len(compact) == 12:
        compact = compact[2:]

    if INDIAN_MOBILE_NUMBER.fullmatch(compact):
        return f"+91{compact}"

    raise ValueError(
        "Traveller phone must be a valid Indian mobile number or an "
        "international number prefixed with +."
    )


def normalize_traveller_email(value: str | None) -> str | None:
    if value is None:
        return None

    try:
        return validate_email(value, check_deliverability=False).normalized
    except EmailNotValidError as exc:
        raise ValueError("Traveller email must be a valid email address.") from exc


class PassengerProfileUpsertRequest(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=120)
    profile_picture_path: str | None = Field(default=None, max_length=255)

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Full name cannot be empty.")
        return cleaned


class PassengerProfileResponse(BaseModel):
    id: str
    user_id: str
    full_name: str
    profile_picture_path: str | None
    created_at: datetime
    updated_at: datetime


class PassengerProfileMutationResponse(BaseModel):
    message: str
    profile: PassengerProfileResponse

class PassengerTravellerProfileCreateRequest(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=120)
    phone: str = Field(..., min_length=5, max_length=20)
    email: str | None = Field(default=None, max_length=255)
    relationship_label: str | None = Field(default=None, max_length=80)
    is_self: bool = False

    @field_validator("full_name", "phone", "email", "relationship_label")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("full_name", "phone")
    @classmethod
    def required_text_must_not_be_empty(cls, value: str | None) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("This field cannot be empty.")
        return cleaned

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        return normalize_traveller_phone(value)

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, value: str | None) -> str | None:
        return normalize_traveller_email(value)


class PassengerTravellerProfileUpdateRequest(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=120)
    phone: str | None = Field(default=None, min_length=5, max_length=20)
    email: str | None = Field(default=None, max_length=255)
    relationship_label: str | None = Field(default=None, max_length=80)
    is_self: bool | None = None
    is_active: bool | None = None

    @field_validator("full_name", "phone", "email", "relationship_label")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_traveller_phone(value)

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, value: str | None) -> str | None:
        return normalize_traveller_email(value)

    @model_validator(mode="after")
    def require_at_least_one_field(self):
        if (
            self.full_name is None
            and self.phone is None
            and self.email is None
            and self.relationship_label is None
            and self.is_self is None
            and self.is_active is None
        ):
            raise ValueError("At least one field must be provided.")
        return self


class PassengerTravellerProfileResponse(BaseModel):
    id: str
    owner_user_id: str
    full_name: str
    phone: str
    email: str | None
    relationship_label: str | None
    is_self: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime


class PassengerTravellerProfileListResponse(BaseModel):
    items: list[PassengerTravellerProfileResponse]
    count: int


class PassengerTravellerProfileMutationResponse(BaseModel):
    message: str
    profile: PassengerTravellerProfileResponse


class StopBriefResponse(BaseModel):
    id: str
    name: str
    lat: Decimal
    lng: Decimal
    radius_meters: int
    is_active: bool

class StopListResponse(BaseModel):
    items: list[StopBriefResponse]
    count: int


class StopSearchResultResponse(StopBriefResponse):
    name_match_score: float | None
    distance_km: Decimal | None


class StopSearchResponse(BaseModel):
    query: str | None
    lat: Decimal | None
    lng: Decimal | None
    radius_km: Decimal | None
    items: list[StopSearchResultResponse]
    count: int


class RouteStopResponse(BaseModel):
    route_stop_id: str
    sequence_no: int
    assume_time_diff_minutes: int | None
    boarding_allowed: bool
    deboarding_allowed: bool
    stop: StopBriefResponse


class ScheduledTripStopResponse(BaseModel):
    route_stop_id: str
    sequence_no: int
    assume_time_diff_minutes: int | None
    minutes_from_trip_start: int
    planned_time_at_stop: datetime
    actual_arrival_time: datetime | None
    actual_departure_time: datetime | None
    boarding_allowed: bool
    deboarding_allowed: bool
    stop: StopBriefResponse


class RouteResponse(BaseModel):
    id: str
    name: str
    code: str
    has_ac: bool | None
    is_active: bool
    stops: list[RouteStopResponse]


class RouteListResponse(BaseModel):
    items: list[RouteResponse]
    count: int


class DriverBriefResponse(BaseModel):
    id: str
    email: str


class VehicleBriefResponse(BaseModel):
    id: str
    registration_number: str
    vehicle_name: str
    vehicle_model: str
    color: str
    seat_count: int
    rfid_reserved_seat_count: int = 0
    app_bookable_seat_count: int = 0
    has_ac: bool


class ScheduledTripDriverVehicleInfoResponse(BaseModel):
    scheduled_trip_id: str
    driver_user_id: str
    driver_name: str | None
    driver_average_rating: Decimal | None
    driver_rating_count: int
    vehicle_registration_number: str | None
    vehicle_name: str | None
    vehicle_model: str | None
    vehicle_color: str | None
    vehicle_total_seat: str | int | None


class CancellationMetadataResponse(BaseModel):
    cancelled_at: datetime
    reason: str
    source: str
    cancelled_by_user_id: str | None


class ScheduledTripResponse(BaseModel):
    id: str
    route_id: str
    driver_user_id: str
    vehicle_id: str
    planned_start_at: datetime
    planned_end_at: datetime
    actual_start_at: datetime | None
    actual_end_at: datetime | None
    status: str
    admin_note: str | None
    cancellation_metadata: CancellationMetadataResponse | None
    available_seats: int | None

    trip_from_stop: StopBriefResponse | None
    trip_to_stop: StopBriefResponse | None
    stops: list[ScheduledTripStopResponse]

    route: RouteResponse
    vehicle: VehicleBriefResponse | None
    driver: DriverBriefResponse | None


class ScheduledTripListResponse(BaseModel):
    items: list[ScheduledTripResponse]
    count: int


class FarePreviewRequest(BaseModel):
    route_id: str = Field(..., min_length=1, max_length=36)
    pickup_stop_id: str = Field(..., min_length=1, max_length=36)
    dropoff_stop_id: str = Field(..., min_length=1, max_length=36)


class FarePreviewResponse(BaseModel):
    route_id: str
    route_name: str
    route_code: str
    has_ac: bool | None
    pickup_stop: StopBriefResponse
    dropoff_stop: StopBriefResponse
    pickup_sequence_no: int
    dropoff_sequence_no: int
    amount: Decimal
    configured_fare_amount: Decimal
    taxable_amount: Decimal
    cgst_rate_percent: Decimal
    cgst_amount: Decimal
    sgst_rate_percent: Decimal
    sgst_amount: Decimal
    igst_rate_percent: Decimal
    igst_amount: Decimal
    total_tax_amount: Decimal
    gst_enabled: bool
    gst_applicable: bool
    gst_inclusive: bool

class LegAvailableSeatsRequest(BaseModel):
    route_id: str = Field(..., min_length=1, max_length=36)
    pickup_stop_id: str = Field(..., min_length=1, max_length=36)
    dropoff_stop_id: str = Field(..., min_length=1, max_length=36)
    seat_number: int | None = Field(default=None, ge=1)


class LegAvailableSeatsResponse(BaseModel):
    scheduled_trip_id: str
    route_id: str
    pickup_stop_id: str
    dropoff_stop_id: str
    pickup_sequence_no: int
    dropoff_sequence_no: int
    seat_capacity: int
    overlapping_active_bookings: int
    available_seats: int
    occupied_seat_numbers: list[int]
    available_seat_numbers: list[int]
    requested_seat_available: bool | None = None
    trip_bookable: bool


class CreateBookingRequest(BaseModel):
    scheduled_trip_id: str = Field(..., min_length=1, max_length=36)
    pickup_stop_id: str = Field(..., min_length=1, max_length=36)
    dropoff_stop_id: str = Field(..., min_length=1, max_length=36)
    seat_number: int = Field(..., ge=1)

class BookingSessionGuestTravellerRequest(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=120)
    phone: str = Field(..., min_length=5, max_length=20)
    email: str | None = Field(default=None, max_length=255)
    relationship_label: str | None = Field(default=None, max_length=80)

    @field_validator("full_name", "phone", "email", "relationship_label")
    @classmethod
    def clean_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("full_name", "phone")
    @classmethod
    def required_text_must_not_be_empty(cls, value: str | None) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("This field cannot be empty.")
        return cleaned

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        return normalize_traveller_phone(value)

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, value: str | None) -> str | None:
        return normalize_traveller_email(value)


class CreateBookingSessionSeatRequest(BaseModel):
    seat_number: int = Field(..., ge=1)
    traveller_profile_id: str | None = Field(default=None, min_length=1, max_length=36)
    traveller: BookingSessionGuestTravellerRequest | None = None

    @model_validator(mode="after")
    def reject_multiple_traveller_sources(self):
        has_profile = self.traveller_profile_id is not None
        has_guest = self.traveller is not None

        if has_profile and has_guest:
            raise ValueError(
                "Provide only one of traveller_profile_id or traveller."
            )

        return self


class CreateBookingSessionRequest(BaseModel):
    scheduled_trip_id: str = Field(..., min_length=1, max_length=36)
    pickup_stop_id: str = Field(..., min_length=1, max_length=36)
    dropoff_stop_id: str = Field(..., min_length=1, max_length=36)
    seats: list[CreateBookingSessionSeatRequest] = Field(..., min_length=1, max_length=10)

    @model_validator(mode="after")
    def seat_numbers_must_be_unique(self):
        seat_numbers = [seat.seat_number for seat in self.seats]
        if len(seat_numbers) != len(set(seat_numbers)):
            raise ValueError("Seat numbers must be unique within one booking session.")
        return self

class BookingSessionSeatRefundResponse(BaseModel):
    id: str
    booking_session_id: str
    booking_id: str
    booking_session_payment_id: str
    owner_user_id: str
    amount: Decimal
    status: str
    razorpay_refund_id: str | None
    failure_reason: str | None
    attempt_count: int
    retry_after: datetime | None
    requested_at: datetime
    processed_at: datetime | None
    created_at: datetime
    updated_at: datetime

class BookingSessionSeatResponse(BaseModel):
    id: str
    booking_session_id: str | None
    passenger_user_id: str
    booked_by_user_id: str | None
    traveller_profile_id: str | None
    traveller_name_snapshot: str | None
    traveller_phone_snapshot: str | None
    traveller_email_snapshot: str | None
    traveller_relationship_label_snapshot: str | None

    scheduled_trip_id: str
    route_id: str
    pickup_stop_id: str
    dropoff_stop_id: str
    seat_number: int
    otp: str | None
    booking_status: str
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
    payment_hold_expires_at: datetime | None
    cancellation_metadata: CancellationMetadataResponse | None
    refund: BookingSessionSeatRefundResponse | None = None
    created_at: datetime
    updated_at: datetime


class BookingSessionPaymentResponse(BaseModel):
    id: str
    booking_session_id: str
    razorpay_order_id: str
    razorpay_payment_id: str | None
    razorpay_refund_id: str | None
    status: str
    effective_status: str
    amount: Decimal
    taxable_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    total_tax_amount: Decimal
    refunded_amount: Decimal
    refund_requested_at: datetime | None
    refund_processed_at: datetime | None
    refund_retry_after: datetime | None
    refund_attempt_count: int | None
    refund_failure_reason: str | None
    created_at: datetime
    updated_at: datetime


class BookingSessionResponse(BaseModel):
    id: str
    owner_user_id: str
    scheduled_trip_id: str
    route_id: str
    pickup_stop_id: str
    dropoff_stop_id: str
    pickup_sequence_no_snapshot: int
    dropoff_sequence_no_snapshot: int
    status: str
    total_fare_amount: Decimal
    total_taxable_amount: Decimal
    total_cgst_amount: Decimal
    total_sgst_amount: Decimal
    total_igst_amount: Decimal
    total_tax_amount: Decimal
    gst_enabled_snapshot: bool
    gst_inclusive_snapshot: bool
    cgst_rate_percent_snapshot: Decimal
    sgst_rate_percent_snapshot: Decimal
    igst_rate_percent_snapshot: Decimal
    payment_hold_expires_at: datetime | None
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    cancellation_metadata: CancellationMetadataResponse | None
    expired_at: datetime | None
    bookings: list[BookingSessionSeatResponse]
    payments: list[BookingSessionPaymentResponse]
    created_at: datetime
    updated_at: datetime

class BookingSessionListResponse(BaseModel):
    items: list[BookingSessionResponse]
    count: int


class BookingSessionCreateResponse(BaseModel):
    message: str
    booking_session: BookingSessionResponse
    payment_order: dict[str, Any]


class BookingSessionRetryPaymentResponse(BaseModel):
    message: str
    booking_session: BookingSessionResponse
    payment_order: dict[str, Any] | None


class BookingSessionMutationResponse(BaseModel):
    message: str
    booking_session: BookingSessionResponse


class VerifyBookingPaymentRequest(BaseModel):
    razorpay_order_id: str = Field(..., min_length=1, max_length=64)
    razorpay_payment_id: str = Field(..., min_length=1, max_length=64)
    razorpay_signature: str = Field(..., min_length=1, max_length=255)

class VerifyBookingSessionPaymentRequest(BaseModel):
    razorpay_order_id: str = Field(..., min_length=1, max_length=64)
    razorpay_payment_id: str = Field(..., min_length=1, max_length=64)
    razorpay_signature: str = Field(..., min_length=1, max_length=255)

class BookingPaymentResponse(BaseModel):
    id: str
    booking_id: str
    razorpay_order_id: str
    razorpay_payment_id: str | None
    status: str
    amount: Decimal
    taxable_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    total_tax_amount: Decimal
    created_at: datetime
    updated_at: datetime


class BookingRatingResponse(BaseModel):
    id: str
    booking_id: str
    trip_rating: int
    driver_rating: int
    review_text: str | None
    created_at: datetime
    updated_at: datetime


class TripBookingResponse(BaseModel):
    id: str
    passenger_user_id: str
    booking_session_id: str | None
    booked_by_user_id: str | None
    traveller_profile_id: str | None
    traveller_name_snapshot: str | None
    traveller_phone_snapshot: str | None
    traveller_email_snapshot: str | None
    traveller_relationship_label_snapshot: str | None
    scheduled_trip_id: str
    route_id: str
    pickup_stop_id: str
    dropoff_stop_id: str
    seat_number: int
    otp: str | None
    booking_status: str
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
    transfer_status: str
    transfer_ready_at: datetime | None
    transfer_processed_at: datetime | None
    boarded_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    cancellation_metadata: CancellationMetadataResponse | None
    pickup_stop: StopBriefResponse
    dropoff_stop: StopBriefResponse
    payment_hold_expires_at: datetime | None
    payments: list[BookingPaymentResponse]
    rating: BookingRatingResponse | None
    created_at: datetime
    updated_at: datetime

class BookingDetailResponse(BaseModel):
    id: str
    passenger_user_id: str
    booking_session_id: str | None
    booked_by_user_id: str | None
    traveller_profile_id: str | None
    traveller_name_snapshot: str | None
    traveller_phone_snapshot: str | None
    traveller_email_snapshot: str | None
    traveller_relationship_label_snapshot: str | None
    scheduled_trip_id: str
    route_id: str
    pickup_stop_id: str
    dropoff_stop_id: str
    seat_number: int
    otp: str | None
    booking_status: str
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
    payment_hold_expires_at: datetime | None
    commission_percent_snapshot: Decimal
    commission_amount: Decimal
    driver_payout_amount: Decimal
    transfer_status: str
    transfer_ready_at: datetime | None
    transfer_processed_at: datetime | None
    boarded_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    cancellation_metadata: CancellationMetadataResponse | None
    pickup_stop: StopBriefResponse
    dropoff_stop: StopBriefResponse
    scheduled_trip: ScheduledTripResponse
    payments: list[BookingPaymentResponse]
    rating: BookingRatingResponse | None
    created_at: datetime
    updated_at: datetime


class PassengerInvoicePartyResponse(BaseModel):
    user_id: str
    full_name: str | None
    email: str | None
    traveller_name: str | None
    traveller_phone: str | None
    traveller_email: str | None
    traveller_relationship_label: str | None


class PassengerInvoiceSupplierResponse(BaseModel):
    gstin: str | None
    legal_name: str | None
    trade_name: str | None
    registered_address: str | None
    state_name: str | None
    state_code: str | None
    postal_code: str | None


class PassengerInvoiceServiceResponse(BaseModel):
    sac_code: str | None
    description: str | None
    quantity: int
    unit: str


class PassengerInvoicePlaceOfSupplyResponse(BaseModel):
    name: str | None
    state_code: str | None


class PassengerInvoiceComplianceResponse(BaseModel):
    reverse_charge_applicable: bool
    digital_signature: str | None
    irn: str | None
    acknowledgement_number: str | None
    acknowledgement_date: datetime | None
    signed_qr_code: str | None


class PassengerInvoiceTripResponse(BaseModel):
    scheduled_trip_id: str
    route_id: str
    seat_number: int
    route_name: str | None
    route_code: str | None
    is_ac: bool
    pickup_stop: StopBriefResponse
    dropoff_stop: StopBriefResponse
    planned_start_at: datetime | None
    planned_end_at: datetime | None
    actual_start_at: datetime | None
    actual_end_at: datetime | None
    completed_at: datetime | None


class PassengerInvoiceBreakdownResponse(BaseModel):
    total_booking_amount: Decimal
    divisor_used: Decimal
    taxable_value: Decimal
    cgst_rate_percent: Decimal
    cgst_amount: Decimal
    sgst_rate_percent: Decimal
    sgst_amount: Decimal
    igst_rate_percent: Decimal
    igst_amount: Decimal
    total_tax_amount: Decimal
    gst_inclusive: bool
    recomputed_total_amount: Decimal
    rounding_adjustment: Decimal


class PassengerInvoiceResponse(BaseModel):
    invoice_number: str
    booking_id: str
    booking_created_at: datetime
    invoice_generated_at: datetime
    invoice_status: str
    currency: str
    supplier_gstin: str | None
    supplier: PassengerInvoiceSupplierResponse
    service: PassengerInvoiceServiceResponse
    place_of_supply: PassengerInvoicePlaceOfSupplyResponse
    compliance: PassengerInvoiceComplianceResponse
    passenger: PassengerInvoicePartyResponse
    trip: PassengerInvoiceTripResponse
    breakdown: PassengerInvoiceBreakdownResponse
    payment: BookingPaymentResponse | None

    
class CurrentTripBookingResponse(BaseModel):
    id: str
    passenger_user_id: str
    scheduled_trip_id: str
    route_id: str
    pickup_stop_id: str
    dropoff_stop_id: str
    seat_number: int
    otp: str | None
    booking_status: str
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
    payment_hold_expires_at: datetime | None
    boarded_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    cancellation_metadata: CancellationMetadataResponse | None
    pickup_stop: StopBriefResponse
    dropoff_stop: StopBriefResponse
    scheduled_trip: ScheduledTripResponse
    created_at: datetime
    updated_at: datetime


class CurrentTripBookingListResponse(BaseModel):
    items: list[CurrentTripBookingResponse]
    count: int

class CurrentTripProgressStopResponse(BaseModel):
    stop: StopBriefResponse
    event_status: str
    actual_time: datetime | None


class CurrentTripSegmentStopResponse(BaseModel):
    route_stop_id: str
    sequence_no: int
    assume_time_diff_minutes: int | None
    is_pickup_stop: bool
    is_dropoff_stop: bool
    stop_status: str
    planned_time_at_stop: datetime
    estimated_time_at_stop: datetime | None
    actual_arrival_time: datetime | None
    actual_departure_time: datetime | None
    stop: StopBriefResponse

class CurrentTripStatusResponse(BaseModel):
    booking_id: str
    booking_session_id: str | None
    passenger_user_id: str
    booked_by_user_id: str | None

    traveller_profile_id: str | None
    traveller_name_snapshot: str | None
    traveller_phone_snapshot: str | None
    traveller_email_snapshot: str | None
    traveller_relationship_label_snapshot: str | None

    seat_number: int
    scheduled_trip_id: str
    otp: str | None
    booking_status: str
    trip_status: str

    boarding_scan_completed: bool
    drop_scan_completed: bool
    trip_completed: bool

    pickup_stop: StopBriefResponse
    dropoff_stop: StopBriefResponse
    trip_from_stop: StopBriefResponse | None
    trip_to_stop: StopBriefResponse | None

    current_progress_stop: CurrentTripProgressStopResponse | None
    segment_stops: list[CurrentTripSegmentStopResponse]


class CurrentTripLiveLocationResponse(BaseModel):
    booking_id: str
    booking_session_id: str | None
    passenger_user_id: str
    booked_by_user_id: str | None

    traveller_profile_id: str | None
    traveller_name_snapshot: str | None
    traveller_phone_snapshot: str | None
    traveller_email_snapshot: str | None
    traveller_relationship_label_snapshot: str | None

    scheduled_trip_id: str
    booking_status: str
    trip_status: str
    tracking_active: bool
    last_lat: Decimal | None
    last_lng: Decimal | None
    planned_start_at: datetime
    completed_at: datetime | None
    actual_end_at: datetime | None
    updated_at: datetime



class CurrentTripBookingSessionResponse(BaseModel):
    booking_session_id: str
    owner_user_id: str
    scheduled_trip_id: str
    route_id: str
    pickup_stop_id: str
    dropoff_stop_id: str
    status: str
    total_fare_amount: Decimal
    total_taxable_amount: Decimal
    total_cgst_amount: Decimal
    total_sgst_amount: Decimal
    total_igst_amount: Decimal
    total_tax_amount: Decimal
    gst_enabled_snapshot: bool
    gst_inclusive_snapshot: bool
    cgst_rate_percent_snapshot: Decimal
    sgst_rate_percent_snapshot: Decimal
    igst_rate_percent_snapshot: Decimal
    payment_hold_expires_at: datetime | None
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    expired_at: datetime | None
    bookings: list[CurrentTripBookingResponse]
    booking_count: int
    created_at: datetime
    updated_at: datetime


class CurrentTripBookingSessionListResponse(BaseModel):
    items: list[CurrentTripBookingSessionResponse]
    count: int


class BookingSessionCurrentTripStatusResponse(BaseModel):
    booking_session_id: str
    items: list[CurrentTripStatusResponse]
    count: int


class BookingSessionCurrentTripLiveLocationResponse(BaseModel):
    booking_session_id: str
    items: list[CurrentTripLiveLocationResponse]
    count: int


class SupportTicketResponse(BaseModel):
    id: str
    user_id: str
    subject: str
    description: str
    attachment_path: str | None
    status: str
    resolved_at: datetime | None
    rejection_reason: str | None
    created_at: datetime
    updated_at: datetime


class SupportTicketListResponse(BaseModel):
    items: list[SupportTicketResponse]
    count: int


class SupportTicketCreateResponse(BaseModel):
    message: str
    ticket: SupportTicketResponse
    
class BookingListResponse(BaseModel):
    items: list[TripBookingResponse]
    count: int


class BookingMutationResponse(BaseModel):
    message: str
    booking: TripBookingResponse


class BookingCreateResponse(BaseModel):
    message: str
    booking: TripBookingResponse
    payment_order: dict[str, Any] | None = None


class BookingQRResponse(BaseModel):
    booking_id: str
    qr_token: str
    payload: dict[str, Any]


class CreateBookingRatingRequest(BaseModel):
    trip_rating: int = Field(..., ge=1, le=5)
    driver_rating: int = Field(..., ge=1, le=5)
    review_text: str | None = Field(default=None, max_length=2000)

    @field_validator("review_text")
    @classmethod
    def validate_review_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class BookingRatingMutationResponse(BaseModel):
    message: str
    rating: BookingRatingResponse


class PassengerTransactionPaymentDetailsResponse(BaseModel):
    id: str
    source: str
    booking_id: str | None
    booking_session_id: str | None
    razorpay_order_id: str
    razorpay_payment_id: str | None
    status: str
    effective_status: str
    amount: Decimal
    taxable_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    total_tax_amount: Decimal
    razorpay_refund_id: str | None
    refunded_amount: Decimal
    refund_requested_at: datetime | None
    refund_processed_at: datetime | None
    refund_failure_reason: str | None
    created_at: datetime
    updated_at: datetime


class PassengerTransactionFailureResponse(BaseModel):
    code: str
    message: str
    gateway: str
    gateway_order_id: str
    gateway_payment_id: str | None
    provider_reason: str | None
    recorded_at: datetime


class PassengerTransactionResponse(BaseModel):
    payment_source: str
    payment_id: str
    booking_id: str | None
    booking_session_id: str | None
    booking_ids: list[str]
    seat_number: int | None
    scheduled_trip_id: str | None
    route_id: str | None
    booking_status: str | None
    payment_status: str
    effective_status: str
    amount: Decimal
    taxable_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    total_tax_amount: Decimal
    razorpay_order_id: str
    razorpay_payment_id: str | None
    pickup_stop: StopBriefResponse | None
    dropoff_stop: StopBriefResponse | None
    route_name: str | None
    route_code: str | None
    planned_start_at: datetime | None
    planned_end_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    cancellation_metadata: CancellationMetadataResponse | None
    created_at: datetime
    updated_at: datetime
    transaction: PassengerTransactionPaymentDetailsResponse
    booking: BookingDetailResponse | None
    bookings: list[BookingDetailResponse] | None
    invoice: PassengerInvoiceResponse | None
    invoices: list[PassengerInvoiceResponse] | None
    invoice_unavailable_reason: str | None
    failure: PassengerTransactionFailureResponse | None


class PassengerTransactionHistoryResponse(BaseModel):
    items: list[PassengerTransactionResponse]
    count: int

class RouteTripDiscoveryTripResponse(BaseModel):
    scheduled_trip_id: str
    route_id: str
    status: str
    planned_start_at: datetime
    planned_end_at: datetime

    pickup_stop: StopBriefResponse
    dropoff_stop: StopBriefResponse
    pickup_sequence_no: int
    dropoff_sequence_no: int
    pickup_planned_time: datetime
    dropoff_planned_time: datetime

    seat_capacity: int
    overlapping_active_bookings: int
    available_seats: int
    occupied_seat_numbers: list[int]
    available_seat_numbers: list[int]
    trip_bookable: bool

    vehicle: VehicleBriefResponse | None
    driver: DriverBriefResponse | None


class RouteTripDiscoveryOptionResponse(BaseModel):
    route: RouteResponse
    pickup_stop: StopBriefResponse
    dropoff_stop: StopBriefResponse
    pickup_sequence_no: int
    dropoff_sequence_no: int
    fare_amount: Decimal
    configured_fare_amount: Decimal
    taxable_amount: Decimal
    cgst_rate_percent: Decimal
    cgst_amount: Decimal
    sgst_rate_percent: Decimal
    sgst_amount: Decimal
    igst_rate_percent: Decimal
    igst_amount: Decimal
    total_tax_amount: Decimal
    gst_enabled: bool
    gst_applicable: bool
    gst_inclusive: bool

    upcoming_scheduled_trips: list[RouteTripDiscoveryTripResponse]
    upcoming_scheduled_trip_count: int


class RouteTripDiscoveryResponse(BaseModel):
    from_stop_id: str
    to_stop_id: str
    from_time: datetime | None
    to_time: datetime | None
    items: list[RouteTripDiscoveryOptionResponse]
    count: int

# ============================================================
# Passenger RFID
# ============================================================


class PassengerRFIDCardResponse(BaseModel):
    id: str
    card_uid_masked: str | None
    inventory_status: str
    authorization_status: str
    assigned_at: datetime | None


class PassengerRFIDAccountResponse(BaseModel):
    id: str
    card_id: str
    current_balance: Decimal
    held_balance: Decimal
    available_balance: Decimal
    currency: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class PassengerRFIDAssignmentResponse(BaseModel):
    id: str
    card_id: str
    passenger_user_id: str
    assigned_at: datetime
    reason: str | None
    created_at: datetime
    updated_at: datetime


class PassengerRFIDMeResponse(BaseModel):
    has_assigned_card: bool
    card: PassengerRFIDCardResponse | None
    account: PassengerRFIDAccountResponse | None
    assignment: PassengerRFIDAssignmentResponse | None


class PassengerRFIDLedgerEntryResponse(BaseModel):
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
    note: str | None
    created_at: datetime


class PassengerRFIDLedgerListResponse(BaseModel):
    items: list[PassengerRFIDLedgerEntryResponse]
    count: int


class PassengerRFIDRechargeResponse(BaseModel):
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
    paid_at: datetime | None
    credited_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PassengerRFIDRechargeListResponse(BaseModel):
    items: list[PassengerRFIDRechargeResponse]
    count: int


class PassengerRFIDRideResponse(BaseModel):
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
    boarded_at: datetime
    board_lat: Decimal | None
    board_lng: Decimal | None

    dropoff_stop_id: str | None
    dropoff_sequence_no: int | None
    dropped_at: datetime | None
    drop_lat: Decimal | None
    drop_lng: Decimal | None

    status: str
    hold_amount: Decimal
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
    fare_reversed_amount: Decimal
    fare_net_amount: Decimal

    transfer_status: str
    transfer_ready_at: datetime | None
    transfer_processed_at: datetime | None

    pickup_stop: StopBriefResponse | None = None
    dropoff_stop: StopBriefResponse | None = None

    created_at: datetime
    updated_at: datetime


class PassengerRFIDRideListResponse(BaseModel):
    items: list[PassengerRFIDRideResponse]
    count: int


class PassengerRFIDRideDetailResponse(BaseModel):
    ride: PassengerRFIDRideResponse
    ledger_entries: list[PassengerRFIDLedgerEntryResponse]
    recharges: list[PassengerRFIDRechargeResponse]

class PassengerRFIDRechargeCreateOrderRequest(BaseModel):
    amount: Decimal = Field(..., gt=Decimal("0.00"))


class PassengerRFIDRechargeCreateOrderResponse(BaseModel):
    message: str
    recharge: PassengerRFIDRechargeResponse
    payment_order: dict[str, Any]


class PassengerRFIDRechargeVerifyPaymentRequest(BaseModel):
    razorpay_order_id: str = Field(..., min_length=1, max_length=64)
    razorpay_payment_id: str = Field(..., min_length=1, max_length=64)
    razorpay_signature: str = Field(..., min_length=1, max_length=255)


class PassengerRFIDRechargeMutationResponse(BaseModel):
    message: str
    recharge: PassengerRFIDRechargeResponse
    account: PassengerRFIDAccountResponse

class PassengerRFIDSummaryResponse(BaseModel):
    me: PassengerRFIDMeResponse
    current_ride: PassengerRFIDRideResponse | None

    recent_ledger_entries: list[PassengerRFIDLedgerEntryResponse]
    recent_recharges: list[PassengerRFIDRechargeResponse]
    recent_rides: list[PassengerRFIDRideResponse]

    recent_ledger_entry_count: int
    recent_recharge_count: int
    recent_ride_count: int

class PassengerRFIDInProgressTripResponse(BaseModel):
    scheduled_trip_id: str
    route_id: str
    status: str

    planned_start_at: datetime
    planned_end_at: datetime
    actual_start_at: datetime | None
    actual_end_at: datetime | None

    pickup_stop: StopBriefResponse
    dropoff_stop: StopBriefResponse
    current_stop: StopBriefResponse | None

    pickup_sequence_no: int
    dropoff_sequence_no: int
    current_sequence_no: int | None

    selected_fare_amount: Decimal
    selected_taxable_amount: Decimal
    selected_cgst_amount: Decimal
    selected_sgst_amount: Decimal
    selected_igst_amount: Decimal
    selected_total_tax_amount: Decimal
    required_hold_amount: Decimal | None

    available_balance: Decimal
    balance_shortfall: Decimal
    minimum_recharge_amount: Decimal

    rfid_seat_policy: str
    rfid_physical_seat_check_required: bool

    rfid_reserved_seat_count: int
    rfid_occupied_seat_count: int
    rfid_available_seat_count: int

    rfid_seat_available: bool
    rfid_balance_sufficient: bool
    rfid_can_avail: bool
    rfid_can_board_now: bool
    rfid_unavailable_reason: str | None

    vehicle: VehicleBriefResponse | None
    driver: DriverBriefResponse | None


class PassengerRFIDRouteTripDiscoveryOptionResponse(BaseModel):
    route: RouteResponse
    pickup_stop: StopBriefResponse
    dropoff_stop: StopBriefResponse
    pickup_sequence_no: int
    dropoff_sequence_no: int
    fare_amount: Decimal
    configured_fare_amount: Decimal
    taxable_amount: Decimal
    cgst_rate_percent: Decimal
    cgst_amount: Decimal
    sgst_rate_percent: Decimal
    sgst_amount: Decimal
    igst_rate_percent: Decimal
    igst_amount: Decimal
    total_tax_amount: Decimal
    gst_enabled: bool
    gst_applicable: bool
    gst_inclusive: bool

    upcoming_scheduled_trips: list[RouteTripDiscoveryTripResponse]
    upcoming_scheduled_trip_count: int

    rfid_in_progress_trips: list[PassengerRFIDInProgressTripResponse]
    rfid_in_progress_trip_count: int

class PassengerRFIDRouteTripDiscoveryResponse(BaseModel):
    from_stop_id: str
    to_stop_id: str
    from_time: datetime | None
    to_time: datetime | None

    has_active_rfid: bool
    rfid_card_id: str
    rfid_account_id: str
    rfid_available_balance: Decimal

    items: list[PassengerRFIDRouteTripDiscoveryOptionResponse]
    count: int
