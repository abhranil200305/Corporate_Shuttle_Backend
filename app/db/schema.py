# app/db/schema.py
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal

# from app.db.database import Base
# app/db/schema.py
from sqlalchemy.orm import DeclarativeBase


# ---------------------------
# Base for SQLAlchemy models
# ---------------------------
class Base(DeclarativeBase):
    """All SQLAlchemy models inherit from this Base"""

    pass


from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

# ============================================================
# base / helpers
# ============================================================


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enum_type(enum_cls: type[enum.Enum], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


class UUIDPKMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


# ============================================================
# enums
# ============================================================


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    DRIVER = "driver"
    PASSENGER = "passenger"


class OTPPurpose(str, enum.Enum):
    LOGIN = "login"
    SIGNUP = "signup"


class DriverVerificationStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class DriverLifecycleStatus(str, enum.Enum):
    ACTIVE = "active"
    BANNED = "banned"
    DELETED = "deleted"


class VehicleVerificationStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class LinkedAccountStatus(str, enum.Enum):
    NOT_CREATED = "not_created"
    CREATED = "created"
    UNDER_REVIEW = "under_review"
    NEEDS_CLARIFICATION = "needs_clarification"
    ACTIVE = "active"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    DELETED = "deleted"


class RouteProductStatus(str, enum.Enum):
    NOT_REQUESTED = "not_requested"
    REQUESTED = "requested"
    UNDER_REVIEW = "under_review"
    NEEDS_CLARIFICATION = "needs_clarification"
    ACTIVATED = "activated"
    SUSPENDED = "suspended"


class ScheduledTripStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    PREMATURE_END = "premature_end"
    PREMATURED_END_REQUEST = "premature_end_requested"

class BookingStatus(str, enum.Enum):
    PENDING_PAYMENT = "pending_payment"
    BOOKED = "booked"
    BOARDED = "boarded"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    MISSED = "missed"


class TransferStatus(str, enum.Enum):
    NOT_READY = "not_ready"
    READY = "ready"
    TRANSFERRED = "transferred"
    WITHHELD = "withheld"
    REVERSED = "reversed"
    FAILED = "failed"


class BookingPaymentStatus(str, enum.Enum):
    CREATED = "created"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"


class BookingTransferStatus(str, enum.Enum):
    CREATED = "created"
    PROCESSED = "processed"
    FAILED = "failed"
    REVERSED = "reversed"


class ScanType(str, enum.Enum):
    BOARD = "board"
    DROP = "drop"


class SupportStatus(str, enum.Enum):
    PENDING = "pending"
    RESOLVED = "resolved"
    REJECTED = "rejected"

class VehicleInspectionStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class PayoutAdjustmentType(str, enum.Enum):
    FINE = "fine"
    DEDUCTION = "deduction"

class PayoutAdjustmentDecision(str, enum.Enum):
    PENDING = "pending"
    INCLUDED = "included"
    EXCLUDED = "excluded"

class EmergencyStopRequestStatus(str, enum.Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    PENDING = "pending"

class VehicleOwnershipType(str, enum.Enum):
    SELF = "self"
    RENTED = "rented"

# ============================================================
# auth / users
# ============================================================


class User(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    role: Mapped[UserRole] = mapped_column(
        enum_type(UserRole, "user_role"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    sessions: Mapped[list["UserSession"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    passenger_profile: Mapped["PassengerProfile | None"] = relationship(
        back_populates="user",
        foreign_keys="PassengerProfile.user_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )

    driver_profile: Mapped["DriverProfile | None"] = relationship(
        back_populates="user",
        foreign_keys="DriverProfile.user_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )

    payout_details: Mapped["DriverPayoutDetails | None"] = relationship(
        back_populates="driver",
        foreign_keys="DriverPayoutDetails.driver_user_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )

    vehicle: Mapped["Vehicle | None"] = relationship(
        back_populates="driver",
        foreign_keys="Vehicle.driver_user_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )

    reviewed_driver_profiles: Mapped[list["DriverProfile"]] = relationship(
        back_populates="reviewed_by_admin",
        foreign_keys="DriverProfile.reviewed_by_admin_id",
        passive_deletes=True,
    )

    reviewed_vehicles: Mapped[list["Vehicle"]] = relationship(
        back_populates="reviewed_by_admin",
        foreign_keys="Vehicle.reviewed_by_admin_id",
        passive_deletes=True,
    )

    driven_trips: Mapped[list["ScheduledTrip"]] = relationship(
        back_populates="driver",
        foreign_keys="ScheduledTrip.driver_user_id",
        passive_deletes=True,
    )

    passenger_bookings: Mapped[list["TripBooking"]] = relationship(
        back_populates="passenger",
        foreign_keys="TripBooking.passenger_user_id",
        passive_deletes=True,
    )

    created_transfers_for_driver: Mapped[list["BookingTransfer"]] = relationship(
        back_populates="driver",
        foreign_keys="BookingTransfer.driver_user_id",
        passive_deletes=True,
    )

    created_payout_adjustments: Mapped[list["PayoutAdjustment"]] = relationship(
        back_populates="created_by_admin",
        foreign_keys="PayoutAdjustment.created_by_admin_id",
        passive_deletes=True,
    )

    decided_payout_adjustments: Mapped[list["PayoutAdjustment"]] = relationship(
        back_populates="decided_by_admin",
        foreign_keys="PayoutAdjustment.decided_by_admin_id",
        passive_deletes=True,
    )

    applied_payout_adjustment_applications: Mapped[list["PayoutAdjustmentApplication"]] = relationship(
        back_populates="applied_by_admin",
        foreign_keys="PayoutAdjustmentApplication.applied_by_admin_id",
        passive_deletes=True,
    )

    scan_events_as_driver: Mapped[list["TripScanEvent"]] = relationship(
        back_populates="driver",
        foreign_keys="TripScanEvent.driver_user_id",
        passive_deletes=True,
    )

    ratings_given: Mapped[list["BookingRating"]] = relationship(
        back_populates="passenger",
        foreign_keys="BookingRating.passenger_user_id",
        passive_deletes=True,
    )

    ratings_received_as_driver: Mapped[list["BookingRating"]] = relationship(
        back_populates="driver",
        foreign_keys="BookingRating.driver_user_id",
        passive_deletes=True,
    )

    notifications: Mapped[list["UserNotification"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (CheckConstraint("email <> ''", name="ck_users_email_nonempty"),)


class OTPRequest(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "otp_requests"

    email: Mapped[str] = mapped_column(String(255), nullable=False)
    otp_code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[OTPPurpose] = mapped_column(
        enum_type(OTPPurpose, "otp_purpose"),
        nullable=False,
        default=OTPPurpose.LOGIN,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("ix_otp_requests_email_expires_at", "email", "expires_at"),)


class UserSession(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "user_sessions"

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped["User"] = relationship(
        back_populates="sessions",
        foreign_keys=[user_id],
    )

    __table_args__ = (
        Index("ix_user_sessions_user_id_expires_at", "user_id", "expires_at"),
    )


class UserNotification(UUIDPKMixin, Base):
    __tablename__ = "user_notifications"

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )

    user: Mapped["User"] = relationship(
        back_populates="notifications",
        foreign_keys=[user_id],
    )

    __table_args__ = (
        CheckConstraint("title <> ''", name="ck_user_notifications_title_nonempty"),
        CheckConstraint("message <> ''", name="ck_user_notifications_message_nonempty"),
        Index("ix_user_notifications_user_created", "user_id", "created_at"),
        Index("ix_user_notifications_user_read", "user_id", "read_at"),
    )


# ============================================================
# driver / payout / vehicle
# ============================================================
class PassengerProfile(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "passenger_profiles"

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    profile_picture_path: Mapped[str | None] = mapped_column(String(255), nullable=True)

    user: Mapped["User"] = relationship(
        back_populates="passenger_profile",
        foreign_keys=[user_id],
    )

    __table_args__ = (
        CheckConstraint(
            "full_name <> ''", name="ck_passenger_profiles_full_name_nonempty"
        ),
    )


class DriverProfile(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "driver_profiles"

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    profile_picture_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    aadhaar_number: Mapped[str | None] = mapped_column(String(12), nullable=True)
    pan_number: Mapped[str | None] = mapped_column(String(10), nullable=True)
    driving_license_number: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    aadhaar_file_path: Mapped[str] = mapped_column(String(255), nullable=True)
    pan_file_path: Mapped[str] = mapped_column(String(255), nullable=True)
    driving_license_file_path: Mapped[str] = mapped_column(String(255), nullable=True)
    bank_account_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ifsc_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    passbook_file_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    residential_street_line_1: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    residential_street_line_2: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    residential_city: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )
    residential_state: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )
    residential_postal_code: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    residential_country: Mapped[str | None] = mapped_column(
        String(2), nullable=True
    )

    verification_status: Mapped[DriverVerificationStatus] = mapped_column(
        enum_type(DriverVerificationStatus, "driver_verification_status"),
        nullable=False,
        default=DriverVerificationStatus.DRAFT,
    )
    lifecycle_status: Mapped[DriverLifecycleStatus] = mapped_column(
        enum_type(DriverLifecycleStatus, "driver_lifecycle_status"),
        nullable=False,
        default=DriverLifecycleStatus.ACTIVE,
    )
    duration_payable_days: Mapped[int] = mapped_column(
    Integer,
    nullable=True,
    )

    verification_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    reviewed_by_admin_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship(
        back_populates="driver_profile",
        foreign_keys=[user_id],
    )
    reviewed_by_admin: Mapped["User | None"] = relationship(
        back_populates="reviewed_driver_profiles",
        foreign_keys=[reviewed_by_admin_id],
    )

    __table_args__ = (
        CheckConstraint(
            "full_name <> ''", name="ck_driver_profiles_full_name_nonempty"
        ),
        CheckConstraint("phone <> ''", name="ck_driver_profiles_phone_nonempty"),
    )


class DriverPayoutDetails(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "driver_payout_details"

    driver_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    account_holder_name: Mapped[str] = mapped_column(String(120), nullable=False)
    bank_account_number: Mapped[str] = mapped_column(String(64), nullable=False)
    ifsc_code: Mapped[str] = mapped_column(String(20), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(20), nullable=False)

    razorpay_linked_account_id: Mapped[str | None] = mapped_column(
        String(64),
        unique=True,
        nullable=True,
    )
    razorpay_stakeholder_id: Mapped[str | None] = mapped_column(
        String(64),
        unique=True,
        nullable=True,
    )
    razorpay_route_product_id: Mapped[str | None] = mapped_column(
        String(64),
        unique=True,
        nullable=True,
    )
    linked_account_status: Mapped[LinkedAccountStatus] = mapped_column(
        enum_type(LinkedAccountStatus, "linked_account_status"),
        nullable=False,
        default=LinkedAccountStatus.NOT_CREATED,
    )
    route_product_status: Mapped[RouteProductStatus] = mapped_column(
        enum_type(RouteProductStatus, "route_product_status"),
        nullable=True,
        default=RouteProductStatus.NOT_REQUESTED,
    )
    route_product_requirements_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    provider_onboarding_last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    is_payout_eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    driver: Mapped["User"] = relationship(
        back_populates="payout_details",
        foreign_keys=[driver_user_id],
    )

    __table_args__ = (
        CheckConstraint(
            "account_holder_name <> ''",
            name="ck_driver_payout_account_holder_name_nonempty",
        ),
        CheckConstraint(
            "bank_account_number <> ''",
            name="ck_driver_payout_bank_account_nonempty",
        ),
        CheckConstraint("ifsc_code <> ''", name="ck_driver_payout_ifsc_nonempty"),
        CheckConstraint("phone_number <> ''", name="ck_driver_payout_phone_nonempty"),
    )


class Vehicle(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "vehicles"

    driver_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    registration_number: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False
    )
    registration_valid_till: Mapped[datetime] = mapped_column(
    DateTime(timezone=True),
    nullable=True,
    )
        # vehicle images
    front_photo_file_path: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    interior_photo_file_path: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    left_side_file_path: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    right_side_file_path: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    # documents
    insurance_document: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    pollution_document: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    owner_aadhaar_card: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    # owner info
    owner_name: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
    )
    vehicle_name: Mapped[str] = mapped_column(String(80), nullable=False)
    vehicle_model: Mapped[str] = mapped_column(String(80), nullable=False)
    color: Mapped[str] = mapped_column(String(40), nullable=False)
    seat_count: Mapped[int] = mapped_column(Integer, nullable=False)
    has_ac: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    rc_file_path: Mapped[str] = mapped_column(String(255), nullable=False)
    rear_photo_file_path: Mapped[str] = mapped_column(String(255), nullable=False)

    verification_status: Mapped[VehicleVerificationStatus] = mapped_column(
        enum_type(VehicleVerificationStatus, "vehicle_verification_status"),
        nullable=False,
        default=VehicleVerificationStatus.DRAFT,
    )
    ownership_type: Mapped[VehicleOwnershipType] = mapped_column(
    enum_type(VehicleOwnershipType, "vehicle_ownership_type"),
    nullable=True,
    )

    authentication_file_path: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    verification_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    
    inspection_created_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True),
    nullable=True,
    )
    inspection_reviewed_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True),
    nullable=True,
    )
    inspection_reason: Mapped[str | None] = mapped_column(
    Text,
    nullable=True,
    )

    inspection_status: Mapped[VehicleInspectionStatus | None] = mapped_column(
        enum_type(VehicleInspectionStatus, "vehicle_inspection_status"),
        nullable=True,
    )
    reviewed_by_admin_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    driver: Mapped["User"] = relationship(
        back_populates="vehicle",
        foreign_keys=[driver_user_id],
    )
    reviewed_by_admin: Mapped["User | None"] = relationship(
        back_populates="reviewed_vehicles",
        foreign_keys=[reviewed_by_admin_id],
    )
    scheduled_trips: Mapped[list["ScheduledTrip"]] = relationship(
        back_populates="vehicle",
        foreign_keys="ScheduledTrip.vehicle_id",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("seat_count > 0", name="ck_vehicles_seat_count_positive"),
        CheckConstraint(
            "registration_number <> ''",
            name="ck_vehicles_registration_number_nonempty",
        ),
        CheckConstraint("vehicle_name <> ''", name="ck_vehicles_vehicle_name_nonempty"),
        CheckConstraint(
            "vehicle_model <> ''", name="ck_vehicles_vehicle_model_nonempty"
        ),
        CheckConstraint("color <> ''", name="ck_vehicles_color_nonempty"),
    )


# ============================================================
# stops / routes / fares
# ============================================================


class Stop(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "stops"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    lat: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    lng: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    radius_meters: Mapped[int] = mapped_column(Integer, nullable=False, default=250)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    route_stops: Mapped[list["RouteStop"]] = relationship(
        back_populates="stop",
        passive_deletes=True,
    )

    pickup_fares: Mapped[list["RouteFare"]] = relationship(
        back_populates="pickup_stop",
        foreign_keys="RouteFare.pickup_stop_id",
        passive_deletes=True,
    )
    dropoff_fares: Mapped[list["RouteFare"]] = relationship(
        back_populates="dropoff_stop",
        foreign_keys="RouteFare.dropoff_stop_id",
        passive_deletes=True,
    )

    started_trips_here: Mapped[list["ScheduledTrip"]] = relationship(
        back_populates="started_near_stop",
        foreign_keys="ScheduledTrip.started_near_stop_id",
        passive_deletes=True,
    )
    ended_trips_here: Mapped[list["ScheduledTrip"]] = relationship(
        back_populates="ended_near_stop",
        foreign_keys="ScheduledTrip.ended_near_stop_id",
        passive_deletes=True,
    )

    pickup_bookings: Mapped[list["TripBooking"]] = relationship(
        back_populates="pickup_stop",
        foreign_keys="TripBooking.pickup_stop_id",
        passive_deletes=True,
    )
    dropoff_bookings: Mapped[list["TripBooking"]] = relationship(
        back_populates="dropoff_stop",
        foreign_keys="TripBooking.dropoff_stop_id",
        passive_deletes=True,
    )
    boarded_bookings_here: Mapped[list["TripBooking"]] = relationship(
        back_populates="boarded_near_stop",
        foreign_keys="TripBooking.boarded_near_stop_id",
        passive_deletes=True,
    )
    completed_bookings_here: Mapped[list["TripBooking"]] = relationship(
        back_populates="completed_near_stop",
        foreign_keys="TripBooking.completed_near_stop_id",
        passive_deletes=True,
    )

    matched_scan_events: Mapped[list["TripScanEvent"]] = relationship(
        back_populates="matched_stop",
        foreign_keys="TripScanEvent.matched_stop_id",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("name <> ''", name="ck_stops_name_nonempty"),
        CheckConstraint("lat >= -90 AND lat <= 90", name="ck_stops_lat_range"),
        CheckConstraint("lng >= -180 AND lng <= 180", name="ck_stops_lng_range"),
        CheckConstraint("radius_meters > 0", name="ck_stops_radius_positive"),
    )


class Route(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "routes"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    has_ac: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True
    )

    route_stops: Mapped[list["RouteStop"]] = relationship(
        back_populates="route",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="RouteStop.sequence_no",
    )
    fares: Mapped[list["RouteFare"]] = relationship(
        back_populates="route",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    scheduled_trips: Mapped[list["ScheduledTrip"]] = relationship(
        back_populates="route",
        passive_deletes=True,
    )
    bookings: Mapped[list["TripBooking"]] = relationship(
        back_populates="route",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("name <> ''", name="ck_routes_name_nonempty"),
        CheckConstraint("code <> ''", name="ck_routes_code_nonempty"),
    )


class RouteStop(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "route_stops"

    route_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("routes.id", ondelete="CASCADE"),
        nullable=False,
    )
    stop_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("stops.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    assume_time_diff_minutes: Mapped[int] = mapped_column(
        Integer, nullable=True, default=0
    )
    boarding_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    deboarding_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    route: Mapped["Route"] = relationship(
        back_populates="route_stops",
        foreign_keys=[route_id],
    )
    stop: Mapped["Stop"] = relationship(
        back_populates="route_stops",
        foreign_keys=[stop_id],
    )

    __table_args__ = (
        UniqueConstraint(
            "route_id", "sequence_no", name="uq_route_stops_route_sequence"
        ),
        CheckConstraint("sequence_no > 0", name="ck_route_stops_sequence_positive"),
        Index("ix_route_stops_route_id_stop_id", "route_id", "stop_id"),
    )


class RouteFare(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "route_fares"

    route_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("routes.id", ondelete="CASCADE"),
        nullable=False,
    )
    pickup_stop_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("stops.id", ondelete="RESTRICT"),
        nullable=False,
    )
    dropoff_stop_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("stops.id", ondelete="RESTRICT"),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    route: Mapped["Route"] = relationship(
        back_populates="fares",
        foreign_keys=[route_id],
    )
    pickup_stop: Mapped["Stop"] = relationship(
        back_populates="pickup_fares",
        foreign_keys=[pickup_stop_id],
    )
    dropoff_stop: Mapped["Stop"] = relationship(
        back_populates="dropoff_fares",
        foreign_keys=[dropoff_stop_id],
    )

    __table_args__ = (
        UniqueConstraint(
            "route_id",
            "pickup_stop_id",
            "dropoff_stop_id",
            name="uq_route_fares_route_pickup_dropoff",
        ),
        CheckConstraint("amount >= 0", name="ck_route_fares_amount_nonnegative"),
        CheckConstraint(
            "pickup_stop_id <> dropoff_stop_id",
            name="ck_route_fares_pickup_dropoff_different",
        ),
    )


class PlatformSettings(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "platform_settings"

    settings_key: Mapped[str] = mapped_column(
        String(32),
        unique=True,
        nullable=False,
        default="default",
    )
    commission_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2),
        nullable=False,
        default=Decimal("0.00"),
    )
    commercial_policy_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "commission_percent >= 0 AND commission_percent <= 100",
            name="ck_platform_settings_commission_percent_range",
        ),
    )


# ============================================================
# trips / bookings
# ============================================================


class ScheduledTrip(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "scheduled_trips"

    route_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("routes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    driver_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    vehicle_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("vehicles.id", ondelete="RESTRICT"),
        nullable=False,
    )
    emergency_stop_request_status: Mapped[EmergencyStopRequestStatus | None] = mapped_column(
    enum_type(EmergencyStopRequestStatus, "emergency_stop_request_status"),
    nullable=True,
    )
    last_lat: Mapped[Decimal | None] = mapped_column(
    Numeric(9, 6),
    nullable=True,
    )

    last_lng: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 6),
        nullable=True,
    )

    planned_start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    planned_end_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    status: Mapped[ScheduledTripStatus] = mapped_column(
        enum_type(ScheduledTripStatus, "scheduled_trip_status"),
        nullable=False,
        default=ScheduledTripStatus.SCHEDULED,
    )

    actual_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    actual_end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    started_at_lat: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 6),
        nullable=True,
    )
    started_at_long: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 6),
        nullable=True,
    )
    ended_at_lat: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 6),
        nullable=True,
    )
    ended_at_long: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 6),
        nullable=True,
    )

    started_near_stop_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("stops.id", ondelete="SET NULL"),
        nullable=True,
    )
    ended_near_stop_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("stops.id", ondelete="SET NULL"),
        nullable=True,
    )

    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    premature_end_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    route: Mapped["Route"] = relationship(
        back_populates="scheduled_trips",
        foreign_keys=[route_id],
    )
    trip_events: Mapped[list["TripEvent"]] = relationship(
        back_populates="scheduled_trip",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    driver: Mapped["User"] = relationship(
        back_populates="driven_trips",
        foreign_keys=[driver_user_id],
    )
    vehicle: Mapped["Vehicle"] = relationship(
        back_populates="scheduled_trips",
        foreign_keys=[vehicle_id],
    )

    started_near_stop: Mapped["Stop | None"] = relationship(
        back_populates="started_trips_here",
        foreign_keys=[started_near_stop_id],
    )
    ended_near_stop: Mapped["Stop | None"] = relationship(
        back_populates="ended_trips_here",
        foreign_keys=[ended_near_stop_id],
    )

    bookings: Mapped[list["TripBooking"]] = relationship(
        back_populates="scheduled_trip",
        passive_deletes=True,
    )
    scan_events: Mapped[list["TripScanEvent"]] = relationship(
        back_populates="scheduled_trip",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    ratings: Mapped[list["BookingRating"]] = relationship(
        back_populates="scheduled_trip",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint(
            "planned_end_at > planned_start_at",
            name="ck_scheduled_trips_planned_window_valid",
        ),
        Index("ix_scheduled_trips_driver_start", "driver_user_id", "planned_start_at"),
        Index("ix_scheduled_trips_vehicle_start", "vehicle_id", "planned_start_at"),
        Index("ix_scheduled_trips_route_start", "route_id", "planned_start_at"),
    )


class TripEvent(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "trip_events"

    scheduled_trip_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("scheduled_trips.id", ondelete="CASCADE"),
        nullable=False,
    )

    stop_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("stops.id", ondelete="RESTRICT"),
        nullable=False,
    )

    arrival_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    departure_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # 🔗 Relationships
    scheduled_trip: Mapped["ScheduledTrip"] = relationship(
        back_populates="trip_events",
        foreign_keys=[scheduled_trip_id],
    )

    stop: Mapped["Stop"] = relationship(
        foreign_keys=[stop_id],
    )

    __table_args__ = (
        # 🚫 Prevent duplicate stop entry per trip
        UniqueConstraint(
            "scheduled_trip_id",
            "stop_id",
            name="uq_trip_events_trip_stop",
        ),
        # ⚡ Faster query
        Index(
            "ix_trip_events_trip_stop",
            "scheduled_trip_id",
            "stop_id",
        ),
    )


class TripBooking(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "trip_bookings"

    passenger_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    otp: Mapped[str | None] = mapped_column(
        String(10),   
        nullable=True
    )
    scheduled_trip_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("scheduled_trips.id", ondelete="RESTRICT"),
        nullable=False,
    )
    route_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("routes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    pickup_stop_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("stops.id", ondelete="RESTRICT"),
        nullable=False,
    )
    dropoff_stop_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("stops.id", ondelete="RESTRICT"),
        nullable=False,
    )

    booking_status: Mapped[BookingStatus] = mapped_column(
        enum_type(BookingStatus, "booking_status"),
        nullable=False,
        default=BookingStatus.PENDING_PAYMENT,
    )

    fare_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    pickup_sequence_no_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    dropoff_sequence_no_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)

    payment_hold_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    commission_percent_snapshot: Mapped[Decimal] = mapped_column(
        Numeric(5, 2),
        nullable=False,
        default=Decimal("0.00"),
    )
    commission_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0.00"),
    )
    driver_payout_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0.00"),
    )

    transfer_status: Mapped[TransferStatus] = mapped_column(
        enum_type(TransferStatus, "transfer_status"),
        nullable=False,
        default=TransferStatus.NOT_READY,
    )
    transfer_ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    transfer_processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    withheld_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    boarded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    refund_retry_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refund_attempt_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    boarded_near_stop_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("stops.id", ondelete="SET NULL"),
        nullable=True,
    )
    completed_near_stop_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("stops.id", ondelete="SET NULL"),
        nullable=True,
    )

    passenger: Mapped["User"] = relationship(
        back_populates="passenger_bookings",
        foreign_keys=[passenger_user_id],
    )
    scheduled_trip: Mapped["ScheduledTrip"] = relationship(
        back_populates="bookings",
        foreign_keys=[scheduled_trip_id],
    )
    route: Mapped["Route"] = relationship(
        back_populates="bookings",
        foreign_keys=[route_id],
    )
    pickup_stop: Mapped["Stop"] = relationship(
        back_populates="pickup_bookings",
        foreign_keys=[pickup_stop_id],
    )
    dropoff_stop: Mapped["Stop"] = relationship(
        back_populates="dropoff_bookings",
        foreign_keys=[dropoff_stop_id],
    )
    boarded_near_stop: Mapped["Stop | None"] = relationship(
        back_populates="boarded_bookings_here",
        foreign_keys=[boarded_near_stop_id],
    )
    completed_near_stop: Mapped["Stop | None"] = relationship(
        back_populates="completed_bookings_here",
        foreign_keys=[completed_near_stop_id],
    )

    payments: Mapped[list["BookingPayment"]] = relationship(
        back_populates="booking",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    transfer: Mapped["BookingTransfer | None"] = relationship(
        back_populates="booking",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )

    originated_payout_adjustments: Mapped[list["PayoutAdjustment"]] = relationship(
        back_populates="origin_booking",
        foreign_keys="PayoutAdjustment.origin_booking_id",
        passive_deletes=True,
    )

    applied_payout_adjustment_applications: Mapped[list["PayoutAdjustmentApplication"]] = relationship(
        back_populates="applied_on_booking",
        foreign_keys="PayoutAdjustmentApplication.applied_on_booking_id",
        passive_deletes=True,
    )

    rating: Mapped["BookingRating | None"] = relationship(
        back_populates="booking",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    scan_events: Mapped[list["TripScanEvent"]] = relationship(
        back_populates="booking",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index(
            "uq_trip_bookings_passenger_trip_active",
            "passenger_user_id",
            "scheduled_trip_id",
            unique=True,
            postgresql_where=text(
                "booking_status IN ('pending_payment', 'booked', 'boarded')"
            ),
        ),
        CheckConstraint("fare_amount >= 0", name="ck_trip_bookings_fare_nonnegative"),
        CheckConstraint(
            "commission_percent_snapshot >= 0 AND commission_percent_snapshot <= 100",
            name="ck_trip_bookings_commission_percent_range",
        ),
        CheckConstraint(
            "commission_amount >= 0",
            name="ck_trip_bookings_commission_amount_nonnegative",
        ),
        CheckConstraint(
            "driver_payout_amount >= 0",
            name="ck_trip_bookings_driver_payout_nonnegative",
        ),
        CheckConstraint(
            "pickup_stop_id <> dropoff_stop_id",
            name="ck_trip_bookings_pickup_dropoff_different",
        ),
        CheckConstraint(
            "pickup_sequence_no_snapshot > 0",
            name="ck_trip_bookings_pickup_sequence_positive",
        ),
        CheckConstraint(
            "dropoff_sequence_no_snapshot > pickup_sequence_no_snapshot",
            name="ck_trip_bookings_dropoff_after_pickup",
        ),
        Index("ix_trip_bookings_trip_status", "scheduled_trip_id", "booking_status"),
        Index(
            "ix_trip_bookings_status_refund_retry_after",
            "booking_status",
            "refund_retry_after",
        ),
    )


# ============================================================
# payments / transfers
# ============================================================


class BookingPayment(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "booking_payments"

    booking_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("trip_bookings.id", ondelete="CASCADE"),
        nullable=False,
    )

    razorpay_order_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )
    razorpay_payment_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True
    )
    razorpay_signature: Mapped[str | None] = mapped_column(String(255), nullable=True)

    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[BookingPaymentStatus] = mapped_column(
        enum_type(BookingPaymentStatus, "booking_payment_status"),
        nullable=False,
        default=BookingPaymentStatus.CREATED,
    )

    booking: Mapped["TripBooking"] = relationship(
        back_populates="payments",
        foreign_keys=[booking_id],
    )

    source_transfer: Mapped[list["BookingTransfer"]] = relationship(
        back_populates="source_booking_payment",
        foreign_keys="BookingTransfer.source_booking_payment_id",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_booking_payments_amount_positive"),
        Index("ix_booking_payments_booking_status", "booking_id", "status"),
    )


