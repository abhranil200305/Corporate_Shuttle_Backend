from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator


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


class StopBriefResponse(BaseModel):
    id: str
    name: str
    lat: Decimal
    lng: Decimal
    radius_meters: int
    is_active: bool


class RouteStopResponse(BaseModel):
    route_stop_id: str
    sequence_no: int
    boarding_allowed: bool
    deboarding_allowed: bool
    stop: StopBriefResponse


class RouteResponse(BaseModel):
    id: str
    name: str
    code: str
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
    has_ac: bool


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
    available_seats: int
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
    pickup_stop: StopBriefResponse
    dropoff_stop: StopBriefResponse
    pickup_sequence_no: int
    dropoff_sequence_no: int
    amount: Decimal


class CreateBookingRequest(BaseModel):
    scheduled_trip_id: str = Field(..., min_length=1, max_length=36)
    pickup_stop_id: str = Field(..., min_length=1, max_length=36)
    dropoff_stop_id: str = Field(..., min_length=1, max_length=36)


class VerifyBookingPaymentRequest(BaseModel):
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
    scheduled_trip_id: str
    route_id: str
    pickup_stop_id: str
    dropoff_stop_id: str
    booking_status: str
    fare_amount: Decimal
    commission_percent_snapshot: Decimal
    commission_amount: Decimal
    driver_payout_amount: Decimal
    transfer_status: str
    transfer_ready_at: datetime | None
    transfer_processed_at: datetime | None
    boarded_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    pickup_stop: StopBriefResponse
    dropoff_stop: StopBriefResponse
    payment_hold_expires_at: datetime | None
    payments: list[BookingPaymentResponse]
    rating: BookingRatingResponse | None
    created_at: datetime
    updated_at: datetime


class BookingListResponse(BaseModel):
    items: list[TripBookingResponse]
    count: int


class BookingMutationResponse(BaseModel):
    message: str
    booking: TripBookingResponse


class BookingCreateResponse(BaseModel):
    message: str
    booking: TripBookingResponse
    payment_order: dict[str, Any]


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