class BookingTransfer(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "booking_transfers"

    booking_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("trip_bookings.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    driver_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_booking_payment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("booking_payments.id", ondelete="RESTRICT"),
        nullable=False,
    )

    applied_payout_adjustment_applications: Mapped[list["PayoutAdjustmentApplication"]] = relationship(
        back_populates="booking_transfer",
        foreign_keys="PayoutAdjustmentApplication.booking_transfer_id",
        passive_deletes=True,
    )

    linked_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    razorpay_transfer_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True
    )

    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[BookingTransferStatus] = mapped_column(
        enum_type(BookingTransferStatus, "booking_transfer_status"),
        nullable=False,
        default=BookingTransferStatus.CREATED,
    )

    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reversed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    booking: Mapped["TripBooking"] = relationship(
        back_populates="transfer",
        foreign_keys=[booking_id],
    )
    driver: Mapped["User"] = relationship(
        back_populates="created_transfers_for_driver",
        foreign_keys=[driver_user_id],
    )
    source_booking_payment: Mapped["BookingPayment"] = relationship(
        back_populates="source_transfer",
        foreign_keys=[source_booking_payment_id],
    )

    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_booking_transfers_amount_nonnegative"),
    )

class PayoutAdjustment(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "payout_adjustments"

    origin_booking_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("trip_bookings.id", ondelete="RESTRICT"),
        nullable=False,
    )

    adjustment_type: Mapped[PayoutAdjustmentType] = mapped_column(
        enum_type(PayoutAdjustmentType, "payout_adjustment_type"),
        nullable=False,
    )

    amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    reason_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    reason_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    admin_note: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    decision_status: Mapped[PayoutAdjustmentDecision] = mapped_column(
        enum_type(PayoutAdjustmentDecision, "payout_adjustment_decision"),
        nullable=False,
        default=PayoutAdjustmentDecision.PENDING,
    )

    created_by_admin_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    decided_by_admin_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    origin_booking: Mapped["TripBooking"] = relationship(
        back_populates="originated_payout_adjustments",
        foreign_keys=[origin_booking_id],
    )

    created_by_admin: Mapped["User"] = relationship(
        back_populates="created_payout_adjustments",
        foreign_keys=[created_by_admin_id],
    )

    decided_by_admin: Mapped["User | None"] = relationship(
        back_populates="decided_payout_adjustments",
        foreign_keys=[decided_by_admin_id],
    )

    applications: Mapped[list["PayoutAdjustmentApplication"]] = relationship(
        back_populates="adjustment",
        foreign_keys="PayoutAdjustmentApplication.payout_adjustment_id",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint(
            "amount > 0",
            name="ck_payout_adjustments_amount_positive",
        ),
        CheckConstraint(
            "reason_text <> ''",
            name="ck_payout_adjustments_reason_text_nonempty",
        ),
        CheckConstraint(
            "("
            "(decision_status = 'pending' AND decided_by_admin_id IS NULL AND decided_at IS NULL)"
            " OR "
            "(decision_status IN ('included', 'excluded') AND decided_by_admin_id IS NOT NULL AND decided_at IS NOT NULL)"
            ")",
            name="ck_payout_adjustments_decision_consistent",
        ),
        Index(
            "ix_payout_adjustments_origin_booking_decision",
            "origin_booking_id",
            "decision_status",
        ),
        Index(
            "ix_payout_adjustments_created_by_admin",
            "created_by_admin_id",
        ),
        Index(
            "ix_payout_adjustments_decided_by_admin",
            "decided_by_admin_id",
        ),
    )

class PayoutAdjustmentApplication(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "payout_adjustment_applications"

    payout_adjustment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("payout_adjustments.id", ondelete="RESTRICT"),
        nullable=False,
    )

    applied_on_booking_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("trip_bookings.id", ondelete="RESTRICT"),
        nullable=False,
    )

    booking_transfer_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("booking_transfers.id", ondelete="RESTRICT"),
        nullable=True,
    )

    applied_by_admin_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    applied_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )

    adjustment: Mapped["PayoutAdjustment"] = relationship(
        back_populates="applications",
        foreign_keys=[payout_adjustment_id],
    )

    applied_on_booking: Mapped["TripBooking"] = relationship(
        back_populates="applied_payout_adjustment_applications",
        foreign_keys=[applied_on_booking_id],
    )

    booking_transfer: Mapped["BookingTransfer | None"] = relationship(
        back_populates="applied_payout_adjustment_applications",
        foreign_keys=[booking_transfer_id],
    )

    applied_by_admin: Mapped["User"] = relationship(
        back_populates="applied_payout_adjustment_applications",
        foreign_keys=[applied_by_admin_id],
    )

    __table_args__ = (
        CheckConstraint(
            "applied_amount > 0",
            name="ck_payout_adjustment_applications_amount_positive",
        ),
        UniqueConstraint(
            "payout_adjustment_id",
            "applied_on_booking_id",
            name="uq_payout_adjustment_application_once_per_booking",
        ),
        Index(
            "ix_payout_adjustment_applications_adjustment",
            "payout_adjustment_id",
        ),
        Index(
            "ix_payout_adjustment_applications_applied_booking",
            "applied_on_booking_id",
        ),
        Index(
            "ix_payout_adjustment_applications_transfer",
            "booking_transfer_id",
        ),
        Index(
            "ix_payout_adjustment_applications_applied_by_admin",
            "applied_by_admin_id",
        ),
    )


# ============================================================
# scans / ratings
# ============================================================


class TripScanEvent(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "trip_scan_events"

    scheduled_trip_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("scheduled_trips.id", ondelete="CASCADE"),
        nullable=False,
    )
    booking_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("trip_bookings.id", ondelete="CASCADE"),
        nullable=False,
    )
    driver_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    scan_type: Mapped[ScanType] = mapped_column(
        enum_type(ScanType, "scan_type"),
        nullable=False,
    )

    scan_lat: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    scan_lng: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)

    matched_stop_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("stops.id", ondelete="SET NULL"),
        nullable=True,
    )
    within_radius: Mapped[bool] = mapped_column(Boolean, nullable=False)
    qr_payload_user_id: Mapped[str] = mapped_column(String(36), nullable=False)

    scheduled_trip: Mapped["ScheduledTrip"] = relationship(
        back_populates="scan_events",
        foreign_keys=[scheduled_trip_id],
    )
    booking: Mapped["TripBooking"] = relationship(
        back_populates="scan_events",
        foreign_keys=[booking_id],
    )
    driver: Mapped["User"] = relationship(
        back_populates="scan_events_as_driver",
        foreign_keys=[driver_user_id],
    )
    matched_stop: Mapped["Stop | None"] = relationship(
        back_populates="matched_scan_events",
        foreign_keys=[matched_stop_id],
    )

    __table_args__ = (
        CheckConstraint(
            "scan_lat >= -90 AND scan_lat <= 90", name="ck_trip_scan_events_lat_range"
        ),
        CheckConstraint(
            "scan_lng >= -180 AND scan_lng <= 180", name="ck_trip_scan_events_lng_range"
        ),
        Index("ix_trip_scan_events_trip_booking", "scheduled_trip_id", "booking_id"),
    )


class BookingRating(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "booking_ratings"

    booking_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("trip_bookings.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    passenger_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    driver_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    scheduled_trip_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("scheduled_trips.id", ondelete="RESTRICT"),
        nullable=False,
    )

    trip_rating: Mapped[int] = mapped_column(Integer, nullable=False)
    driver_rating: Mapped[int] = mapped_column(Integer, nullable=False)
    review_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    booking: Mapped["TripBooking"] = relationship(
        back_populates="rating",
        foreign_keys=[booking_id],
    )
    passenger: Mapped["User"] = relationship(
        back_populates="ratings_given",
        foreign_keys=[passenger_user_id],
    )
    driver: Mapped["User"] = relationship(
        back_populates="ratings_received_as_driver",
        foreign_keys=[driver_user_id],
    )
    scheduled_trip: Mapped["ScheduledTrip"] = relationship(
        back_populates="ratings",
        foreign_keys=[scheduled_trip_id],
    )

    __table_args__ = (
        CheckConstraint(
            "trip_rating >= 1 AND trip_rating <= 5",
            name="ck_booking_ratings_trip_range",
        ),
        CheckConstraint(
            "driver_rating >= 1 AND driver_rating <= 5",
            name="ck_booking_ratings_driver_range",
        ),
    )


class SupportTicket(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "support_tickets"

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    attachment_path: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[SupportStatus] = mapped_column(
        enum_type(SupportStatus, "support_status"),
        nullable=False,
        default=SupportStatus.PENDING,
    )

    # 🔗 Admin handling
    resolved_by_admin_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    rejection_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # 🔗 Relationships
    user: Mapped["User"] = relationship(
        foreign_keys=[user_id],
    )

    resolved_by_admin: Mapped["User | None"] = relationship(
        foreign_keys=[resolved_by_admin_id],
    )

    __table_args__ = (
        CheckConstraint("subject <> ''", name="ck_support_subject_nonempty"),
        CheckConstraint("description <> ''", name="ck_support_description_nonempty"),
        Index("ix_support_user_status", "user_id", "status"),
    )

class JobLease(Base):
    __tablename__ = "job_leases"

    job_name: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        nullable=False,
    )
    owner_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    __table_args__ = (
        CheckConstraint("job_name <> ''", name="ck_job_leases_job_name_nonempty"),
        CheckConstraint("owner_id <> ''", name="ck_job_leases_owner_id_nonempty"),
        Index("ix_job_leases_lease_expires_at", "lease_expires_at"),
    )
