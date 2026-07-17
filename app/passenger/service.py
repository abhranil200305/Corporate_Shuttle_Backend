from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import HTTPException, UploadFile
from sqlalchemy import Numeric, and_, cast, func, literal, or_, select, text, union_all
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.schema import (
    BookingPayment,
    BookingPaymentStatus,
    BookingRating,
    BookingStatus,
    PassengerProfile,
    PassengerTravellerProfile,
    PlatformSettings,
    Route,
    RouteFare,
    RouteStop,
    ScheduledTrip,
    ScheduledTripStatus,
    Stop,
    SupportTicket,
    TripBooking,
    TripEvent,
    User,
    UserRole,
    RFIDCard,
    RFIDCardAccount,
    RFIDCardAssignment,
    RFIDLedgerEntry,
    RFIDRecharge,
    RFIDTripRide,
    RFIDFundingLot,
    RFIDFundingLotSourceType,
    RFIDFundingLotStatus,
    RFIDLedgerEntryType,
    RFIDRechargeSourceType,
    RFIDRechargeStatus,
    new_id,
    RFIDCardAuthorizationStatus,
    RFIDCardInventoryStatus,
    RFIDRideStatus,
    BookingSession,
    BookingSessionPayment,
    BookingSessionStatus,
    PassengerTravellerProfile,
    TravellerContactNotification,
    TravellerContactNotificationStatus,
    BookingSeatRefundRequest,
    BookingSeatRefundRequestStatus,
    InvoiceEmailDelivery,
)
from app.notifications.hub import WSHub
from app.notifications.service import NotificationService
from app.passenger.booking_conflicts import (
    DEFAULT_TRANSFER_BUFFER_MINUTES,
    build_guest_traveller_identity,
    build_profile_traveller_identity,
    build_self_traveller_identity,
    journey_windows_conflict,
    normalize_phone_for_identity,
    route_segments_overlap,
)
from app.passenger.schemas import (
    CreateBookingRatingRequest,
    CreateBookingRequest,
    FarePreviewRequest,
    LegAvailableSeatsRequest,
    PassengerProfileUpsertRequest,
    PassengerTravellerProfileCreateRequest,
    PassengerTravellerProfileUpdateRequest,
    VerifyBookingPaymentRequest,
    PassengerRFIDRechargeCreateOrderRequest,
    PassengerRFIDRechargeVerifyPaymentRequest,
    CreateBookingSessionRequest,
    VerifyBookingSessionPaymentRequest,
    normalize_traveller_email,
    normalize_traveller_phone,
)
from app.tax import (
    GSTBreakdown,
    build_gst_breakdown,
    gst_config_from_settings,
    gst_invoice_profile_from_settings,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


IST = timezone(timedelta(hours=5, minutes=30))


class PassengerService:
    def __init__(
        self,
        db: AsyncSession,
        ws_hub: WSHub | None = None,
    ) -> None:
        self.db = db
        self.ws_hub = ws_hub

    # ------------------------------------------------------------------
    # role guard
    # ------------------------------------------------------------------
    @staticmethod
    def ensure_passenger(user: User) -> None:
        if user.role != UserRole.PASSENGER:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "passenger_only",
                    "message": "This endpoint is only available to passengers.",
                },
            )

    # ------------------------------------------------------------------
    # generic helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_name(value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_full_name",
                    "message": "Full name cannot be empty.",
                },
            )
        return cleaned
    
    @staticmethod
    def _get_profile_picture_upload_dir() -> Path:
        upload_dir = Path.cwd() / "uploads" / "passenger" / "profilepictures"
        upload_dir.mkdir(parents=True, exist_ok=True)
        return upload_dir
    
    @staticmethod
    def _guess_profile_picture_extension(
        filename: str | None,
        content_type: str | None,
    ) -> str:
        suffix = Path(filename or "").suffix.lower().strip()
        if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            return ".jpg" if suffix == ".jpeg" else suffix

        mime_to_ext = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }
        return mime_to_ext.get((content_type or "").lower(), ".jpg")
    
    @staticmethod
    def _clean_support_text(value: str, *, field_name: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"invalid_{field_name}",
                    "message": f"{field_name.replace('_', ' ').capitalize()} cannot be empty.",
                },
            )
        return cleaned

    @staticmethod
    def _get_support_upload_dir() -> Path:
        upload_dir = Path.cwd() / "uploads" / "support" / "passenger"
        upload_dir.mkdir(parents=True, exist_ok=True)
        return upload_dir

    @staticmethod
    def _guess_support_attachment_extension(filename: str | None) -> str:
        suffix = Path(filename or "").suffix.lower().strip()
        if not suffix:
            return ""
        if len(suffix) > 12:
            return ""
        allowed = {
            ".jpg", ".jpeg", ".png", ".webp", ".gif",
            ".pdf", ".txt", ".doc", ".docx",
        }
        if suffix not in allowed:
            return ""
        return ".jpg" if suffix == ".jpeg" else suffix

    def _serialize_support_ticket(self, ticket: SupportTicket) -> dict[str, Any]:
        return {
            "id": ticket.id,
            "user_id": ticket.user_id,
            "subject": ticket.subject,
            "description": ticket.description,
            "attachment_path": ticket.attachment_path,
            "status": ticket.status,
            "resolved_at": ticket.resolved_at,
            "rejection_reason": ticket.rejection_reason,
            "created_at": ticket.created_at,
            "updated_at": ticket.updated_at,
        }

    @staticmethod
    def _quantize_money(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @classmethod
    def _to_subunits(cls, amount: Decimal) -> int:
        return int((cls._quantize_money(amount) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    
    @staticmethod
    def _get_payment_hold_minutes() -> int:
        raw = os.getenv("PASSENGER_PAYMENT_HOLD_MINUTES", "5").strip()
        try:
            minutes = int(raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "invalid_payment_hold_minutes",
                    "message": "PASSENGER_PAYMENT_HOLD_MINUTES must be an integer.",
                },
            ) from exc

        if minutes <= 0:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "invalid_payment_hold_minutes",
                    "message": "PASSENGER_PAYMENT_HOLD_MINUTES must be greater than 0.",
                },
            )

        return minutes
    
    @staticmethod
    def _generate_booking_otp(length: int = 6) -> str:
        if length <= 0:
            raise ValueError("Booking OTP length must be positive.")
        upper_bound = 10 ** length
        return f"{secrets.randbelow(upper_bound):0{length}d}"

    @staticmethod
    def _should_expose_booking_otp(booking: TripBooking) -> bool:
        return booking.booking_status not in (
            BookingStatus.COMPLETED,
            BookingStatus.CANCELLED,
            BookingStatus.MISSED,
        )

    def _serialize_booking_otp(self, booking: TripBooking) -> str | None:
        if not self._should_expose_booking_otp(booking):
            return None
        return booking.otp

    @staticmethod
    def _set_cancellation_metadata(
        record: Any,
        *,
        reason: str,
        source: str,
        cancelled_by_user_id: str | None,
        cancelled_at: datetime | None = None,
    ) -> datetime:
        occurred_at = cancelled_at or utcnow()
        record.cancelled_at = getattr(record, "cancelled_at", None) or occurred_at
        record.cancellation_reason = reason.strip()
        record.cancellation_source = source
        record.cancelled_by_user_id = cancelled_by_user_id
        return occurred_at

    @staticmethod
    def _serialize_cancellation_metadata(record: Any) -> dict[str, Any] | None:
        cancelled_at = getattr(record, "cancelled_at", None)
        if cancelled_at is None:
            return None

        return {
            "cancelled_at": cancelled_at,
            "reason": (
                getattr(record, "cancellation_reason", None)
                or getattr(record, "premature_end_reason", None)
                or "Cancellation reason was not recorded."
            ),
            "source": getattr(record, "cancellation_source", None) or "legacy",
            "cancelled_by_user_id": getattr(
                record, "cancelled_by_user_id", None
            ),
        }

    @classmethod
    def _get_payment_hold_expires_at(cls) -> datetime:
        return utcnow() + timedelta(minutes=cls._get_payment_hold_minutes())
    
    async def _get_platform_settings_obj(self) -> PlatformSettings | None:
        stmt = (
            select(PlatformSettings)
            .where(PlatformSettings.settings_key == "default")
            .limit(1)
        )
        result = await self.db.execute(stmt)
        settings = result.scalar_one_or_none()

        if settings is not None:
            return settings

        fallback_stmt = (
            select(PlatformSettings)
            .order_by(
                PlatformSettings.updated_at.desc(),
                PlatformSettings.created_at.desc(),
            )
            .limit(1)
        )
        fallback_result = await self.db.execute(fallback_stmt)
        return fallback_result.scalar_one_or_none()

    async def _build_gst_breakdown(
        self,
        amount: Decimal,
        *,
        is_ac: bool | None,
    ) -> GSTBreakdown:
        settings = await self._get_platform_settings_obj()
        return build_gst_breakdown(
            amount,
            is_ac=is_ac,
            config=gst_config_from_settings(settings),
        )

    @staticmethod
    def _gst_breakdown_public_fields(
        breakdown: GSTBreakdown,
        *,
        configured_fare_amount: Decimal,
    ) -> dict[str, Any]:
        return {
            "fare_amount": breakdown.gross_amount,
            "configured_fare_amount": configured_fare_amount,
            "taxable_amount": breakdown.taxable_amount,
            "cgst_rate_percent": breakdown.cgst_rate_percent,
            "cgst_amount": breakdown.cgst_amount,
            "sgst_rate_percent": breakdown.sgst_rate_percent,
            "sgst_amount": breakdown.sgst_amount,
            "igst_rate_percent": breakdown.igst_rate_percent,
            "igst_amount": breakdown.igst_amount,
            "total_tax_amount": breakdown.total_tax_amount,
            "gst_enabled": breakdown.gst_enabled,
            "gst_applicable": breakdown.gst_applicable,
            "gst_inclusive": breakdown.gst_inclusive,
        }

    @staticmethod
    def _booking_taxable_basis(booking: TripBooking) -> Decimal:
        taxable_amount = PassengerService._quantize_money(
            Decimal(getattr(booking, "taxable_amount", Decimal("0.00")) or 0)
        )
        if taxable_amount > Decimal("0.00"):
            return taxable_amount
        return PassengerService._quantize_money(Decimal(booking.fare_amount or 0))

    @staticmethod
    def _apply_gst_breakdown_to_booking(
        booking: TripBooking,
        breakdown: GSTBreakdown,
    ) -> None:
        booking.fare_amount = breakdown.gross_amount
        booking.taxable_amount = breakdown.taxable_amount
        booking.cgst_rate_percent_snapshot = breakdown.cgst_rate_percent
        booking.cgst_amount = breakdown.cgst_amount
        booking.sgst_rate_percent_snapshot = breakdown.sgst_rate_percent
        booking.sgst_amount = breakdown.sgst_amount
        booking.igst_rate_percent_snapshot = breakdown.igst_rate_percent
        booking.igst_amount = breakdown.igst_amount
        booking.total_tax_amount = breakdown.total_tax_amount
        booking.gst_enabled_snapshot = breakdown.gst_enabled and breakdown.gst_applicable
        booking.gst_inclusive_snapshot = breakdown.gst_inclusive

    @staticmethod
    def _booking_tax_fields(booking: TripBooking) -> dict[str, Any]:
        fare_amount = PassengerService._quantize_money(
            Decimal(booking.fare_amount or 0)
        )
        taxable_amount = PassengerService._quantize_money(
            Decimal(getattr(booking, "taxable_amount", 0) or 0)
        )
        if taxable_amount <= Decimal("0.00") and fare_amount > Decimal("0.00"):
            taxable_amount = fare_amount

        cgst_amount = PassengerService._quantize_money(
            Decimal(getattr(booking, "cgst_amount", 0) or 0)
        )
        sgst_amount = PassengerService._quantize_money(
            Decimal(getattr(booking, "sgst_amount", 0) or 0)
        )
        igst_amount = PassengerService._quantize_money(
            Decimal(getattr(booking, "igst_amount", 0) or 0)
        )
        total_tax_amount = PassengerService._quantize_money(
            Decimal(getattr(booking, "total_tax_amount", 0) or 0)
        )
        if total_tax_amount <= Decimal("0.00"):
            total_tax_amount = PassengerService._quantize_money(
                cgst_amount + sgst_amount + igst_amount
            )

        return {
            "taxable_amount": taxable_amount,
            "cgst_rate_percent_snapshot": Decimal(
                getattr(booking, "cgst_rate_percent_snapshot", 0) or 0
            ),
            "cgst_amount": cgst_amount,
            "sgst_rate_percent_snapshot": Decimal(
                getattr(booking, "sgst_rate_percent_snapshot", 0) or 0
            ),
            "sgst_amount": sgst_amount,
            "igst_rate_percent_snapshot": Decimal(
                getattr(booking, "igst_rate_percent_snapshot", 0) or 0
            ),
            "igst_amount": igst_amount,
            "total_tax_amount": total_tax_amount,
            "gst_enabled_snapshot": bool(
                getattr(booking, "gst_enabled_snapshot", False)
            ),
            "gst_inclusive_snapshot": bool(
                getattr(booking, "gst_inclusive_snapshot", True)
            ),
        }

    @staticmethod
    def _rfid_ride_tax_fields(ride: RFIDTripRide) -> dict[str, Any]:
        fare_amount = PassengerService._quantize_money(
            Decimal(ride.fare_amount or 0)
        )
        taxable_amount = PassengerService._quantize_money(
            Decimal(getattr(ride, "taxable_amount", 0) or 0)
        )
        if taxable_amount <= Decimal("0.00") and fare_amount > Decimal("0.00"):
            taxable_amount = fare_amount

        cgst_amount = PassengerService._quantize_money(
            Decimal(getattr(ride, "cgst_amount", 0) or 0)
        )
        sgst_amount = PassengerService._quantize_money(
            Decimal(getattr(ride, "sgst_amount", 0) or 0)
        )
        igst_amount = PassengerService._quantize_money(
            Decimal(getattr(ride, "igst_amount", 0) or 0)
        )
        total_tax_amount = PassengerService._quantize_money(
            Decimal(getattr(ride, "total_tax_amount", 0) or 0)
        )
        if total_tax_amount <= Decimal("0.00"):
            total_tax_amount = PassengerService._quantize_money(
                cgst_amount + sgst_amount + igst_amount
            )

        return {
            "taxable_amount": taxable_amount,
            "cgst_rate_percent_snapshot": Decimal(
                getattr(ride, "cgst_rate_percent_snapshot", 0) or 0
            ),
            "cgst_amount": cgst_amount,
            "sgst_rate_percent_snapshot": Decimal(
                getattr(ride, "sgst_rate_percent_snapshot", 0) or 0
            ),
            "sgst_amount": sgst_amount,
            "igst_rate_percent_snapshot": Decimal(
                getattr(ride, "igst_rate_percent_snapshot", 0) or 0
            ),
            "igst_amount": igst_amount,
            "total_tax_amount": total_tax_amount,
            "gst_enabled_snapshot": bool(
                getattr(ride, "gst_enabled_snapshot", False)
            ),
            "gst_inclusive_snapshot": bool(
                getattr(ride, "gst_inclusive_snapshot", True)
            ),
        }

    @staticmethod
    def _apply_booking_tax_fields_to_payment(
        payment: BookingPayment,
        booking: TripBooking,
    ) -> None:
        tax_fields = PassengerService._booking_tax_fields(booking)
        payment.taxable_amount = tax_fields["taxable_amount"]
        payment.cgst_amount = tax_fields["cgst_amount"]
        payment.sgst_amount = tax_fields["sgst_amount"]
        payment.igst_amount = tax_fields["igst_amount"]
        payment.total_tax_amount = tax_fields["total_tax_amount"]

    @staticmethod
    def _apply_session_tax_fields_to_payment(
        payment: BookingSessionPayment,
        booking_session: BookingSession,
    ) -> None:
        payment.taxable_amount = PassengerService._quantize_money(
            Decimal(getattr(booking_session, "total_taxable_amount", 0) or 0)
        )
        payment.cgst_amount = PassengerService._quantize_money(
            Decimal(getattr(booking_session, "total_cgst_amount", 0) or 0)
        )
        payment.sgst_amount = PassengerService._quantize_money(
            Decimal(getattr(booking_session, "total_sgst_amount", 0) or 0)
        )
        payment.igst_amount = PassengerService._quantize_money(
            Decimal(getattr(booking_session, "total_igst_amount", 0) or 0)
        )
        payment.total_tax_amount = PassengerService._quantize_money(
            Decimal(getattr(booking_session, "total_tax_amount", 0) or 0)
        )

    async def _get_current_commission_percent(self) -> Decimal:
        settings = await self._get_platform_settings_obj()
        if settings is None or settings.commission_percent is None:
            return Decimal("0.00")

        return self._quantize_money(Decimal(settings.commission_percent))

    def _build_booking_commission_snapshot(
        self,
        *,
        fare_amount: Decimal,
        commission_percent: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal]:
        normalized_fare = self._quantize_money(Decimal(fare_amount))
        normalized_commission_percent = self._quantize_money(Decimal(commission_percent))

        commission_amount = self._quantize_money(
            (normalized_fare * normalized_commission_percent) / Decimal("100")
        )
        driver_payout_amount = self._quantize_money(
            normalized_fare - commission_amount
        )

        if driver_payout_amount < Decimal("0.00"):
            driver_payout_amount = Decimal("0.00")

        return (
            normalized_commission_percent,
            commission_amount,
            driver_payout_amount,
        )

    async def _ensure_booking_commission_snapshot(
        self,
        booking: TripBooking,
    ) -> None:
        if (
            booking.commission_percent_snapshot != Decimal("0.00")
            or booking.commission_amount != Decimal("0.00")
            or booking.driver_payout_amount != Decimal("0.00")
        ):
            return

        commission_percent = await self._get_current_commission_percent()
        (
            commission_percent_snapshot,
            commission_amount,
            driver_payout_amount,
        ) = self._build_booking_commission_snapshot(
            fare_amount=self._booking_taxable_basis(booking),
            commission_percent=commission_percent,
        )

        booking.commission_percent_snapshot = commission_percent_snapshot
        booking.commission_amount = commission_amount
        booking.driver_payout_amount = driver_payout_amount

        self.db.add(booking)
        await self.db.flush()

    async def _expire_pending_booking_hold(self, booking: TripBooking) -> None:
        if booking.booking_status != BookingStatus.PENDING_PAYMENT:
            return

        booking.booking_status = BookingStatus.CANCELLED
        self._set_cancellation_metadata(
            booking,
            reason="Payment hold expired before confirmation.",
            source="system",
            cancelled_by_user_id=None,
        )
        booking.payment_hold_expires_at = None
        self.db.add(booking)

        for payment in booking.payments:
            if payment.status == BookingPaymentStatus.CREATED:
                payment.status = BookingPaymentStatus.FAILED
                self.db.add(payment)

        await self.db.flush()

    async def _expire_pending_booking_session(
        self,
        *,
        booking_session: BookingSession,
        bookings: list[TripBooking],
        payments: list[BookingSessionPayment],
    ) -> None:
        now = utcnow()

        if booking_session.status != BookingSessionStatus.PENDING_PAYMENT:
            return

        booking_session.status = BookingSessionStatus.EXPIRED
        booking_session.expired_at = booking_session.expired_at or now
        booking_session.payment_hold_expires_at = None
        self.db.add(booking_session)

        for booking in bookings:
            if booking.booking_status == BookingStatus.PENDING_PAYMENT:
                booking.booking_status = BookingStatus.CANCELLED
                self._set_cancellation_metadata(
                    booking,
                    reason="Booking session payment hold expired before confirmation.",
                    source="system",
                    cancelled_by_user_id=None,
                    cancelled_at=now,
                )
                booking.payment_hold_expires_at = None
                self.db.add(booking)

        for payment in payments:
            if payment.status == BookingPaymentStatus.CREATED:
                payment.status = BookingPaymentStatus.FAILED
                self.db.add(payment)

        await self.db.flush()

    async def _cancel_pending_booking_session(
        self,
        *,
        booking_session: BookingSession,
        bookings: list[TripBooking],
        payments: list[BookingSessionPayment],
        cancelled_by_user_id: str,
    ) -> None:
        now = utcnow()

        if booking_session.status != BookingSessionStatus.PENDING_PAYMENT:
            return

        booking_session.status = BookingSessionStatus.CANCELLED
        self._set_cancellation_metadata(
            booking_session,
            reason="Booking session cancelled by passenger.",
            source="passenger",
            cancelled_by_user_id=cancelled_by_user_id,
            cancelled_at=now,
        )
        booking_session.payment_hold_expires_at = None
        self.db.add(booking_session)

        for booking in bookings:
            if booking.booking_status == BookingStatus.PENDING_PAYMENT:
                booking.booking_status = BookingStatus.CANCELLED
                self._set_cancellation_metadata(
                    booking,
                    reason="Booking session cancelled by passenger.",
                    source="passenger",
                    cancelled_by_user_id=cancelled_by_user_id,
                    cancelled_at=now,
                )
                booking.payment_hold_expires_at = None
                self.db.add(booking)

        for payment in payments:
            if payment.status == BookingPaymentStatus.CREATED:
                payment.status = BookingPaymentStatus.FAILED
                self.db.add(payment)

        await self.db.flush()

    async def _expire_stale_pending_bookings_for_trip(self, scheduled_trip_id: str) -> int:
        stmt = (
            select(TripBooking)
            .where(
                TripBooking.scheduled_trip_id == scheduled_trip_id,
                TripBooking.booking_status == BookingStatus.PENDING_PAYMENT,
                TripBooking.payment_hold_expires_at.is_not(None),
                TripBooking.payment_hold_expires_at <= utcnow(),
            )
            .options(selectinload(TripBooking.payments))
            .with_for_update()
        )
        result = await self.db.execute(stmt)
        stale_bookings = result.scalars().unique().all()

        for stale_booking in stale_bookings:
            await self._expire_pending_booking_hold(stale_booking)

        return len(stale_bookings)

    async def _get_profile_obj(self, user_id: str) -> PassengerProfile | None:
        stmt = select(PassengerProfile).where(PassengerProfile.user_id == user_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()
    
    @staticmethod
    def _clean_optional_text(value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @staticmethod
    def _clean_required_text(
        value: str,
        *,
        field_name: str,
        max_length: int,
    ) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"invalid_{field_name}",
                    "message": f"{field_name.replace('_', ' ').capitalize()} cannot be empty.",
                },
            )
        if len(cleaned) > max_length:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"invalid_{field_name}",
                    "message": f"{field_name.replace('_', ' ').capitalize()} is too long.",
                    "max_length": max_length,
                },
            )
        return cleaned

    @staticmethod
    def _normalize_traveller_contact(
        *,
        phone: str,
        email: str | None,
    ) -> tuple[str, str | None]:
        try:
            normalized_phone = normalize_traveller_phone(phone)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_traveller_phone",
                    "message": str(exc),
                },
            ) from exc

        try:
            normalized_email = normalize_traveller_email(email)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_traveller_email",
                    "message": str(exc),
                },
            ) from exc

        return normalized_phone, normalized_email

    async def _ensure_traveller_profile_phone_unique(
        self,
        *,
        owner_user_id: str,
        phone: str,
        except_profile_id: str | None = None,
    ) -> None:
        filters = [
            PassengerTravellerProfile.owner_user_id == owner_user_id,
            PassengerTravellerProfile.phone == phone,
        ]
        if except_profile_id is not None:
            filters.append(PassengerTravellerProfile.id != except_profile_id)

        result = await self.db.execute(
            select(PassengerTravellerProfile.id).where(*filters).limit(1)
        )
        existing_profile_id = result.scalar_one_or_none()
        if existing_profile_id is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "traveller_phone_already_saved",
                    "message": (
                        "A saved traveller already uses this phone number. "
                        "Update or reactivate that traveller instead."
                    ),
                    "traveller_profile_id": existing_profile_id,
                },
            )

    def _serialize_traveller_profile(
        self,
        profile: PassengerTravellerProfile,
    ) -> dict[str, Any]:
        return {
            "id": profile.id,
            "owner_user_id": profile.owner_user_id,
            "full_name": profile.full_name,
            "phone": profile.phone,
            "email": profile.email,
            "relationship_label": profile.relationship_label,
            "is_self": profile.is_self,
            "is_active": profile.is_active,
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
        }

    async def _get_traveller_profile_for_owner_or_404(
        self,
        *,
        owner_user_id: str,
        profile_id: str,
    ) -> PassengerTravellerProfile:
        stmt = select(PassengerTravellerProfile).where(
            PassengerTravellerProfile.id == profile_id,
            PassengerTravellerProfile.owner_user_id == owner_user_id,
        )
        result = await self.db.execute(stmt)
        profile = result.scalar_one_or_none()

        if profile is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "traveller_profile_not_found",
                    "message": "Traveller profile not found.",
                },
            )

        return profile

    async def _get_booking_session_obj(
        self,
        *,
        booking_session_id: str,
        owner_user_id: str,
    ) -> BookingSession:
        stmt = (
            select(BookingSession)
            .where(
                BookingSession.id == booking_session_id,
                BookingSession.owner_user_id == owner_user_id,
            )
            .options(
                selectinload(BookingSession.bookings),
                selectinload(BookingSession.payments),
            )
        )
        result = await self.db.execute(stmt)
        booking_session = result.scalar_one_or_none()

        if booking_session is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "booking_session_not_found",
                    "message": "Booking session not found.",
                },
            )

        return booking_session
    
    async def _get_booking_session_for_update_or_404(
        self,
        *,
        booking_session_id: str,
        owner_user_id: str,
    ) -> BookingSession:
        stmt = (
            select(BookingSession)
            .where(
                BookingSession.id == booking_session_id,
                BookingSession.owner_user_id == owner_user_id,
            )
            .with_for_update()
        )
        result = await self.db.execute(stmt)
        booking_session = result.scalar_one_or_none()

        if booking_session is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "booking_session_not_found",
                    "message": "Booking session not found.",
                },
            )

        return booking_session

    async def _list_booking_session_payments_for_update(
        self,
        booking_session_id: str,
    ) -> list[BookingSessionPayment]:
        stmt = (
            select(BookingSessionPayment)
            .where(BookingSessionPayment.booking_session_id == booking_session_id)
            .order_by(BookingSessionPayment.created_at.desc())
            .with_for_update()
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _list_booking_session_bookings_for_update(
        self,
        booking_session_id: str,
    ) -> list[TripBooking]:
        stmt = (
            select(TripBooking)
            .where(TripBooking.booking_session_id == booking_session_id)
            .order_by(TripBooking.seat_number.asc())
            .with_for_update()
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    def _get_booking_session_payment_by_order_id(
        self,
        payments: list[BookingSessionPayment],
        *,
        razorpay_order_id: str,
    ) -> BookingSessionPayment | None:
        return next(
            (
                payment
                for payment in payments
                if payment.razorpay_order_id == razorpay_order_id
            ),
            None,
        )
    
    async def _expire_pending_booking_session(
        self,
        *,
        booking_session: BookingSession,
        bookings: list[TripBooking],
        payments: list[BookingSessionPayment],
    ) -> None:
        now = utcnow()

        if booking_session.status != BookingSessionStatus.PENDING_PAYMENT:
            return

        booking_session.status = BookingSessionStatus.EXPIRED
        booking_session.expired_at = booking_session.expired_at or now
        booking_session.payment_hold_expires_at = None
        self.db.add(booking_session)

        for booking in bookings:
            if booking.booking_status == BookingStatus.PENDING_PAYMENT:
                booking.booking_status = BookingStatus.CANCELLED
                self._set_cancellation_metadata(
                    booking,
                    reason="Booking session payment hold expired before confirmation.",
                    source="system",
                    cancelled_by_user_id=None,
                    cancelled_at=now,
                )
                booking.payment_hold_expires_at = None
                self.db.add(booking)

        for payment in payments:
            if payment.status == BookingPaymentStatus.CREATED:
                payment.status = BookingPaymentStatus.FAILED
                self.db.add(payment)

        await self.db.flush()

    async def _mark_booking_session_paid_and_confirmed(
        self,
        *,
        booking_session: BookingSession,
        payment: BookingSessionPayment,
        bookings: list[TripBooking],
        razorpay_payment_id: str | None,
        razorpay_signature: str | None = None,
    ) -> None:
        now = utcnow()

        if razorpay_payment_id:
            payment.razorpay_payment_id = razorpay_payment_id

        if razorpay_signature:
            payment.razorpay_signature = razorpay_signature

        payment.status = BookingPaymentStatus.PAID

        booking_session.status = BookingSessionStatus.CONFIRMED
        booking_session.confirmed_at = booking_session.confirmed_at or now
        booking_session.payment_hold_expires_at = None

        for booking in bookings:
            if booking.booking_status == BookingStatus.PENDING_PAYMENT:
                booking.booking_status = BookingStatus.BOOKED
                booking.payment_hold_expires_at = None
                self.db.add(booking)
            await self._queue_invoice_email_delivery(booking)

        self.db.add(payment)
        self.db.add(booking_session)
        await self.db.flush()

    async def _queue_invoice_email_delivery(self, booking: TripBooking) -> None:
        delivery_key = (
            f"session:{booking.booking_session_id}"
            if booking.booking_session_id
            else f"booking:{booking.id}"
        )
        await self.db.execute(
            pg_insert(InvoiceEmailDelivery)
            .values(
                id=new_id(),
                delivery_key=delivery_key,
                booking_id=booking.id,
                status="pending",
                attempt_count=0,
            )
            .on_conflict_do_nothing(
                index_elements=[InvoiceEmailDelivery.delivery_key]
            )
        )
        await self.db.flush()

    async def _mark_booking_session_paid_but_expired(
        self,
        *,
        booking_session: BookingSession,
        payment: BookingSessionPayment,
        bookings: list[TripBooking],
        payments: list[BookingSessionPayment],
        razorpay_payment_id: str | None,
    ) -> None:
        await self._expire_pending_booking_session(
            booking_session=booking_session,
            bookings=bookings,
            payments=payments,
        )

        now = utcnow()
        if razorpay_payment_id:
            payment.razorpay_payment_id = razorpay_payment_id
        if payment.status != BookingPaymentStatus.REFUNDED:
            payment.status = BookingPaymentStatus.PAID
            payment.refund_requested_at = payment.refund_requested_at or now
            payment.refund_retry_after = payment.refund_retry_after or now
            payment.refund_attempt_count = payment.refund_attempt_count or 0
            payment.refund_failure_reason = None
        self.db.add(payment)

        for booking in bookings:
            await self._ensure_booking_seat_refund_request(
                booking_session=booking_session,
                booking=booking,
                payment=payment,
            )

        await self.db.flush()

    async def reconcile_pending_booking_session_payment(
        self,
        booking_session: BookingSession,
        *,
        bookings: list[TripBooking] | None = None,
        payments: list[BookingSessionPayment] | None = None,
    ) -> str:
        if booking_session.status != BookingSessionStatus.PENDING_PAYMENT:
            return "skip_non_pending"

        session_bookings = list(
            booking_session.bookings if bookings is None else bookings
        )
        session_payments = sorted(
            list(booking_session.payments if payments is None else payments),
            key=lambda item: item.created_at,
            reverse=True,
        )
        hold_expired = (
            booking_session.payment_hold_expires_at is not None
            and booking_session.payment_hold_expires_at <= utcnow()
        )

        if not session_payments:
            if hold_expired:
                await self._expire_pending_booking_session(
                    booking_session=booking_session,
                    bookings=session_bookings,
                    payments=session_payments,
                )
                return "expired_without_local_payment"
            return "pending_without_local_payment"

        paid_payment = next(
            (
                payment
                for payment in session_payments
                if payment.status == BookingPaymentStatus.PAID
            ),
            None,
        )
        if paid_payment is not None:
            if hold_expired:
                await self._mark_booking_session_paid_but_expired(
                    booking_session=booking_session,
                    payment=paid_payment,
                    bookings=session_bookings,
                    payments=session_payments,
                    razorpay_payment_id=paid_payment.razorpay_payment_id,
                )
                return "paid_after_hold_expiry"

            await self._mark_booking_session_paid_and_confirmed(
                booking_session=booking_session,
                payment=paid_payment,
                bookings=session_bookings,
                razorpay_payment_id=paid_payment.razorpay_payment_id,
                razorpay_signature=paid_payment.razorpay_signature,
            )
            return "promoted_local_paid"

        best_provider_payment: dict[str, Any] | None = None
        selected_payment: BookingSessionPayment | None = None

        for payment in session_payments:
            provider_items = await self._fetch_razorpay_order_payments(
                payment.razorpay_order_id
            )
            provider_payment = self._select_best_razorpay_order_payment(
                provider_items,
                expected_order_id=payment.razorpay_order_id,
                expected_amount_subunits=self._to_subunits(payment.amount),
            )
            if provider_payment is None:
                continue

            provider_status = str(
                provider_payment.get("status") or ""
            ).strip().lower()
            if best_provider_payment is None:
                best_provider_payment = provider_payment
                selected_payment = payment
            if provider_status in {"captured", "authorized"}:
                best_provider_payment = provider_payment
                selected_payment = payment
                break

        if selected_payment is None or best_provider_payment is None:
            if hold_expired:
                await self._expire_pending_booking_session(
                    booking_session=booking_session,
                    bookings=session_bookings,
                    payments=session_payments,
                )
                return "expired_without_provider_payment"
            return "pending_without_provider_payment"

        provider_status = str(
            best_provider_payment.get("status") or ""
        ).strip().lower()
        provider_payment_id = (
            str(best_provider_payment.get("id") or "").strip() or None
        )

        if provider_status == "captured":
            if hold_expired:
                await self._mark_booking_session_paid_but_expired(
                    booking_session=booking_session,
                    payment=selected_payment,
                    bookings=session_bookings,
                    payments=session_payments,
                    razorpay_payment_id=provider_payment_id,
                )
                return "captured_after_hold_expiry"

            await self._mark_booking_session_paid_and_confirmed(
                booking_session=booking_session,
                payment=selected_payment,
                bookings=session_bookings,
                razorpay_payment_id=provider_payment_id,
            )
            return "confirmed_from_captured_payment"

        if provider_status == "authorized":
            if hold_expired:
                await self._expire_pending_booking_session(
                    booking_session=booking_session,
                    bookings=session_bookings,
                    payments=session_payments,
                )
                return "expired_with_authorized_payment"

            if not provider_payment_id:
                return "pending_authorized_without_payment_id"

            captured_payment = await self._capture_razorpay_payment(
                provider_payment_id,
                self._to_subunits(selected_payment.amount),
            )
            captured_status = str(
                captured_payment.get("status") or ""
            ).strip().lower()
            captured_flag = bool(captured_payment.get("captured", False))

            if captured_status == "captured" or captured_flag:
                await self._mark_booking_session_paid_and_confirmed(
                    booking_session=booking_session,
                    payment=selected_payment,
                    bookings=session_bookings,
                    razorpay_payment_id=provider_payment_id,
                )
                return "confirmed_after_capture"

            return f"pending_after_capture_attempt_{captured_status or 'unknown'}"

        if hold_expired:
            await self._expire_pending_booking_session(
                booking_session=booking_session,
                bookings=session_bookings,
                payments=session_payments,
            )
            return f"expired_with_{provider_status or 'unknown'}_payment"

        return f"pending_with_{provider_status or 'unknown'}_payment"

    async def reconcile_closed_booking_session_payment(
        self,
        booking_session: BookingSession,
        *,
        bookings: list[TripBooking] | None = None,
        payments: list[BookingSessionPayment] | None = None,
    ) -> str:
        if booking_session.status not in {
            BookingSessionStatus.EXPIRED,
            BookingSessionStatus.CANCELLED,
        }:
            return "skip_non_closed"

        session_bookings = list(
            booking_session.bookings if bookings is None else bookings
        )
        session_payments = sorted(
            list(booking_session.payments if payments is None else payments),
            key=lambda item: item.created_at,
            reverse=True,
        )
        if not session_payments:
            return "closed_without_local_payment"

        paid_without_refund = next(
            (
                payment
                for payment in session_payments
                if payment.status == BookingPaymentStatus.PAID
                and payment.refund_requested_at is None
            ),
            None,
        )
        if paid_without_refund is not None:
            await self._mark_booking_session_paid_but_expired(
                booking_session=booking_session,
                payment=paid_without_refund,
                bookings=session_bookings,
                payments=session_payments,
                razorpay_payment_id=paid_without_refund.razorpay_payment_id,
            )
            return "closed_paid_payment_refund_queued"

        observed_statuses: set[str] = set()
        for payment in session_payments:
            if payment.status in {
                BookingPaymentStatus.PAID,
                BookingPaymentStatus.REFUNDED,
            }:
                continue

            provider_items = await self._fetch_razorpay_order_payments(
                payment.razorpay_order_id
            )
            provider_payment = self._select_best_razorpay_order_payment(
                provider_items,
                expected_order_id=payment.razorpay_order_id,
                expected_amount_subunits=self._to_subunits(payment.amount),
            )
            if provider_payment is None:
                continue

            provider_status = str(
                provider_payment.get("status") or ""
            ).strip().lower()
            observed_statuses.add(provider_status or "unknown")
            provider_payment_id = (
                str(provider_payment.get("id") or "").strip() or None
            )

            if provider_status != "captured" and not bool(
                provider_payment.get("captured", False)
            ):
                continue

            await self._mark_booking_session_paid_but_expired(
                booking_session=booking_session,
                payment=payment,
                bookings=session_bookings,
                payments=session_payments,
                razorpay_payment_id=provider_payment_id,
            )
            return "closed_captured_payment_refund_queued"

        if not observed_statuses:
            return "closed_without_provider_payment"

        return "closed_with_" + "_or_".join(sorted(observed_statuses))

    @staticmethod
    def _get_booking_from_session_bookings_or_404(
        *,
        bookings: list[TripBooking],
        booking_id: str,
    ) -> TripBooking:
        for booking in bookings:
            if booking.id == booking_id:
                return booking

        raise HTTPException(
            status_code=404,
            detail={
                "error": "booking_not_found_in_session",
                "message": "Seat booking was not found in this booking session.",
            },
        )

    async def _ensure_confirmed_booking_session_seat_cancellable(
        self,
        *,
        booking_session: BookingSession,
        booking: TripBooking,
    ) -> None:
        trip = await self._get_trip_obj_for_booking_update(
            booking_session.scheduled_trip_id
        )

        if trip.actual_start_at is not None or trip.planned_start_at <= utcnow():
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "trip_already_started",
                    "message": "Seat can only be cancelled before the trip starts.",
                },
            )

        if booking.booking_status == BookingStatus.CANCELLED:
            return

        if booking.booking_status != BookingStatus.BOOKED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "seat_booking_not_cancellable",
                    "message": "Only booked seats can be cancelled through this endpoint.",
                    "booking_status": booking.booking_status.value
                    if hasattr(booking.booking_status, "value")
                    else str(booking.booking_status),
                },
            )
        
    async def _get_existing_active_booking_seat_refund_request(
        self,
        *,
        booking_id: str,
    ) -> BookingSeatRefundRequest | None:
        stmt = (
            select(BookingSeatRefundRequest)
            .where(
                BookingSeatRefundRequest.booking_id == booking_id,
                BookingSeatRefundRequest.status.in_(
                    (
                        BookingSeatRefundRequestStatus.PENDING,
                        BookingSeatRefundRequestStatus.PROCESSING,
                        BookingSeatRefundRequestStatus.SUCCEEDED,
                    )
                ),
            )
            .order_by(BookingSeatRefundRequest.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()
    
    async def _ensure_booking_seat_refund_request(
        self,
        *,
        booking_session: BookingSession,
        booking: TripBooking,
        payment: BookingSessionPayment,
    ) -> BookingSeatRefundRequest:
        existing_request = await self._get_existing_active_booking_seat_refund_request(
            booking_id=booking.id,
        )

        if existing_request is not None:
            return existing_request

        now = utcnow()
        refund_amount = self._quantize_money(Decimal(booking.fare_amount or 0))

        if refund_amount <= Decimal("0.00"):
            refund_request = BookingSeatRefundRequest(
                booking_session_id=booking_session.id,
                booking_id=booking.id,
                booking_session_payment_id=payment.id,
                owner_user_id=booking_session.owner_user_id,
                amount=Decimal("0.00"),
                status=BookingSeatRefundRequestStatus.SKIPPED,
                failure_reason="Seat fare amount is zero, so refund is not required.",
                attempt_count=0,
                retry_after=None,
                requested_at=now,
                processed_at=now,
            )
            self.db.add(refund_request)
            await self.db.flush()
            return refund_request

        refund_request = BookingSeatRefundRequest(
            booking_session_id=booking_session.id,
            booking_id=booking.id,
            booking_session_payment_id=payment.id,
            owner_user_id=booking_session.owner_user_id,
            amount=refund_amount,
            status=BookingSeatRefundRequestStatus.PENDING,
            attempt_count=0,
            retry_after=now,
            requested_at=now,
        )

        self.db.add(refund_request)
        await self.db.flush()
        return refund_request
    
    async def _sync_booking_session_status_after_seat_cancellation(
        self,
        *,
        booking_session: BookingSession,
        bookings: list[TripBooking],
    ) -> None:
        active_statuses = {
            BookingStatus.PENDING_PAYMENT,
            BookingStatus.BOOKED,
            BookingStatus.BOARDED,
        }

        has_active_booking = any(
            booking.booking_status in active_statuses
            for booking in bookings
        )

        if has_active_booking:
            return

        if booking_session.status == BookingSessionStatus.CONFIRMED:
            booking_session.status = BookingSessionStatus.CANCELLED
            cancelled_booking = max(
                (item for item in bookings if item.cancelled_at is not None),
                key=lambda item: item.cancelled_at,
                default=None,
            )
            self._set_cancellation_metadata(
                booking_session,
                reason=(
                    getattr(cancelled_booking, "cancellation_reason", None)
                    or "All seats in the booking session were cancelled."
                ),
                source=(
                    getattr(cancelled_booking, "cancellation_source", None)
                    or "system"
                ),
                cancelled_by_user_id=getattr(
                    cancelled_booking, "cancelled_by_user_id", None
                ),
                cancelled_at=(
                    None if cancelled_booking is None else cancelled_booking.cancelled_at
                ),
            )
            self.db.add(booking_session)
            await self.db.flush()

    async def _get_route_obj(self, route_id: str) -> Route:
        stmt = (
            select(Route)
            .where(Route.id == route_id)
            .options(
                selectinload(Route.route_stops).selectinload(RouteStop.stop),
            )
        )
        result = await self.db.execute(stmt)
        route = result.scalar_one_or_none()
        if route is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "route_not_found", "message": "Route not found."},
            )
        return route

    async def _get_trip_obj(self, trip_id: str) -> ScheduledTrip:
        stmt = (
            select(ScheduledTrip)
            .where(ScheduledTrip.id == trip_id)
            .options(
                selectinload(ScheduledTrip.route)
                .selectinload(Route.route_stops)
                .selectinload(RouteStop.stop),
                selectinload(ScheduledTrip.vehicle),
                selectinload(ScheduledTrip.driver),
                selectinload(ScheduledTrip.trip_events).selectinload(TripEvent.stop),
            )
        )
        result = await self.db.execute(stmt)
        trip = result.scalar_one_or_none()
        if trip is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "scheduled_trip_not_found",
                    "message": "Scheduled trip not found.",
                },
            )
        return trip
    
    async def _get_trip_obj_for_booking_update(self, trip_id: str) -> ScheduledTrip:
        stmt = (
            select(ScheduledTrip)
            .where(ScheduledTrip.id == trip_id)
            .with_for_update()
            .options(
                selectinload(ScheduledTrip.route)
                .selectinload(Route.route_stops)
                .selectinload(RouteStop.stop),
                selectinload(ScheduledTrip.vehicle),
                selectinload(ScheduledTrip.driver),
            )
        )
        result = await self.db.execute(stmt)
        trip = result.scalar_one_or_none()
        if trip is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "scheduled_trip_not_found",
                    "message": "Scheduled trip not found.",
                },
            )
        return trip

    async def _get_booking_obj(
        self,
        *,
        booking_id: str,
        passenger_user_id: str,
    ) -> TripBooking:
        stmt = (
            select(TripBooking)
            .where(
                TripBooking.id == booking_id,
                TripBooking.passenger_user_id == passenger_user_id,
            )
             .options(
                selectinload(TripBooking.pickup_stop),
                selectinload(TripBooking.dropoff_stop),
                selectinload(TripBooking.payments),
                selectinload(TripBooking.booking_session).selectinload(
                    BookingSession.payments
                ),
                selectinload(TripBooking.rating),
                selectinload(TripBooking.scan_events),
                selectinload(TripBooking.scheduled_trip)
                .selectinload(ScheduledTrip.route)
                .selectinload(Route.route_stops)
                .selectinload(RouteStop.stop),
                selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.vehicle),
                selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.driver),
                selectinload(TripBooking.scheduled_trip)
                .selectinload(ScheduledTrip.trip_events)
                .selectinload(TripEvent.stop),
            )
        )
        result = await self.db.execute(stmt)
        booking = result.scalar_one_or_none()
        if booking is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "booking_not_found", "message": "Booking not found."},
            )
        return booking

    @staticmethod
    def _current_trip_sql_filter(now: datetime):
        terminal_statuses = (
            ScheduledTripStatus.CANCELLED,
            ScheduledTripStatus.COMPLETED,
            ScheduledTripStatus.PREMATURE_END,
        )

        return and_(
            ScheduledTrip.planned_start_at <= now,
            ScheduledTrip.status.notin_(terminal_statuses),
            ScheduledTrip.actual_end_at.is_(None),
        )

    @staticmethod
    def _is_current_trip_for_passenger(
        trip: ScheduledTrip,
        now: datetime,
    ) -> bool:
        terminal_statuses = (
            ScheduledTripStatus.CANCELLED,
            ScheduledTripStatus.COMPLETED,
            ScheduledTripStatus.PREMATURE_END,
        )

        if trip.status in terminal_statuses:
            return False

        if trip.planned_start_at > now:
            return False

        if trip.actual_end_at is not None:
            return False

        return True

    def _is_current_booking_for_passenger(
        self,
        booking: TripBooking,
        now: datetime,
    ) -> bool:
        if booking.booking_status not in (
            BookingStatus.BOOKED,
            BookingStatus.BOARDED,
        ):
            return False

        if booking.completed_at is not None:
            return False

        return self._is_current_trip_for_passenger(
            booking.scheduled_trip,
            now,
        )

    async def _list_booking_session_current_trip_bookings(
        self,
        *,
        booking_session_id: str,
        owner_user_id: str,
    ) -> list[TripBooking]:
        now = utcnow()

        stmt = (
            select(TripBooking)
            .join(ScheduledTrip, ScheduledTrip.id == TripBooking.scheduled_trip_id)
            .where(
                TripBooking.booking_session_id == booking_session_id,
                TripBooking.passenger_user_id == owner_user_id,
                TripBooking.booking_status.in_(
                    (
                        BookingStatus.BOOKED,
                        BookingStatus.BOARDED,
                    )
                ),
                TripBooking.completed_at.is_(None),
                self._current_trip_sql_filter(now),
            )
            .options(
                selectinload(TripBooking.pickup_stop),
                selectinload(TripBooking.dropoff_stop),
                selectinload(TripBooking.payments),
                selectinload(TripBooking.rating),
                selectinload(TripBooking.scan_events),
                selectinload(TripBooking.scheduled_trip)
                .selectinload(ScheduledTrip.route)
                .selectinload(Route.route_stops)
                .selectinload(RouteStop.stop),
                selectinload(TripBooking.scheduled_trip).selectinload(
                    ScheduledTrip.vehicle
                ),
                selectinload(TripBooking.scheduled_trip).selectinload(
                    ScheduledTrip.driver
                ),
                selectinload(TripBooking.scheduled_trip)
                .selectinload(ScheduledTrip.trip_events)
                .selectinload(TripEvent.stop),
            )
            .order_by(
                TripBooking.seat_number.asc(),
                TripBooking.created_at.asc(),
            )
        )

        result = await self.db.execute(stmt)
        bookings = list(result.scalars().unique().all())

        if not bookings:
            await self._get_booking_session_obj(
                booking_session_id=booking_session_id,
                owner_user_id=owner_user_id,
            )

        return bookings

    async def _get_route_sequence_map(self, route_id: str) -> dict[str, RouteStop]:
        stmt = select(RouteStop).where(RouteStop.route_id == route_id)
        result = await self.db.execute(stmt)
        rows = result.scalars().all()
        return {row.stop_id: row for row in rows}

    async def _resolve_fare(
        self,
        *,
        route_id: str,
        pickup_stop_id: str,
        dropoff_stop_id: str,
    ) -> tuple[RouteFare, RouteStop, RouteStop]:
        sequence_map = await self._get_route_sequence_map(route_id)
        pickup_route_stop = sequence_map.get(pickup_stop_id)
        dropoff_route_stop = sequence_map.get(dropoff_stop_id)

        if pickup_route_stop is None or dropoff_route_stop is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "stops_not_on_route",
                    "message": "Pickup and dropoff stops must both belong to the route.",
                },
            )

        if pickup_stop_id == dropoff_stop_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "same_pickup_dropoff",
                    "message": "Pickup and dropoff stops must be different.",
                },
            )

        if pickup_route_stop.sequence_no >= dropoff_route_stop.sequence_no:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_stop_order",
                    "message": "Pickup stop must come before dropoff stop on the route.",
                },
            )

        if not pickup_route_stop.boarding_allowed:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "boarding_not_allowed",
                    "message": "Boarding is not allowed at the selected pickup stop.",
                },
            )

        if not dropoff_route_stop.deboarding_allowed:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "deboarding_not_allowed",
                    "message": "Deboarding is not allowed at the selected dropoff stop.",
                },
            )

        fare_stmt = select(RouteFare).where(
            RouteFare.route_id == route_id,
            RouteFare.pickup_stop_id == pickup_stop_id,
            RouteFare.dropoff_stop_id == dropoff_stop_id,
            RouteFare.is_active.is_(True),
        )
        fare_result = await self.db.execute(fare_stmt)
        fare = fare_result.scalar_one_or_none()

        if fare is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "fare_not_found",
                    "message": "No active fare exists for the selected route segment.",
                },
            )

        return fare, pickup_route_stop, dropoff_route_stop

    async def _count_active_trip_bookings(self, scheduled_trip_id: str) -> int:
        current_time = utcnow()

        stmt = select(func.count(TripBooking.id)).where(
            TripBooking.scheduled_trip_id == scheduled_trip_id,
            or_(
                TripBooking.booking_status.in_(
                    (BookingStatus.BOOKED, BookingStatus.BOARDED)
                ),
                and_(
                    TripBooking.booking_status == BookingStatus.PENDING_PAYMENT,
                    or_(
                        TripBooking.payment_hold_expires_at.is_(None),
                        TripBooking.payment_hold_expires_at > current_time,
                    ),
                ),
            ),
        )
        result = await self.db.execute(stmt)
        return int(result.scalar_one() or 0)
    
    async def _count_overlapping_active_trip_bookings(
        self,
        *,
        scheduled_trip_id: str,
        pickup_sequence_no: int,
        dropoff_sequence_no: int,
    ) -> int:
        current_time = utcnow()

        stmt = select(func.count(TripBooking.id)).where(
            TripBooking.scheduled_trip_id == scheduled_trip_id,
            TripBooking.pickup_sequence_no_snapshot < dropoff_sequence_no,
            TripBooking.dropoff_sequence_no_snapshot > pickup_sequence_no,
            or_(
                TripBooking.booking_status.in_(
                    (BookingStatus.BOOKED, BookingStatus.BOARDED)
                ),
                and_(
                    TripBooking.booking_status == BookingStatus.PENDING_PAYMENT,
                    or_(
                        TripBooking.payment_hold_expires_at.is_(None),
                        TripBooking.payment_hold_expires_at > current_time,
                    ),
                ),
            ),
        )
        result = await self.db.execute(stmt)
        return int(result.scalar_one() or 0)
    
    async def _get_occupied_app_seat_numbers_for_leg(
        self,
        *,
        scheduled_trip_id: str,
        pickup_sequence_no: int,
        dropoff_sequence_no: int,
    ) -> set[int]:
        current_time = utcnow()

        stmt = select(TripBooking.seat_number).where(
            TripBooking.scheduled_trip_id == scheduled_trip_id,
            TripBooking.pickup_sequence_no_snapshot < dropoff_sequence_no,
            TripBooking.dropoff_sequence_no_snapshot > pickup_sequence_no,
            or_(
                TripBooking.booking_status.in_(
                    (BookingStatus.BOOKED, BookingStatus.BOARDED)
                ),
                and_(
                    TripBooking.booking_status == BookingStatus.PENDING_PAYMENT,
                    or_(
                        TripBooking.payment_hold_expires_at.is_(None),
                        TripBooking.payment_hold_expires_at > current_time,
                    ),
                ),
            ),
        )

        result = await self.db.execute(stmt)

        return {
            int(seat_number)
            for seat_number in result.scalars().all()
            if seat_number is not None
        }

    async def _ensure_requested_seat_available_for_leg(
        self,
        *,
        scheduled_trip_id: str,
        seat_number: int,
        seat_capacity: int,
        pickup_sequence_no: int,
        dropoff_sequence_no: int,
    ) -> None:
        if seat_number < 1 or seat_number > seat_capacity:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_seat_number",
                    "message": "Selected seat is outside the app-bookable seat range for this trip.",
                    "seat_number": seat_number,
                    "seat_capacity": seat_capacity,
                },
            )

        occupied_seat_numbers = await self._get_occupied_app_seat_numbers_for_leg(
            scheduled_trip_id=scheduled_trip_id,
            pickup_sequence_no=pickup_sequence_no,
            dropoff_sequence_no=dropoff_sequence_no,
        )

        if seat_number in occupied_seat_numbers:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "seat_unavailable",
                    "message": "Selected seat is already occupied for the selected route segment.",
                    "seat_number": seat_number,
                },
            )
    
    async def _is_driver_rfid_seat_reservation_enabled(self) -> bool:
        stmt = (
            select(PlatformSettings.allow_driver_rfid_seat_reservation)
            .where(PlatformSettings.settings_key == "default")
            .limit(1)
        )
        result = await self.db.execute(stmt)
        value = result.scalar_one_or_none()

        return True if value is None else bool(value)

    async def _get_app_bookable_capacity_for_trip(
        self,
        trip: ScheduledTrip,
    ) -> int:
        vehicle_capacity = (
            int(trip.vehicle.seat_count or 0)
            if trip.vehicle is not None
            else 0
        )

        if not await self._is_driver_rfid_seat_reservation_enabled():
            return max(vehicle_capacity, 0)

        rfid_reserved_capacity = int(trip.rfid_reserved_seat_count or 0)

        return max(vehicle_capacity - rfid_reserved_capacity, 0)

    async def _get_rfid_capacity_for_in_progress_discovery(
        self,
        *,
        trip: ScheduledTrip,
    ) -> dict[str, Any]:
        use_fixed_rfid_reserved_seats = (
            await self._is_driver_rfid_seat_reservation_enabled()
        )

        if not use_fixed_rfid_reserved_seats:
            return {
                "rfid_seat_policy": "crew_managed",
                "rfid_physical_seat_check_required": True,
                "rfid_reserved_seat_count": 0,
                "rfid_occupied_seat_count": 0,
                "rfid_available_seat_count": 0,
                "rfid_seat_available": True,
            }

        reserved_capacity = self._get_rfid_reserved_capacity_for_trip(trip)

        open_rfid_ride_count = await self._count_open_rfid_rides_overlapping_leg(
            scheduled_trip_id=trip.id,
            dropoff_sequence_no=10**9,
        )

        available_seat_count = max(
            reserved_capacity - open_rfid_ride_count,
            0,
        )

        return {
            "rfid_seat_policy": "reserved_pool",
            "rfid_physical_seat_check_required": False,
            "rfid_reserved_seat_count": reserved_capacity,
            "rfid_occupied_seat_count": open_rfid_ride_count,
            "rfid_available_seat_count": available_seat_count,
            "rfid_seat_available": available_seat_count > 0,
        }

    def _serialize_stop_brief(self, stop: Stop) -> dict[str, Any]:
        return {
            "id": stop.id,
            "name": stop.name,
            "lat": stop.lat,
            "lng": stop.lng,
            "radius_meters": stop.radius_meters,
            "is_active": stop.is_active,
        }

    def _serialize_route(self, route: Route) -> dict[str, Any]:
        return {
            "id": route.id,
            "name": route.name,
            "code": route.code,
            "has_ac": route.has_ac,
            "is_active": route.is_active,
            "stops": [
                {
                    "route_stop_id": route_stop.id,
                    "sequence_no": route_stop.sequence_no,
                    "assume_time_diff_minutes": route_stop.assume_time_diff_minutes,
                    "boarding_allowed": route_stop.boarding_allowed,
                    "deboarding_allowed": route_stop.deboarding_allowed,
                    "stop": self._serialize_stop_brief(route_stop.stop),
                }
                for route_stop in sorted(route.route_stops, key=lambda item: item.sequence_no)
            ],
        }
    
    def _build_trip_event_map(self, trip: ScheduledTrip) -> dict[str, TripEvent]:
        return {event.stop_id: event for event in trip.trip_events}
    
    async def _serialize_trip(self, trip: ScheduledTrip) -> dict[str, Any]:
        available_seats = None
        sorted_route_stops = sorted(trip.route.route_stops, key=lambda item: item.sequence_no)

        trip_from_stop = (
            self._serialize_stop_brief(sorted_route_stops[0].stop)
            if sorted_route_stops
            else None
        )
        trip_to_stop = (
            self._serialize_stop_brief(sorted_route_stops[-1].stop)
            if sorted_route_stops
            else None
        )

        return {
            "id": trip.id,
            "route_id": trip.route_id,
            "driver_user_id": trip.driver_user_id,
            "vehicle_id": trip.vehicle_id,
            "planned_start_at": trip.planned_start_at,
            "planned_end_at": trip.planned_end_at,
            "actual_start_at": trip.actual_start_at,
            "actual_end_at": trip.actual_end_at,
            "status": trip.status,
            "admin_note": trip.admin_note,
            "cancellation_metadata": self._serialize_cancellation_metadata(trip),
            "available_seats": available_seats,
            "trip_from_stop": trip_from_stop,
            "trip_to_stop": trip_to_stop,
            "stops": self._serialize_trip_stops(trip),
            "route": self._serialize_route(trip.route),
            "vehicle": None if trip.vehicle is None else {
                "id": trip.vehicle.id,
                "registration_number": trip.vehicle.registration_number,
                "vehicle_name": trip.vehicle.vehicle_name,
                "vehicle_model": trip.vehicle.vehicle_model,
                "color": trip.vehicle.color,
                "seat_count": trip.vehicle.seat_count,
                "rfid_reserved_seat_count": trip.rfid_reserved_seat_count,
                "app_bookable_seat_count": await self._get_app_bookable_capacity_for_trip(trip),
                "has_ac": trip.vehicle.has_ac,
            },
            "driver": {
                "id": trip.driver.id,
                "email": trip.driver.email,
            } if trip.driver is not None else None,
        }
    
    async def _serialize_booking_detail(self, booking: TripBooking) -> dict[str, Any]:
        tax_fields = self._booking_tax_fields(booking)
        return {
            "id": booking.id,
            "passenger_user_id": booking.passenger_user_id,
            "booking_session_id": booking.booking_session_id,
            "booked_by_user_id": booking.booked_by_user_id,
            "traveller_profile_id": booking.traveller_profile_id,
            "traveller_name_snapshot": booking.traveller_name_snapshot,
            "traveller_phone_snapshot": booking.traveller_phone_snapshot,
            "traveller_email_snapshot": booking.traveller_email_snapshot,
            "traveller_relationship_label_snapshot": booking.traveller_relationship_label_snapshot,
            "scheduled_trip_id": booking.scheduled_trip_id,
            "route_id": booking.route_id,
            "pickup_stop_id": booking.pickup_stop_id,
            "dropoff_stop_id": booking.dropoff_stop_id,
            "seat_number": booking.seat_number,
            "otp": self._serialize_booking_otp(booking),
            "booking_status": booking.booking_status,
            "fare_amount": booking.fare_amount,
            **tax_fields,
            "payment_hold_expires_at": booking.payment_hold_expires_at,
            "commission_percent_snapshot": booking.commission_percent_snapshot,
            "commission_amount": booking.commission_amount,
            "driver_payout_amount": booking.driver_payout_amount,
            "transfer_status": booking.transfer_status,
            "transfer_ready_at": booking.transfer_ready_at,
            "transfer_processed_at": booking.transfer_processed_at,
            "boarded_at": booking.boarded_at,
            "completed_at": booking.completed_at,
            "cancelled_at": booking.cancelled_at,
            "cancellation_metadata": self._serialize_cancellation_metadata(booking),
            "pickup_stop": self._serialize_stop_brief(booking.pickup_stop),
            "dropoff_stop": self._serialize_stop_brief(booking.dropoff_stop),
            "scheduled_trip": await self._serialize_trip(booking.scheduled_trip),
            "payments": [self._serialize_payment(payment) for payment in booking.payments],
            "rating": self._serialize_rating(booking.rating),
            "created_at": booking.created_at,
            "updated_at": booking.updated_at,
        }
    
    @staticmethod
    def _generate_invoice_number(booking: TripBooking) -> str:
        reference_time = booking.completed_at or booking.updated_at or booking.created_at
        return f"INV-{reference_time.strftime('%Y%m%d')}-{booking.id[:8].upper()}"

    def _get_latest_paid_invoice_payment(
        self,
        booking: TripBooking,
    ) -> BookingPayment | BookingSessionPayment | None:
        paid_payments = [
            payment
            for payment in booking.payments
            if payment.status == BookingPaymentStatus.PAID and payment.razorpay_payment_id
        ]
        if booking.booking_session is not None:
            paid_payments.extend(
                payment
                for payment in booking.booking_session.payments
                if payment.status == BookingPaymentStatus.PAID
                and payment.razorpay_payment_id
            )
        if not paid_payments:
            return None

        paid_payments.sort(key=lambda item: item.created_at, reverse=True)
        return paid_payments[0]

    def _serialize_invoice_payment(
        self,
        booking: TripBooking,
        payment: BookingPayment | BookingSessionPayment,
    ) -> dict[str, Any]:
        if isinstance(payment, BookingPayment):
            return self._serialize_payment(payment)

        tax_fields = self._booking_tax_fields(booking)
        return {
            "id": payment.id,
            "booking_id": booking.id,
            "razorpay_order_id": payment.razorpay_order_id,
            "razorpay_payment_id": payment.razorpay_payment_id,
            "status": payment.status,
            "amount": booking.fare_amount,
            "taxable_amount": tax_fields["taxable_amount"],
            "cgst_amount": tax_fields["cgst_amount"],
            "sgst_amount": tax_fields["sgst_amount"],
            "igst_amount": tax_fields["igst_amount"],
            "total_tax_amount": tax_fields["total_tax_amount"],
            "created_at": payment.created_at,
            "updated_at": payment.updated_at,
        }

    def _build_invoice_breakdown(
        self,
        *,
        total_booking_amount: Decimal,
        is_ac: bool,
        booking: TripBooking | None = None,
    ) -> dict[str, Any]:
        total_booking_amount = self._quantize_money(Decimal(total_booking_amount))

        if booking is not None:
            tax_fields = self._booking_tax_fields(booking)
            taxable_value = tax_fields["taxable_amount"]
            cgst_rate_percent = tax_fields["cgst_rate_percent_snapshot"]
            cgst_amount = tax_fields["cgst_amount"]
            sgst_rate_percent = tax_fields["sgst_rate_percent_snapshot"]
            sgst_amount = tax_fields["sgst_amount"]
            igst_rate_percent = tax_fields["igst_rate_percent_snapshot"]
            igst_amount = tax_fields["igst_amount"]
            total_tax_amount = tax_fields["total_tax_amount"]
            gst_inclusive = tax_fields["gst_inclusive_snapshot"]
            divisor_used = (
                Decimal("1.00")
                + (
                    cgst_rate_percent
                    + sgst_rate_percent
                    + igst_rate_percent
                )
                / Decimal("100")
            )
            divisor_used = divisor_used.quantize(
                Decimal("0.0001"),
                rounding=ROUND_HALF_UP,
            )
        elif is_ac:
            cgst_rate_percent = Decimal("2.50")
            sgst_rate_percent = Decimal("2.50")
            igst_rate_percent = Decimal("0.00")
            divisor_used = Decimal("1.0500")
            taxable_value = self._quantize_money(total_booking_amount / divisor_used)
            cgst_amount = self._quantize_money(
                (taxable_value * cgst_rate_percent) / Decimal("100")
            )
            sgst_amount = self._quantize_money(
                (taxable_value * sgst_rate_percent) / Decimal("100")
            )
            igst_amount = Decimal("0.00")
            total_tax_amount = self._quantize_money(
                cgst_amount + sgst_amount + igst_amount
            )
            gst_inclusive = True
        else:
            cgst_rate_percent = Decimal("0.00")
            sgst_rate_percent = Decimal("0.00")
            igst_rate_percent = Decimal("0.00")
            divisor_used = Decimal("1.0000")
            taxable_value = total_booking_amount
            cgst_amount = Decimal("0.00")
            sgst_amount = Decimal("0.00")
            igst_amount = Decimal("0.00")
            total_tax_amount = Decimal("0.00")
            gst_inclusive = True

        recomputed_total_amount = self._quantize_money(
            taxable_value + total_tax_amount
        )
        rounding_adjustment = self._quantize_money(
            total_booking_amount - recomputed_total_amount
        )

        return {
            "total_booking_amount": total_booking_amount,
            "divisor_used": divisor_used,
            "taxable_value": taxable_value,
            "cgst_rate_percent": cgst_rate_percent,
            "cgst_amount": cgst_amount,
            "sgst_rate_percent": sgst_rate_percent,
            "sgst_amount": sgst_amount,
            "igst_rate_percent": igst_rate_percent,
            "igst_amount": igst_amount,
            "total_tax_amount": total_tax_amount,
            "gst_inclusive": gst_inclusive,
            "recomputed_total_amount": recomputed_total_amount,
            "rounding_adjustment": rounding_adjustment,
        }

    def _serialize_trip_stops(self, trip: ScheduledTrip) -> list[dict[str, Any]]:
        sorted_route_stops = sorted(trip.route.route_stops, key=lambda item: item.sequence_no)
        trip_event_map = self._build_trip_event_map(trip)

        items: list[dict[str, Any]] = []
        cumulative_minutes = 0

        for index, route_stop in enumerate(sorted_route_stops):
            if index == 0:
                cumulative_minutes = 0
            else:
                cumulative_minutes += max(int(route_stop.assume_time_diff_minutes or 0), 0)

            planned_time_at_stop = trip.planned_start_at + timedelta(minutes=cumulative_minutes)
            trip_event = trip_event_map.get(route_stop.stop_id)

            items.append(
                {
                    "route_stop_id": route_stop.id,
                    "sequence_no": route_stop.sequence_no,
                    "assume_time_diff_minutes": route_stop.assume_time_diff_minutes,
                    "minutes_from_trip_start": cumulative_minutes,
                    "planned_time_at_stop": planned_time_at_stop,
                    "actual_arrival_time": None if trip_event is None else trip_event.arrival_time,
                    "actual_departure_time": None if trip_event is None else trip_event.departure_time,
                    "boarding_allowed": route_stop.boarding_allowed,
                    "deboarding_allowed": route_stop.deboarding_allowed,
                    "stop": self._serialize_stop_brief(route_stop.stop),
                }
            )

        return items

    def _serialize_payment(self, payment: BookingPayment) -> dict[str, Any]:
        return {
            "id": payment.id,
            "booking_id": payment.booking_id,
            "razorpay_order_id": payment.razorpay_order_id,
            "razorpay_payment_id": payment.razorpay_payment_id,
            "status": payment.status,
            "amount": payment.amount,
            "taxable_amount": self._quantize_money(
                Decimal(getattr(payment, "taxable_amount", 0) or 0)
            ),
            "cgst_amount": self._quantize_money(
                Decimal(getattr(payment, "cgst_amount", 0) or 0)
            ),
            "sgst_amount": self._quantize_money(
                Decimal(getattr(payment, "sgst_amount", 0) or 0)
            ),
            "igst_amount": self._quantize_money(
                Decimal(getattr(payment, "igst_amount", 0) or 0)
            ),
            "total_tax_amount": self._quantize_money(
                Decimal(getattr(payment, "total_tax_amount", 0) or 0)
            ),
            "created_at": payment.created_at,
            "updated_at": payment.updated_at,
        }

    def _serialize_rating(self, rating: BookingRating | None) -> dict[str, Any] | None:
        if rating is None:
            return None
        return {
            "id": rating.id,
            "booking_id": rating.booking_id,
            "trip_rating": rating.trip_rating,
            "driver_rating": rating.driver_rating,
            "review_text": rating.review_text,
            "created_at": rating.created_at,
            "updated_at": rating.updated_at,
        }

    def _serialize_booking(self, booking: TripBooking) -> dict[str, Any]:
        tax_fields = self._booking_tax_fields(booking)
        return {
            "id": booking.id,
            "passenger_user_id": booking.passenger_user_id,
            "booking_session_id": booking.booking_session_id,
            "booked_by_user_id": booking.booked_by_user_id,
            "traveller_profile_id": booking.traveller_profile_id,
            "traveller_name_snapshot": booking.traveller_name_snapshot,
            "traveller_phone_snapshot": booking.traveller_phone_snapshot,
            "traveller_email_snapshot": booking.traveller_email_snapshot,
            "traveller_relationship_label_snapshot": booking.traveller_relationship_label_snapshot,
            "scheduled_trip_id": booking.scheduled_trip_id,
            "route_id": booking.route_id,
            "pickup_stop_id": booking.pickup_stop_id,
            "dropoff_stop_id": booking.dropoff_stop_id,
            "seat_number": booking.seat_number,
            "otp": self._serialize_booking_otp(booking),
            "booking_status": booking.booking_status,
            "fare_amount": booking.fare_amount,
            **tax_fields,
            "payment_hold_expires_at": booking.payment_hold_expires_at,
            "commission_percent_snapshot": booking.commission_percent_snapshot,
            "commission_amount": booking.commission_amount,
            "driver_payout_amount": booking.driver_payout_amount,
            "transfer_status": booking.transfer_status,
            "transfer_ready_at": booking.transfer_ready_at,
            "transfer_processed_at": booking.transfer_processed_at,
            "boarded_at": booking.boarded_at,
            "completed_at": booking.completed_at,
            "cancelled_at": booking.cancelled_at,
            "cancellation_metadata": self._serialize_cancellation_metadata(booking),
            "pickup_stop": self._serialize_stop_brief(booking.pickup_stop),
            "dropoff_stop": self._serialize_stop_brief(booking.dropoff_stop),
            "payments": [self._serialize_payment(payment) for payment in booking.payments],
            "rating": self._serialize_rating(booking.rating),
            "created_at": booking.created_at,
            "updated_at": booking.updated_at,
        }
    
    def _serialize_booking_seat_refund_request(
        self,
        refund_request: BookingSeatRefundRequest | None,
    ) -> dict[str, Any] | None:
        if refund_request is None:
            return None

        return {
            "id": refund_request.id,
            "booking_session_id": refund_request.booking_session_id,
            "booking_id": refund_request.booking_id,
            "booking_session_payment_id": refund_request.booking_session_payment_id,
            "owner_user_id": refund_request.owner_user_id,
            "amount": refund_request.amount,
            "status": refund_request.status,
            "razorpay_refund_id": refund_request.razorpay_refund_id,
            "failure_reason": refund_request.failure_reason,
            "attempt_count": refund_request.attempt_count,
            "retry_after": refund_request.retry_after,
            "requested_at": refund_request.requested_at,
            "processed_at": refund_request.processed_at,
            "created_at": refund_request.created_at,
            "updated_at": refund_request.updated_at,
        }
    
    def _serialize_booking_session_payment(
        self,
        payment: BookingSessionPayment,
    ) -> dict[str, Any]:
        return {
            "id": payment.id,
            "booking_session_id": payment.booking_session_id,
            "razorpay_order_id": payment.razorpay_order_id,
            "razorpay_payment_id": payment.razorpay_payment_id,
            "razorpay_refund_id": payment.razorpay_refund_id,
            "status": payment.status,
            "effective_status": self._get_booking_session_payment_effective_status(
                payment
            ),
            "amount": payment.amount,
            "taxable_amount": self._quantize_money(
                Decimal(getattr(payment, "taxable_amount", 0) or 0)
            ),
            "cgst_amount": self._quantize_money(
                Decimal(getattr(payment, "cgst_amount", 0) or 0)
            ),
            "sgst_amount": self._quantize_money(
                Decimal(getattr(payment, "sgst_amount", 0) or 0)
            ),
            "igst_amount": self._quantize_money(
                Decimal(getattr(payment, "igst_amount", 0) or 0)
            ),
            "total_tax_amount": self._quantize_money(
                Decimal(getattr(payment, "total_tax_amount", 0) or 0)
            ),
            "refunded_amount": payment.refunded_amount,
            "refund_requested_at": payment.refund_requested_at,
            "refund_processed_at": payment.refund_processed_at,
            "refund_retry_after": payment.refund_retry_after,
            "refund_attempt_count": payment.refund_attempt_count,
            "refund_failure_reason": payment.refund_failure_reason,
            "created_at": payment.created_at,
            "updated_at": payment.updated_at,
        }

    def _serialize_booking_session_seat(
        self,
        booking: TripBooking,
        *,
        refund_requests_by_booking_id: dict[str, BookingSeatRefundRequest] | None = None,
    ) -> dict[str, Any]:
        refund_request = None
        if refund_requests_by_booking_id is not None:
            refund_request = refund_requests_by_booking_id.get(booking.id)
        tax_fields = self._booking_tax_fields(booking)
        return {
            "id": booking.id,
            "booking_session_id": booking.booking_session_id,
            "passenger_user_id": booking.passenger_user_id,
            "booked_by_user_id": booking.booked_by_user_id,
            "traveller_profile_id": booking.traveller_profile_id,
            "traveller_name_snapshot": booking.traveller_name_snapshot,
            "traveller_phone_snapshot": booking.traveller_phone_snapshot,
            "traveller_email_snapshot": booking.traveller_email_snapshot,
            "traveller_relationship_label_snapshot": booking.traveller_relationship_label_snapshot,
            "scheduled_trip_id": booking.scheduled_trip_id,
            "route_id": booking.route_id,
            "pickup_stop_id": booking.pickup_stop_id,
            "dropoff_stop_id": booking.dropoff_stop_id,
            "seat_number": booking.seat_number,
            "otp": self._serialize_booking_otp(booking),
            "booking_status": booking.booking_status,
            "fare_amount": booking.fare_amount,
            **tax_fields,
            "payment_hold_expires_at": booking.payment_hold_expires_at,
            "cancellation_metadata": self._serialize_cancellation_metadata(booking),
            "refund": self._serialize_booking_seat_refund_request(
                refund_request
            ),
            "created_at": booking.created_at,
            "updated_at": booking.updated_at,
        }

    def _serialize_booking_session(
        self,
        booking_session: BookingSession,
        *,
        refund_requests_by_booking_id: dict[str, BookingSeatRefundRequest] | None = None,
    ) -> dict[str, Any]:
        bookings = sorted(
            list(booking_session.bookings),
            key=lambda item: item.seat_number,
        )
        payments = sorted(
            list(booking_session.payments),
            key=lambda item: item.created_at,
            reverse=True,
        )

        return {
            "id": booking_session.id,
            "owner_user_id": booking_session.owner_user_id,
            "scheduled_trip_id": booking_session.scheduled_trip_id,
            "route_id": booking_session.route_id,
            "pickup_stop_id": booking_session.pickup_stop_id,
            "dropoff_stop_id": booking_session.dropoff_stop_id,
            "pickup_sequence_no_snapshot": booking_session.pickup_sequence_no_snapshot,
            "dropoff_sequence_no_snapshot": booking_session.dropoff_sequence_no_snapshot,
            "status": booking_session.status,
            "total_fare_amount": booking_session.total_fare_amount,
            "total_taxable_amount": self._quantize_money(
                Decimal(getattr(booking_session, "total_taxable_amount", 0) or 0)
            ),
            "total_cgst_amount": self._quantize_money(
                Decimal(getattr(booking_session, "total_cgst_amount", 0) or 0)
            ),
            "total_sgst_amount": self._quantize_money(
                Decimal(getattr(booking_session, "total_sgst_amount", 0) or 0)
            ),
            "total_igst_amount": self._quantize_money(
                Decimal(getattr(booking_session, "total_igst_amount", 0) or 0)
            ),
            "total_tax_amount": self._quantize_money(
                Decimal(getattr(booking_session, "total_tax_amount", 0) or 0)
            ),
            "gst_enabled_snapshot": bool(
                getattr(booking_session, "gst_enabled_snapshot", False)
            ),
            "gst_inclusive_snapshot": bool(
                getattr(booking_session, "gst_inclusive_snapshot", True)
            ),
            "cgst_rate_percent_snapshot": Decimal(
                getattr(booking_session, "cgst_rate_percent_snapshot", 0) or 0
            ),
            "sgst_rate_percent_snapshot": Decimal(
                getattr(booking_session, "sgst_rate_percent_snapshot", 0) or 0
            ),
            "igst_rate_percent_snapshot": Decimal(
                getattr(booking_session, "igst_rate_percent_snapshot", 0) or 0
            ),
            "payment_hold_expires_at": booking_session.payment_hold_expires_at,
            "confirmed_at": booking_session.confirmed_at,
            "cancelled_at": booking_session.cancelled_at,
            "cancellation_metadata": self._serialize_cancellation_metadata(
                booking_session
            ),
            "expired_at": booking_session.expired_at,
            "bookings": [
                self._serialize_booking_session_seat(
                    booking,
                    refund_requests_by_booking_id=refund_requests_by_booking_id,
                )
                for booking in bookings
            ],
            "payments": [
                self._serialize_booking_session_payment(payment)
                for payment in payments
            ],
            "created_at": booking_session.created_at,
            "updated_at": booking_session.updated_at,
        }
    
    async def _serialize_booking_session_with_refunds(
        self,
        booking_session: BookingSession,
    ) -> dict[str, Any]:
        refund_requests_by_booking_id = (
            await self._get_latest_booking_seat_refund_requests_by_booking_id(
                booking_session_ids=[booking_session.id],
            )
        )

        return self._serialize_booking_session(
            booking_session,
            refund_requests_by_booking_id=refund_requests_by_booking_id,
        )

    async def _serialize_booking_sessions_with_refunds(
        self,
        booking_sessions: list[BookingSession],
    ) -> list[dict[str, Any]]:
        refund_requests_by_booking_id = (
            await self._get_latest_booking_seat_refund_requests_by_booking_id(
                booking_session_ids=[
                    booking_session.id
                    for booking_session in booking_sessions
                ],
            )
        )

        return [
            self._serialize_booking_session(
                booking_session,
                refund_requests_by_booking_id=refund_requests_by_booking_id,
            )
            for booking_session in booking_sessions
        ]

    @staticmethod
    def _get_booking_session_payment_effective_status(
        payment: BookingSessionPayment,
    ) -> str:
        if payment.status == BookingPaymentStatus.REFUNDED:
            return "refunded"

        if (
            payment.status == BookingPaymentStatus.PAID
            and payment.refund_requested_at is not None
            and payment.refund_processed_at is None
        ):
            return "refund_pending"

        return payment.status.value
    
    def _get_paid_booking_session_payment(
        self,
        payments: list[BookingSessionPayment],
    ) -> BookingSessionPayment | None:
        paid_payments = [
            payment
            for payment in payments
            if payment.status == BookingPaymentStatus.PAID
        ]

        if not paid_payments:
            return None

        paid_payments.sort(key=lambda item: item.created_at, reverse=True)
        return paid_payments[0]
    
    async def _ensure_confirmed_booking_session_cancellable(
        self,
        *,
        booking_session: BookingSession,
        bookings: list[TripBooking],
    ) -> None:
        trip = await self._get_trip_obj_for_booking_update(
            booking_session.scheduled_trip_id
        )

        if trip.actual_start_at is not None or trip.planned_start_at <= utcnow():
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "trip_already_started",
                    "message": "Confirmed booking sessions can only be cancelled before the trip starts.",
                },
            )

        non_cancellable_bookings = [
            booking
            for booking in bookings
            if booking.booking_status
            not in (
                BookingStatus.BOOKED,
                BookingStatus.CANCELLED,
            )
        ]

        if non_cancellable_bookings:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "booking_session_contains_non_cancellable_seats",
                    "message": "This booking session contains seats that can no longer be cancelled through whole-session cancellation.",
                    "booking_ids": [
                        booking.id
                        for booking in non_cancellable_bookings
                    ],
                },
            )

    async def _cancel_confirmed_booking_session_and_request_refund(
        self,
        *,
        booking_session: BookingSession,
        bookings: list[TripBooking],
        payment: BookingSessionPayment,
        cancelled_by_user_id: str,
    ) -> None:
        now = utcnow()

        booking_session.status = BookingSessionStatus.CANCELLED
        self._set_cancellation_metadata(
            booking_session,
            reason="Confirmed booking session cancelled by passenger.",
            source="passenger",
            cancelled_by_user_id=cancelled_by_user_id,
            cancelled_at=now,
        )
        booking_session.payment_hold_expires_at = None
        self.db.add(booking_session)

        for booking in bookings:
            if booking.booking_status == BookingStatus.BOOKED:
                booking.booking_status = BookingStatus.CANCELLED
                self._set_cancellation_metadata(
                    booking,
                    reason="Confirmed booking session cancelled by passenger.",
                    source="passenger",
                    cancelled_by_user_id=cancelled_by_user_id,
                    cancelled_at=now,
                )
                booking.payment_hold_expires_at = None
                booking.refund_retry_after = booking.refund_retry_after or now
                booking.refund_attempt_count = booking.refund_attempt_count or 0
                self.db.add(booking)

        payment.refund_requested_at = payment.refund_requested_at or now
        payment.refund_retry_after = payment.refund_retry_after or now
        payment.refund_attempt_count = payment.refund_attempt_count or 0
        payment.refund_failure_reason = None
        self.db.add(payment)

        await self.db.flush()
    
    async def _serialize_current_booking(self, booking: TripBooking) -> dict[str, Any]:
        tax_fields = self._booking_tax_fields(booking)
        return {
            "id": booking.id,
            "passenger_user_id": booking.passenger_user_id,
            "booking_session_id": booking.booking_session_id,
            "booked_by_user_id": booking.booked_by_user_id,
            "traveller_profile_id": booking.traveller_profile_id,
            "traveller_name_snapshot": booking.traveller_name_snapshot,
            "traveller_phone_snapshot": booking.traveller_phone_snapshot,
            "traveller_email_snapshot": booking.traveller_email_snapshot,
            "traveller_relationship_label_snapshot": booking.traveller_relationship_label_snapshot,
            "scheduled_trip_id": booking.scheduled_trip_id,
            "route_id": booking.route_id,
            "pickup_stop_id": booking.pickup_stop_id,
            "dropoff_stop_id": booking.dropoff_stop_id,
            "seat_number": booking.seat_number,
            "otp": self._serialize_booking_otp(booking),
            "booking_status": booking.booking_status,
            "fare_amount": booking.fare_amount,
            **tax_fields,
            "payment_hold_expires_at": booking.payment_hold_expires_at,
            "boarded_at": booking.boarded_at,
            "completed_at": booking.completed_at,
            "cancelled_at": booking.cancelled_at,
            "cancellation_metadata": self._serialize_cancellation_metadata(booking),
            "pickup_stop": self._serialize_stop_brief(booking.pickup_stop),
            "dropoff_stop": self._serialize_stop_brief(booking.dropoff_stop),
            "scheduled_trip": await self._serialize_trip(booking.scheduled_trip),
            "created_at": booking.created_at,
            "updated_at": booking.updated_at,
        }
    
    def _get_sorted_route_stops(self, trip: ScheduledTrip) -> list[RouteStop]:
        return sorted(trip.route.route_stops, key=lambda item: item.sequence_no)

    def _build_route_stop_by_stop_id(self, trip: ScheduledTrip) -> dict[str, RouteStop]:
        return {item.stop_id: item for item in self._get_sorted_route_stops(trip)}

    def _get_route_stop_planned_time(
        self,
        *,
        trip: ScheduledTrip,
        target_sequence_no: int,
    ) -> datetime:
        cumulative_minutes = 0
        for index, route_stop in enumerate(self._get_sorted_route_stops(trip)):
            if index == 0:
                cumulative_minutes = 0
            else:
                cumulative_minutes += max(int(route_stop.assume_time_diff_minutes or 0), 0)

            if route_stop.sequence_no == target_sequence_no:
                return trip.planned_start_at + timedelta(minutes=cumulative_minutes)

        return trip.planned_start_at

    async def _lock_traveller_identity_keys(
        self,
        traveller_identity_keys: list[str],
    ) -> None:
        """Serialize conflict checks for each physical traveller on PostgreSQL."""
        bind = self.db.get_bind()
        if bind.dialect.name != "postgresql":
            return

        for identity_key in sorted(set(traveller_identity_keys)):
            await self.db.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended(:identity_key, 0)"
                    ")"
                ),
                {"identity_key": identity_key},
            )

    async def _list_active_bookings_for_traveller_identities(
        self,
        *,
        traveller_identity_keys: list[str],
    ) -> list[TripBooking]:
        if not traveller_identity_keys:
            return []

        now = utcnow()
        active_booking_filter = or_(
            TripBooking.booking_status.in_(
                (BookingStatus.BOOKED, BookingStatus.BOARDED)
            ),
            and_(
                TripBooking.booking_status == BookingStatus.PENDING_PAYMENT,
                or_(
                    TripBooking.payment_hold_expires_at.is_(None),
                    TripBooking.payment_hold_expires_at > now,
                ),
            ),
        )

        stmt = (
            select(TripBooking)
            .where(
                TripBooking.traveller_identity_key.in_(
                    set(traveller_identity_keys)
                ),
                active_booking_filter,
            )
            .with_for_update()
            .options(
                selectinload(TripBooking.scheduled_trip)
                .selectinload(ScheduledTrip.route)
                .selectinload(Route.route_stops)
            )
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().unique().all())

    async def _ensure_traveller_bookings_do_not_conflict(
        self,
        *,
        trip: ScheduledTrip,
        pickup_stop_id: str,
        dropoff_stop_id: str,
        pickup_sequence_no: int,
        dropoff_sequence_no: int,
        traveller_requests: list[tuple[str, int]],
    ) -> None:
        """Reject overlapping route legs or journey windows per traveller."""
        seats_by_identity: dict[str, list[int]] = {}
        for identity_key, seat_number in traveller_requests:
            seats_by_identity.setdefault(identity_key, []).append(seat_number)

        duplicate_seat_groups = [
            sorted(seat_numbers)
            for seat_numbers in seats_by_identity.values()
            if len(seat_numbers) > 1
        ]
        if duplicate_seat_groups:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "duplicate_traveller_in_booking_session",
                    "message": "The same traveller cannot occupy multiple seats in one booking session.",
                    "seat_number_groups": duplicate_seat_groups,
                },
            )

        identity_keys = list(seats_by_identity)
        await self._lock_traveller_identity_keys(identity_keys)
        active_bookings = await self._list_active_bookings_for_traveller_identities(
            traveller_identity_keys=identity_keys
        )

        bookings_by_identity: dict[str, list[TripBooking]] = {}
        for booking in active_bookings:
            bookings_by_identity.setdefault(
                booking.traveller_identity_key, []
            ).append(booking)

        requested_start: datetime | None = None
        requested_end: datetime | None = None
        now = utcnow()

        for identity_key, seat_number in traveller_requests:
            for booking in bookings_by_identity.get(identity_key, []):
                if booking.scheduled_trip_id == trip.id:
                    if not route_segments_overlap(
                        existing_pickup_sequence_no=(
                            booking.pickup_sequence_no_snapshot
                        ),
                        existing_dropoff_sequence_no=(
                            booking.dropoff_sequence_no_snapshot
                        ),
                        requested_pickup_sequence_no=pickup_sequence_no,
                        requested_dropoff_sequence_no=dropoff_sequence_no,
                    ):
                        continue

                    conflict_type = "overlapping_route_segment"
                else:
                    if requested_start is None or requested_end is None:
                        requested_start = self._get_route_stop_planned_time(
                            trip=trip,
                            target_sequence_no=pickup_sequence_no,
                        )
                        requested_end = self._get_route_stop_planned_time(
                            trip=trip,
                            target_sequence_no=dropoff_sequence_no,
                        )

                    existing_trip = booking.scheduled_trip
                    existing_start = self._get_route_stop_planned_time(
                        trip=existing_trip,
                        target_sequence_no=(
                            booking.pickup_sequence_no_snapshot
                        ),
                    )
                    existing_end = self._get_route_stop_planned_time(
                        trip=existing_trip,
                        target_sequence_no=(
                            booking.dropoff_sequence_no_snapshot
                        ),
                    )
                    if (
                        booking.booking_status == BookingStatus.BOARDED
                        and existing_end < now
                    ):
                        existing_end = now

                    if not journey_windows_conflict(
                        existing_start=existing_start,
                        existing_end=existing_end,
                        existing_pickup_stop_id=booking.pickup_stop_id,
                        existing_dropoff_stop_id=booking.dropoff_stop_id,
                        requested_start=requested_start,
                        requested_end=requested_end,
                        requested_pickup_stop_id=pickup_stop_id,
                        requested_dropoff_stop_id=dropoff_stop_id,
                    ):
                        continue

                    windows_overlap = (
                        existing_start < requested_end
                        and existing_end > requested_start
                    )
                    conflict_type = (
                        "overlapping_trip_window"
                        if windows_overlap
                        else "insufficient_transfer_time"
                    )

                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "traveller_booking_conflict",
                        "message": "This traveller already has an active booking that conflicts with the requested journey.",
                        "seat_number": seat_number,
                        "conflicting_booking_id": booking.id,
                        "conflicting_scheduled_trip_id": (
                            booking.scheduled_trip_id
                        ),
                        "conflict_type": conflict_type,
                        "transfer_buffer_minutes": (
                            DEFAULT_TRANSFER_BUFFER_MINUTES
                        ),
                    },
                )

    def _get_current_progress_stop(
        self,
        trip: ScheduledTrip,
    ) -> dict[str, Any] | None:
        route_stop_by_stop_id = self._build_route_stop_by_stop_id(trip)

        best_departed: tuple[int, TripEvent] | None = None
        best_arrived: tuple[int, TripEvent] | None = None

        for event in trip.trip_events:
            route_stop = route_stop_by_stop_id.get(event.stop_id)
            if route_stop is None:
                continue

            sequence_no = route_stop.sequence_no

            if event.departure_time is not None:
                if best_departed is None or sequence_no > best_departed[0]:
                    best_departed = (sequence_no, event)
            elif event.arrival_time is not None:
                if best_arrived is None or sequence_no > best_arrived[0]:
                    best_arrived = (sequence_no, event)

        if best_departed is not None:
            _, event = best_departed
            return {
                "stop": self._serialize_stop_brief(event.stop),
                "event_status": "departed",
                "actual_time": event.departure_time,
            }

        if best_arrived is not None:
            _, event = best_arrived
            return {
                "stop": self._serialize_stop_brief(event.stop),
                "event_status": "arrived",
                "actual_time": event.arrival_time,
            }

        return None

    def _get_estimated_time_for_sequence(
        self,
        *,
        trip: ScheduledTrip,
        target_sequence_no: int,
    ) -> datetime:
        sorted_route_stops = self._get_sorted_route_stops(trip)
        trip_event_map = self._build_trip_event_map(trip)

        anchor_sequence_no: int | None = None
        anchor_time: datetime | None = None

        for route_stop in reversed(sorted_route_stops):
            event = trip_event_map.get(route_stop.stop_id)
            if event is None:
                continue

            if event.departure_time is not None:
                anchor_sequence_no = route_stop.sequence_no
                anchor_time = event.departure_time
                break

            if event.arrival_time is not None:
                anchor_sequence_no = route_stop.sequence_no
                anchor_time = event.arrival_time
                break

        if anchor_sequence_no is None or anchor_time is None:
            return self._get_route_stop_planned_time(
                trip=trip,
                target_sequence_no=target_sequence_no,
            )

        if target_sequence_no <= anchor_sequence_no:
            return self._get_route_stop_planned_time(
                trip=trip,
                target_sequence_no=target_sequence_no,
            )

        minutes_to_add = 0
        started = False
        for route_stop in sorted_route_stops:
            if route_stop.sequence_no == anchor_sequence_no:
                started = True
                continue

            if not started:
                continue

            minutes_to_add += max(int(route_stop.assume_time_diff_minutes or 0), 0)

            if route_stop.sequence_no == target_sequence_no:
                return anchor_time + timedelta(minutes=minutes_to_add)

        return self._get_route_stop_planned_time(
            trip=trip,
            target_sequence_no=target_sequence_no,
        )

    def _serialize_segment_stops(
        self,
        booking: TripBooking,
    ) -> list[dict[str, Any]]:
        trip = booking.scheduled_trip
        sorted_route_stops = self._get_sorted_route_stops(trip)
        trip_event_map = self._build_trip_event_map(trip)

        segment_route_stops = [
            item
            for item in sorted_route_stops
            if booking.pickup_sequence_no_snapshot <= item.sequence_no <= booking.dropoff_sequence_no_snapshot
        ]

        boarding_scan_completed = any(
            event.scan_type.value == "board" and event.within_radius
            for event in booking.scan_events
        ) or booking.boarded_at is not None or booking.booking_status in (
            BookingStatus.BOARDED,
            BookingStatus.COMPLETED,
        )

        drop_scan_completed = any(
            event.scan_type.value == "drop" and event.within_radius
            for event in booking.scan_events
        ) or booking.completed_at is not None or booking.booking_status == BookingStatus.COMPLETED

        items: list[dict[str, Any]] = []

        for route_stop in segment_route_stops:
            trip_event = trip_event_map.get(route_stop.stop_id)
            planned_time = self._get_route_stop_planned_time(
                trip=trip,
                target_sequence_no=route_stop.sequence_no,
            )
            estimated_time = self._get_estimated_time_for_sequence(
                trip=trip,
                target_sequence_no=route_stop.sequence_no,
            )

            stop_status = "upcoming"

            if trip_event is not None and trip_event.departure_time is not None:
                stop_status = "departed"
            elif trip_event is not None and trip_event.arrival_time is not None:
                stop_status = "arrived"

            if route_stop.sequence_no == booking.pickup_sequence_no_snapshot and boarding_scan_completed:
                stop_status = "boarded_here"

            if route_stop.sequence_no == booking.dropoff_sequence_no_snapshot and drop_scan_completed:
                stop_status = "dropped_here"

            if booking.scheduled_trip.status == ScheduledTripStatus.COMPLETED:
                if route_stop.sequence_no < booking.dropoff_sequence_no_snapshot:
                    stop_status = "passed"
                elif route_stop.sequence_no == booking.dropoff_sequence_no_snapshot and drop_scan_completed:
                    stop_status = "dropped_here"

            items.append(
                {
                    "route_stop_id": route_stop.id,
                    "sequence_no": route_stop.sequence_no,
                    "assume_time_diff_minutes": route_stop.assume_time_diff_minutes,
                    "is_pickup_stop": route_stop.sequence_no == booking.pickup_sequence_no_snapshot,
                    "is_dropoff_stop": route_stop.sequence_no == booking.dropoff_sequence_no_snapshot,
                    "stop_status": stop_status,
                    "planned_time_at_stop": planned_time,
                    "estimated_time_at_stop": estimated_time,
                    "actual_arrival_time": None if trip_event is None else trip_event.arrival_time,
                    "actual_departure_time": None if trip_event is None else trip_event.departure_time,
                    "stop": self._serialize_stop_brief(route_stop.stop),
                }
            )

        return items

    def _serialize_current_trip_status(
        self,
        booking: TripBooking,
    ) -> dict[str, Any]:
        trip = booking.scheduled_trip
        sorted_route_stops = self._get_sorted_route_stops(trip)

        trip_from_stop = (
            self._serialize_stop_brief(sorted_route_stops[0].stop)
            if sorted_route_stops
            else None
        )
        trip_to_stop = (
            self._serialize_stop_brief(sorted_route_stops[-1].stop)
            if sorted_route_stops
            else None
        )

        boarding_scan_completed = any(
            event.scan_type.value == "board" and event.within_radius
            for event in booking.scan_events
        ) or booking.boarded_at is not None or booking.booking_status in (
            BookingStatus.BOARDED,
            BookingStatus.COMPLETED,
        )

        drop_scan_completed = any(
            event.scan_type.value == "drop" and event.within_radius
            for event in booking.scan_events
        ) or booking.completed_at is not None or booking.booking_status == BookingStatus.COMPLETED

        trip_completed = (
            trip.status == ScheduledTripStatus.COMPLETED
            or booking.booking_status == BookingStatus.COMPLETED
            or trip.actual_end_at is not None
        )

        return {
            "booking_id": booking.id,
            "booking_session_id": booking.booking_session_id,
            "passenger_user_id": booking.passenger_user_id,
            "booked_by_user_id": booking.booked_by_user_id,

            "traveller_profile_id": booking.traveller_profile_id,
            "traveller_name_snapshot": booking.traveller_name_snapshot,
            "traveller_phone_snapshot": booking.traveller_phone_snapshot,
            "traveller_email_snapshot": booking.traveller_email_snapshot,
            "traveller_relationship_label_snapshot": (
                booking.traveller_relationship_label_snapshot
            ),

            "seat_number": booking.seat_number,
            "scheduled_trip_id": booking.scheduled_trip_id,
            "otp": self._serialize_booking_otp(booking),
            "booking_status": booking.booking_status,
            "trip_status": trip.status,
            "boarding_scan_completed": boarding_scan_completed,
            "drop_scan_completed": drop_scan_completed,
            "trip_completed": trip_completed,
            "pickup_stop": self._serialize_stop_brief(booking.pickup_stop),
            "dropoff_stop": self._serialize_stop_brief(booking.dropoff_stop),
            "trip_from_stop": trip_from_stop,
            "trip_to_stop": trip_to_stop,
            "current_progress_stop": self._get_current_progress_stop(trip),
            "segment_stops": self._serialize_segment_stops(booking),
        }
    
    @staticmethod
    def _get_transaction_effective_status(
        booking: TripBooking,
        payment: BookingPayment,
    ) -> str:
        if payment.status == BookingPaymentStatus.REFUNDED:
            return "refunded"

        if (
            booking.booking_status == BookingStatus.CANCELLED
            and payment.status == BookingPaymentStatus.PAID
        ):
            return "refund_pending"

        return payment.status.value

    def _serialize_transaction(self, payment: BookingPayment) -> dict[str, Any]:
        booking = payment.booking
        route = booking.route
        trip = booking.scheduled_trip
        effective_status = self._get_transaction_effective_status(booking, payment)

        return {
            "payment_source": "booking",
            "payment_id": payment.id,
            "booking_id": booking.id,
            "booking_session_id": booking.booking_session_id,
            "booking_ids": [booking.id],
            "seat_number": booking.seat_number,
            "scheduled_trip_id": booking.scheduled_trip_id,
            "route_id": booking.route_id,
            "booking_status": booking.booking_status,
            "payment_status": payment.status,
            "effective_status": effective_status,
            "amount": payment.amount,
            "taxable_amount": self._quantize_money(
                Decimal(getattr(payment, "taxable_amount", 0) or 0)
            ),
            "cgst_amount": self._quantize_money(
                Decimal(getattr(payment, "cgst_amount", 0) or 0)
            ),
            "sgst_amount": self._quantize_money(
                Decimal(getattr(payment, "sgst_amount", 0) or 0)
            ),
            "igst_amount": self._quantize_money(
                Decimal(getattr(payment, "igst_amount", 0) or 0)
            ),
            "total_tax_amount": self._quantize_money(
                Decimal(getattr(payment, "total_tax_amount", 0) or 0)
            ),
            "razorpay_order_id": payment.razorpay_order_id,
            "razorpay_payment_id": payment.razorpay_payment_id,
            "pickup_stop": self._serialize_stop_brief(booking.pickup_stop),
            "dropoff_stop": self._serialize_stop_brief(booking.dropoff_stop),
            "route_name": None if route is None else route.name,
            "route_code": None if route is None else route.code,
            "planned_start_at": None if trip is None else trip.planned_start_at,
            "planned_end_at": None if trip is None else trip.planned_end_at,
            "completed_at": booking.completed_at,
            "cancelled_at": booking.cancelled_at,
            "cancellation_metadata": self._serialize_cancellation_metadata(booking),
            "created_at": payment.created_at,
            "updated_at": payment.updated_at,
            "transaction": self._serialize_transaction_payment_details(
                payment,
                source="booking",
                effective_status=effective_status,
                booking_id=booking.id,
                booking_session_id=booking.booking_session_id,
            ),
            "booking": None,
            "bookings": None,
            "invoice": None,
            "invoices": None,
            "invoice_unavailable_reason": None,
            "failure": self._serialize_transaction_failure(
                payment,
                effective_status=effective_status,
            ),
        }

    def _serialize_booking_session_transaction(
        self,
        payment: BookingSessionPayment,
    ) -> dict[str, Any]:
        booking_session = payment.booking_session
        bookings = sorted(booking_session.bookings, key=lambda item: item.seat_number)
        first_booking = bookings[0] if bookings else None
        route = booking_session.route
        trip = booking_session.scheduled_trip
        refunded_amount = self._quantize_money(
            Decimal(getattr(payment, "refunded_amount", 0) or 0)
        )
        if payment.status == BookingPaymentStatus.REFUNDED:
            effective_status = "refunded"
        elif payment.status == BookingPaymentStatus.PAID and refunded_amount > 0:
            effective_status = "partially_refunded"
        else:
            effective_status = payment.status.value

        return {
            "payment_source": "booking_session",
            "payment_id": payment.id,
            "booking_id": None,
            "booking_session_id": booking_session.id,
            "booking_ids": [booking.id for booking in bookings],
            "seat_number": None,
            "scheduled_trip_id": booking_session.scheduled_trip_id,
            "route_id": booking_session.route_id,
            "booking_status": None,
            "payment_status": payment.status,
            "effective_status": effective_status,
            "amount": payment.amount,
            "taxable_amount": self._quantize_money(
                Decimal(getattr(payment, "taxable_amount", 0) or 0)
            ),
            "cgst_amount": self._quantize_money(
                Decimal(getattr(payment, "cgst_amount", 0) or 0)
            ),
            "sgst_amount": self._quantize_money(
                Decimal(getattr(payment, "sgst_amount", 0) or 0)
            ),
            "igst_amount": self._quantize_money(
                Decimal(getattr(payment, "igst_amount", 0) or 0)
            ),
            "total_tax_amount": self._quantize_money(
                Decimal(getattr(payment, "total_tax_amount", 0) or 0)
            ),
            "razorpay_order_id": payment.razorpay_order_id,
            "razorpay_payment_id": payment.razorpay_payment_id,
            "pickup_stop": self._serialize_stop_brief(booking_session.pickup_stop),
            "dropoff_stop": self._serialize_stop_brief(booking_session.dropoff_stop),
            "route_name": None if route is None else route.name,
            "route_code": None if route is None else route.code,
            "planned_start_at": None if trip is None else trip.planned_start_at,
            "planned_end_at": None if trip is None else trip.planned_end_at,
            "completed_at": (
                None
                if first_booking is None
                else first_booking.completed_at
            ),
            "cancelled_at": booking_session.cancelled_at,
            "cancellation_metadata": self._serialize_cancellation_metadata(
                booking_session
            ),
            "created_at": payment.created_at,
            "updated_at": payment.updated_at,
            "transaction": self._serialize_transaction_payment_details(
                payment,
                source="booking_session",
                effective_status=effective_status,
                booking_id=None,
                booking_session_id=booking_session.id,
            ),
            "booking": None,
            "bookings": None,
            "invoice": None,
            "invoices": None,
            "invoice_unavailable_reason": None,
            "failure": self._serialize_transaction_failure(
                payment,
                effective_status=effective_status,
            ),
        }

    def _serialize_transaction_payment_details(
        self,
        payment: BookingPayment | BookingSessionPayment,
        *,
        source: str,
        effective_status: str,
        booking_id: str | None,
        booking_session_id: str | None,
    ) -> dict[str, Any]:
        return {
            "id": payment.id,
            "source": source,
            "booking_id": booking_id,
            "booking_session_id": booking_session_id,
            "razorpay_order_id": payment.razorpay_order_id,
            "razorpay_payment_id": payment.razorpay_payment_id,
            "status": payment.status,
            "effective_status": effective_status,
            "amount": payment.amount,
            "taxable_amount": self._quantize_money(
                Decimal(getattr(payment, "taxable_amount", 0) or 0)
            ),
            "cgst_amount": self._quantize_money(
                Decimal(getattr(payment, "cgst_amount", 0) or 0)
            ),
            "sgst_amount": self._quantize_money(
                Decimal(getattr(payment, "sgst_amount", 0) or 0)
            ),
            "igst_amount": self._quantize_money(
                Decimal(getattr(payment, "igst_amount", 0) or 0)
            ),
            "total_tax_amount": self._quantize_money(
                Decimal(getattr(payment, "total_tax_amount", 0) or 0)
            ),
            "razorpay_refund_id": getattr(payment, "razorpay_refund_id", None),
            "refunded_amount": self._quantize_money(
                Decimal(getattr(payment, "refunded_amount", 0) or 0)
            ),
            "refund_requested_at": getattr(payment, "refund_requested_at", None),
            "refund_processed_at": getattr(payment, "refund_processed_at", None),
            "refund_failure_reason": getattr(payment, "refund_failure_reason", None),
            "created_at": payment.created_at,
            "updated_at": payment.updated_at,
        }

    @staticmethod
    def _serialize_transaction_failure(
        payment: BookingPayment | BookingSessionPayment,
        *,
        effective_status: str,
    ) -> dict[str, Any] | None:
        if payment.status != BookingPaymentStatus.FAILED:
            return None
        return {
            "code": "payment_failed",
            "message": "The payment attempt failed before the booking was confirmed.",
            "gateway": "razorpay",
            "gateway_order_id": payment.razorpay_order_id,
            "gateway_payment_id": payment.razorpay_payment_id,
            "provider_reason": None,
            "recorded_at": payment.updated_at,
        }
    
    def _get_notification_service(self) -> NotificationService:
        return NotificationService(
            db=self.db,
            ws_hub=self.ws_hub,
        )

    @staticmethod
    def _build_booking_notification_data(
        booking: TripBooking,
        *,
        refresh: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "booking_id": booking.id,
            "scheduled_trip_id": booking.scheduled_trip_id,
            "route_id": booking.route_id,
            "pickup_stop_id": booking.pickup_stop_id,
            "dropoff_stop_id": booking.dropoff_stop_id,
            "seat_number": booking.seat_number,
            "booking_status": booking.booking_status.value,
            "refresh": refresh or ["bookings_list", "booking_detail"],
        }

    @staticmethod
    def _build_support_ticket_notification_data(
        ticket: SupportTicket,
    ) -> dict[str, Any]:
        return {
            "ticket_id": ticket.id,
            "status": ticket.status.value,
            "refresh": ["support_tickets", "support_ticket_detail"],
        }

    async def _build_traveller_trip_sms_context(
        self,
        *,
        booking_session: BookingSession,
        booking: TripBooking,
    ) -> dict[str, Any]:
        trip = await self._get_trip_obj(booking_session.scheduled_trip_id)

        pickup_stop = await self._get_stop_obj_or_raise(booking.pickup_stop_id)
        dropoff_stop = await self._get_stop_obj_or_raise(booking.dropoff_stop_id)

        pickup_time = self._get_route_stop_planned_time(
            trip=trip,
            target_sequence_no=booking.pickup_sequence_no_snapshot,
        )

        dropoff_time = self._get_route_stop_planned_time(
            trip=trip,
            target_sequence_no=booking.dropoff_sequence_no_snapshot,
        )

        route_name = None
        if trip.route is not None:
            route_name = trip.route.name

        vehicle_name = None
        vehicle_number = None
        if trip.vehicle is not None:
            vehicle_name = trip.vehicle.vehicle_name
            vehicle_number = trip.vehicle.registration_number

        return {
            "route_name": route_name,
            "pickup_stop_name": pickup_stop.name,
            "dropoff_stop_name": dropoff_stop.name,
            "pickup_time": pickup_time,
            "dropoff_time": dropoff_time,
            "vehicle_name": vehicle_name,
            "vehicle_number": vehicle_number,
        }
    
    async def _build_traveller_sms_message(
        self,
        *,
        booking_session: BookingSession,
        booking: TripBooking,
        event_type: str,
    ) -> str:
        traveller_name = booking.traveller_name_snapshot or "Passenger"

        context = await self._build_traveller_trip_sms_context(
            booking_session=booking_session,
            booking=booking,
        )

        pickup_time_text = self._format_sms_datetime(context["pickup_time"])
        dropoff_time_text = self._format_sms_datetime(context["dropoff_time"])

        route_text = context["route_name"] or "your shuttle route"

        vehicle_parts = [
            value
            for value in (
                context["vehicle_name"],
                context["vehicle_number"],
            )
            if value
        ]
        vehicle_text = ", ".join(vehicle_parts) if vehicle_parts else "TBA"

        if event_type == "traveller_seat_confirmed":
            first_line = f"Hi {traveller_name}, your shuttle seat is confirmed."
        elif event_type == "traveller_seat_cancelled":
            first_line = f"Hi {traveller_name}, your shuttle seat has been cancelled."
        else:
            first_line = f"Hi {traveller_name}, your shuttle booking has an update."

        otp = self._serialize_booking_otp(booking)

        otp_line = ""
        if event_type == "traveller_seat_confirmed" and otp:
            otp_line = f"Boarding OTP: {otp}\n"

        return (
            f"{first_line}\n"
            f"Route: {route_text}\n"
            f"Pickup: {context['pickup_stop_name']} at {pickup_time_text}\n"
            f"Drop: {context['dropoff_stop_name']} around {dropoff_time_text}\n"
            f"Seat: {booking.seat_number}\n"
            f"{otp_line}"
            f"Vehicle: {vehicle_text}\n"
            f"For changes or cancellation, contact the person who booked this ride."
        )

    @staticmethod
    def _format_sms_datetime(value: datetime | None) -> str:
        if value is None:
            return "TBA"

        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=timezone.utc)

        return value.astimezone(IST).strftime(
            "%d %b %Y, %I:%M %p IST"
        )

    @staticmethod
    def _get_traveller_notification_channel(booking: TripBooking) -> str:
        primary_channel = os.getenv(
            "TRAVELLER_CONTACT_PRIMARY_CHANNEL",
            "email",
        ).strip().lower()

        has_email = bool((booking.traveller_email_snapshot or "").strip())
        has_phone = bool((booking.traveller_phone_snapshot or "").strip())

        if primary_channel == "sms":
            if has_phone:
                return "sms"
            if has_email:
                return "email"
            return "none"

        if has_email:
            return "email"
        if has_phone:
            return "sms"
        return "none"

    def _build_traveller_booking_payload(
        self,
        *,
        booking_session: BookingSession,
        booking: TripBooking,
        event_type: str,
    ) -> dict[str, Any]:
        return {
            "type": event_type,
            "delivery_assumption": "traveller_contact_sms_or_email",
            "booking_session_id": booking_session.id,
            "booking_id": booking.id,
            "owner_user_id": booking_session.owner_user_id,
            "scheduled_trip_id": booking.scheduled_trip_id,
            "route_id": booking.route_id,
            "pickup_stop_id": booking.pickup_stop_id,
            "dropoff_stop_id": booking.dropoff_stop_id,
            "pickup_sequence_no_snapshot": booking.pickup_sequence_no_snapshot,
            "dropoff_sequence_no_snapshot": booking.dropoff_sequence_no_snapshot,
            "seat_number": booking.seat_number,
            "traveller_profile_id": booking.traveller_profile_id,
            "traveller_name": booking.traveller_name_snapshot,
            "traveller_phone": booking.traveller_phone_snapshot,
            "traveller_email": booking.traveller_email_snapshot,
            "booking_status": booking.booking_status.value
            if hasattr(booking.booking_status, "value")
            else str(booking.booking_status),
            "contains_public_link": False,
            "contains_payment_data": False,
        }

    async def _queue_traveller_contact_notification(
        self,
        *,
        booking_session: BookingSession,
        booking: TripBooking,
        event_type: str,
        title: str,
        message: str,
    ) -> bool:
        channel = self._get_traveller_notification_channel(booking)

        status = (
            TravellerContactNotificationStatus.PENDING
            if channel != "none"
            else TravellerContactNotificationStatus.SKIPPED
        )

        payload = self._build_traveller_booking_payload(
            booking_session=booking_session,
            booking=booking,
            event_type=event_type,
        )

        result = await self.db.execute(
            pg_insert(TravellerContactNotification)
            .values(
                id=new_id(),
                booking_session_id=booking_session.id,
                booking_id=booking.id,
                owner_user_id=booking_session.owner_user_id,
                traveller_profile_id=booking.traveller_profile_id,
                traveller_name_snapshot=booking.traveller_name_snapshot,
                traveller_phone_snapshot=booking.traveller_phone_snapshot,
                traveller_email_snapshot=booking.traveller_email_snapshot,
                channel=channel,
                event_type=event_type,
                title=title,
                message=message,
                payload_json=json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ),
                status=status,
                failure_reason=None
                if channel != "none"
                else "No traveller phone or email snapshot is available.",
                delivery_attempt_count=0,
            )
            .on_conflict_do_nothing(
                constraint="uq_traveller_contact_notifications_booking_event"
            )
            .returning(TravellerContactNotification.id)
        )
        await self.db.flush()

        return result.scalar_one_or_none() is not None

    async def _queue_booking_session_traveller_notifications(
        self,
        *,
        booking_session: BookingSession,
        bookings: list[TripBooking],
        event_type: str,
    ) -> None:
        for booking in bookings:
            traveller_name = (
                booking.traveller_name_snapshot
                or "Passenger"
            )

            if event_type == "traveller_seat_confirmed":
                title = "Shuttle seat confirmed"
            elif event_type == "traveller_seat_cancelled":
                title = "Shuttle seat cancelled"
            else:
                title = "Shuttle booking update"

            message = await self._build_traveller_sms_message(
                booking_session=booking_session,
                booking=booking,
                event_type=event_type,
            )

            await self._queue_traveller_contact_notification(
                booking_session=booking_session,
                booking=booking,
                event_type=event_type,
                title=title,
                message=message,
            )

    async def _notify_user(
        self,
        *,
        user_id: str,
        title: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        notification_service = self._get_notification_service()
        await notification_service.notify_user(
            user_id=user_id,
            title=title,
            message=message,
            data=data or {},
        )

    async def _broadcast_seatmap_snapshots_for_trip(
        self,
        *,
        scheduled_trip_id: str,
        reason: str,
    ) -> None:
        try:
            from app.passenger.seatmap_ws import (
                broadcast_all_seatmap_snapshots_for_trip,
            )

            await broadcast_all_seatmap_snapshots_for_trip(
                scheduled_trip_id=scheduled_trip_id,
                reason=reason,
            )
        except Exception:
            # Seatmap WS is best-effort; DB state remains the source of truth.
            # Clients can still recover by manual REST refresh.
            pass

    @staticmethod
    def _available_rfid_balance(account: RFIDCardAccount) -> Decimal:
        return PassengerService._quantize_money(
            Decimal(account.current_balance or 0)
            - Decimal(account.held_balance or 0)
        )

    def _serialize_passenger_rfid_card(
        self,
        card: RFIDCard,
    ) -> dict[str, Any]:
        return {
            "id": card.id,
            "card_uid_masked": card.card_uid_masked,
            "inventory_status": card.inventory_status,
            "authorization_status": card.authorization_status,
            "assigned_at": card.assigned_at,
        }

    def _serialize_passenger_rfid_account(
        self,
        account: RFIDCardAccount,
    ) -> dict[str, Any]:
        return {
            "id": account.id,
            "card_id": account.card_id,
            "current_balance": account.current_balance,
            "held_balance": account.held_balance,
            "available_balance": self._available_rfid_balance(account),
            "currency": account.currency,
            "is_active": account.is_active,
            "created_at": account.created_at,
            "updated_at": account.updated_at,
        }

    def _serialize_passenger_rfid_assignment(
        self,
        assignment: RFIDCardAssignment,
    ) -> dict[str, Any]:
        return {
            "id": assignment.id,
            "card_id": assignment.card_id,
            "passenger_user_id": assignment.passenger_user_id,
            "assigned_at": assignment.assigned_at,
            "reason": assignment.reason,
            "created_at": assignment.created_at,
            "updated_at": assignment.updated_at,
        }

    def _serialize_passenger_rfid_ledger_entry(
        self,
        entry: RFIDLedgerEntry,
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
            "note": entry.note,
            "created_at": entry.created_at,
        }

    def _serialize_passenger_rfid_recharge(
        self,
        recharge: RFIDRecharge,
    ) -> dict[str, Any]:
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
            "paid_at": recharge.paid_at,
            "credited_at": recharge.credited_at,
            "created_at": recharge.created_at,
            "updated_at": recharge.updated_at,
        }

    def _serialize_passenger_rfid_ride(
        self,
        ride: RFIDTripRide,
        *,
        stops_by_id: dict[str, Stop] | None = None,
    ) -> dict[str, Any]:
        stops_by_id = stops_by_id or {}

        pickup_stop = stops_by_id.get(ride.pickup_stop_id)
        dropoff_stop = (
            None
            if ride.dropoff_stop_id is None
            else stops_by_id.get(ride.dropoff_stop_id)
        )
        tax_fields = self._rfid_ride_tax_fields(ride)

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
            "boarded_at": ride.boarded_at,
            "board_lat": ride.board_lat,
            "board_lng": ride.board_lng,
            "dropoff_stop_id": ride.dropoff_stop_id,
            "dropoff_sequence_no": ride.dropoff_sequence_no,
            "dropped_at": ride.dropped_at,
            "drop_lat": ride.drop_lat,
            "drop_lng": ride.drop_lng,
            "status": ride.status,
            "hold_amount": ride.hold_amount,
            "fare_amount": ride.fare_amount,
            **tax_fields,
            "fare_reversed_amount": ride.fare_reversed_amount,
            "fare_net_amount": self._quantize_money(
                Decimal(ride.fare_amount or 0)
                - Decimal(ride.fare_reversed_amount or 0)
            ),
            "transfer_status": ride.transfer_status,
            "transfer_ready_at": ride.transfer_ready_at,
            "transfer_processed_at": ride.transfer_processed_at,
            "pickup_stop": None
            if pickup_stop is None
            else self._serialize_stop_brief(pickup_stop),
            "dropoff_stop": None
            if dropoff_stop is None
            else self._serialize_stop_brief(dropoff_stop),
            "created_at": ride.created_at,
            "updated_at": ride.updated_at,
        }

    async def _get_current_rfid_assignment(
        self,
        passenger_user_id: str,
    ) -> RFIDCardAssignment | None:
        stmt = (
            select(RFIDCardAssignment)
            .where(
                RFIDCardAssignment.passenger_user_id == passenger_user_id,
                RFIDCardAssignment.unassigned_at.is_(None),
            )
            .order_by(RFIDCardAssignment.assigned_at.desc())
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_passenger_rfid_card_account(
        self,
        *,
        passenger_user_id: str,
    ) -> tuple[RFIDCard | None, RFIDCardAccount | None, RFIDCardAssignment | None]:
        assignment = await self._get_current_rfid_assignment(passenger_user_id)

        if assignment is None:
            return None, None, None

        card_stmt = (
            select(RFIDCard)
            .where(
                RFIDCard.id == assignment.card_id,
                RFIDCard.assigned_passenger_user_id == passenger_user_id,
            )
            .limit(1)
        )
        card_result = await self.db.execute(card_stmt)
        card = card_result.scalar_one_or_none()

        if card is None:
            return None, None, None

        account_stmt = (
            select(RFIDCardAccount)
            .where(RFIDCardAccount.card_id == card.id)
            .limit(1)
        )
        account_result = await self.db.execute(account_stmt)
        account = account_result.scalar_one_or_none()

        return card, account, assignment

    async def _get_stops_by_id(
        self,
        stop_ids: list[str],
    ) -> dict[str, Stop]:
        cleaned_stop_ids = [
            stop_id
            for stop_id in set(stop_ids)
            if stop_id
        ]

        if not cleaned_stop_ids:
            return {}

        stmt = select(Stop).where(Stop.id.in_(cleaned_stop_ids))
        result = await self.db.execute(stmt)
        stops = list(result.scalars().all())
        return {stop.id: stop for stop in stops}
    
    @staticmethod
    def _get_rfid_razorpay_credentials() -> tuple[str, str]:
        key_id = os.getenv("RAZORPAY_KEY_ID", "").strip()
        key_secret = os.getenv("RAZORPAY_KEY_SECRET", "").strip()

        if not key_id or not key_secret:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "razorpay_credentials_missing",
                    "message": "Razorpay credentials are not configured.",
                },
            )

        return key_id, key_secret

    @classmethod
    def _build_rfid_razorpay_auth_header(cls) -> str:
        key_id, key_secret = cls._get_rfid_razorpay_credentials()
        token = base64.b64encode(
            f"{key_id}:{key_secret}".encode("utf-8")
        ).decode("ascii")
        return f"Basic {token}"

    @classmethod
    async def _rfid_razorpay_request(
        cls,
        *,
        method: str,
        path: str,
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_url = os.getenv("RAZORPAY_BASE_URL", "https://api.razorpay.com/v1").rstrip(
            "/"
        )
        url = f"{base_url}{path}"

        headers = {
            "Authorization": cls._build_rfid_razorpay_auth_header(),
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    json=json_payload,
                )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "razorpay_request_failed",
                    "message": "Could not reach Razorpay.",
                },
            ) from exc

        try:
            data = response.json()
        except ValueError:
            data = {"raw_response": response.text}

        if response.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "razorpay_request_failed",
                    "message": "Razorpay rejected the request.",
                    "provider_status_code": response.status_code,
                    "provider_response": data,
                },
            )

        if not isinstance(data, dict):
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "razorpay_response_invalid",
                    "message": "Razorpay returned an invalid response.",
                },
            )

        return data

    @classmethod
    async def _create_rfid_recharge_razorpay_order(
        cls,
        *,
        amount: Decimal,
        receipt: str,
        notes: dict[str, Any],
    ) -> dict[str, Any]:
        amount_subunits = cls._to_subunits(amount)

        if amount_subunits < 100:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "rfid_recharge_amount_too_small",
                    "message": "RFID recharge amount must be at least ₹1.00.",
                },
            )

        return await cls._rfid_razorpay_request(
            method="POST",
            path="/orders",
            json_payload={
                "amount": amount_subunits,
                "currency": "INR",
                "receipt": receipt,
                "payment_capture": 1,
                "notes": notes,
            },
        )

    @classmethod
    def _verify_rfid_recharge_signature(
        cls,
        *,
        razorpay_order_id: str,
        razorpay_payment_id: str,
        razorpay_signature: str,
    ) -> None:
        _key_id, key_secret = cls._get_rfid_razorpay_credentials()

        message = f"{razorpay_order_id}|{razorpay_payment_id}".encode("utf-8")
        expected_signature = hmac.new(
            key_secret.encode("utf-8"),
            message,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected_signature, razorpay_signature):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_rfid_recharge_signature",
                    "message": "RFID recharge payment signature is invalid.",
                },
            )

    async def _get_rfid_recharge_for_update_or_404(
        self,
        *,
        recharge_id: str,
        passenger_user_id: str,
    ) -> RFIDRecharge:
        stmt = (
            select(RFIDRecharge)
            .where(
                RFIDRecharge.id == recharge_id,
                RFIDRecharge.passenger_user_id == passenger_user_id,
            )
            .with_for_update()
            .limit(1)
        )
        result = await self.db.execute(stmt)
        recharge = result.scalar_one_or_none()

        if recharge is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "rfid_recharge_not_found",
                    "message": "RFID recharge not found.",
                },
            )

        return recharge

    async def _get_rfid_account_for_update_or_404(
        self,
        *,
        card_id: str,
        passenger_user_id: str,
    ) -> RFIDCardAccount:
        card, account, _assignment = await self._get_passenger_rfid_card_account(
            passenger_user_id=passenger_user_id
        )

        if card is None or account is None or card.id != card_id:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "rfid_card_account_not_found",
                    "message": "RFID card account not found.",
                },
            )

        stmt = (
            select(RFIDCardAccount)
            .where(RFIDCardAccount.id == account.id)
            .with_for_update()
            .limit(1)
        )
        result = await self.db.execute(stmt)
        locked_account = result.scalar_one_or_none()

        if locked_account is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "rfid_card_account_not_found",
                    "message": "RFID card account not found.",
                },
            )

        return locked_account

    async def _ensure_active_passenger_rfid_card_account(
        self,
        current_user: User,
    ) -> tuple[RFIDCard, RFIDCardAccount, RFIDCardAssignment]:
        card, account, assignment = await self._get_passenger_rfid_card_account(
            passenger_user_id=current_user.id
        )

        if card is None or account is None or assignment is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_not_assigned",
                    "message": "No active RFID card is assigned to this passenger.",
                },
            )

        if card.inventory_status != RFIDCardInventoryStatus.ASSIGNED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_not_assigned",
                    "message": "RFID card is not currently assigned.",
                },
            )

        if card.authorization_status != RFIDCardAuthorizationStatus.ALLOWED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_not_allowed",
                    "message": "RFID card is not allowed.",
                },
            )

        if not account.is_active:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_account_inactive",
                    "message": "RFID account is inactive.",
                },
            )

        return card, account, assignment

    @staticmethod
    def _get_loaded_active_trip_event(
        trip: ScheduledTrip,
    ) -> TripEvent | None:
        active_events = [
            event
            for event in trip.trip_events
            if event.arrival_time is not None and event.departure_time is None
        ]

        active_events.sort(
            key=lambda event: event.arrival_time,
            reverse=True,
        )

        return active_events[0] if active_events else None

    @staticmethod
    def _get_loaded_route_stop_by_stop_id(
        trip: ScheduledTrip,
        stop_id: str,
    ) -> RouteStop | None:
        if trip.route is None:
            return None

        for route_stop in trip.route.route_stops:
            if route_stop.stop_id == stop_id:
                return route_stop

        return None

    async def _get_rfid_max_downstream_fare_from_stop(
        self,
        *,
        route_id: str,
        pickup_stop_id: str,
        pickup_sequence_no: int,
        is_ac: bool | None,
    ) -> Decimal | None:
        stmt = (
            select(func.max(RouteFare.amount))
            .join(
                RouteStop,
                (RouteStop.route_id == RouteFare.route_id)
                & (RouteStop.stop_id == RouteFare.dropoff_stop_id),
            )
            .where(
                RouteFare.route_id == route_id,
                RouteFare.pickup_stop_id == pickup_stop_id,
                RouteFare.is_active.is_(True),
                RouteStop.sequence_no > pickup_sequence_no,
            )
        )

        result = await self.db.execute(stmt)
        amount = result.scalar_one_or_none()

        if amount is None:
            return None

        breakdown = await self._build_gst_breakdown(
            self._quantize_money(Decimal(amount)),
            is_ac=is_ac,
        )
        return breakdown.gross_amount

    async def _count_open_rfid_rides_overlapping_leg(
        self,
        *,
        scheduled_trip_id: str,
        dropoff_sequence_no: int,
    ) -> int:
        stmt = (
            select(func.count(RFIDTripRide.id))
            .where(
                RFIDTripRide.scheduled_trip_id == scheduled_trip_id,
                RFIDTripRide.status == RFIDRideStatus.BOARDED,
                RFIDTripRide.pickup_sequence_no < dropoff_sequence_no,
            )
        )
        result = await self.db.execute(stmt)
        return int(result.scalar_one() or 0)

    async def _has_open_rfid_ride_for_card_on_trip(
        self,
        *,
        card_id: str,
        scheduled_trip_id: str,
    ) -> bool:
        stmt = (
            select(func.count(RFIDTripRide.id))
            .where(
                RFIDTripRide.card_id == card_id,
                RFIDTripRide.scheduled_trip_id == scheduled_trip_id,
                RFIDTripRide.status == RFIDRideStatus.BOARDED,
            )
        )
        result = await self.db.execute(stmt)
        return int(result.scalar_one() or 0) > 0

    @staticmethod
    def _get_rfid_reserved_capacity_for_trip(
        trip: ScheduledTrip,
    ) -> int:
        if trip.vehicle is not None:
            return max(int(trip.vehicle.default_rfid_reserved_seat_count or 0), 0)

        return max(int(getattr(trip, "rfid_reserved_seat_count", 0) or 0), 0)

    # ------------------------------------------------------------------
    # profile
    # ------------------------------------------------------------------
    async def get_profile(self, current_user: User) -> dict[str, Any]:
        self.ensure_passenger(current_user)
        profile = await self._get_profile_obj(current_user.id)
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "profile_not_found", "message": "Passenger profile not found."},
            )

        return {
            "id": profile.id,
            "user_id": profile.user_id,
            "full_name": profile.full_name,
            "profile_picture_path": profile.profile_picture_path,
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
        }

    async def create_profile(
        self,
        current_user: User,
        payload: PassengerProfileUpsertRequest,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        existing = await self._get_profile_obj(current_user.id)
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "profile_already_exists",
                    "message": "Passenger profile already exists.",
                },
            )

        profile = PassengerProfile(
            user_id=current_user.id,
            full_name=self._clean_name(payload.full_name),
            profile_picture_path=payload.profile_picture_path,
        )
        self.db.add(profile)
        await self.db.commit()
        await self.db.refresh(profile)

        return {
            "message": "Passenger profile created successfully.",
            "profile": {
                "id": profile.id,
                "user_id": profile.user_id,
                "full_name": profile.full_name,
                "profile_picture_path": profile.profile_picture_path,
                "created_at": profile.created_at,
                "updated_at": profile.updated_at,
            },
        }
    
    async def upsert_profile_picture(
        self,
        current_user: User,
        file: UploadFile,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        profile = await self._get_profile_obj(current_user.id)
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "profile_not_found",
                    "message": "Passenger profile not found. Create profile first.",
                },
            )

        if file is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "file_required",
                    "message": "Profile picture file is required.",
                },
            )

        content_type = (file.content_type or "").lower().strip()
        allowed_content_types = {
            "image/jpeg",
            "image/png",
            "image/webp",
            "image/gif",
        }
        if content_type not in allowed_content_types:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_file_type",
                    "message": "Only JPG, PNG, WEBP, and GIF images are allowed.",
                },
            )

        try:
            content = await file.read()
        finally:
            await file.close()

        if not content:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "empty_file",
                    "message": "Uploaded file is empty.",
                },
            )

        upload_dir = self._get_profile_picture_upload_dir()
        extension = self._guess_profile_picture_extension(
            file.filename,
            file.content_type,
        )
        filename = f"passenger_profile_{current_user.id}_{uuid4().hex}{extension}"

        disk_path = upload_dir / filename
        disk_path.write_bytes(content)

        public_path = f"/uploads/passenger/profilepictures/{filename}"

        old_public_path = (profile.profile_picture_path or "").strip()
        if old_public_path and old_public_path != public_path:
            try:
                old_filename = Path(old_public_path).name
                old_disk_path = upload_dir / old_filename
                if old_disk_path.is_file():
                    old_disk_path.unlink()
            except Exception:
                pass

        profile.profile_picture_path = public_path
        self.db.add(profile)
        await self.db.commit()
        await self.db.refresh(profile)

        return {
            "message": "Passenger profile picture uploaded successfully.",
            "profile": {
                "id": profile.id,
                "user_id": profile.user_id,
                "full_name": profile.full_name,
                "profile_picture_path": profile.profile_picture_path,
                "created_at": profile.created_at,
                "updated_at": profile.updated_at,
            },
        }

    async def patch_profile(
        self,
        current_user: User,
        payload: PassengerProfileUpsertRequest,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)
        profile = await self._get_profile_obj(current_user.id)
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "profile_not_found", "message": "Passenger profile not found."},
            )

        profile.full_name = self._clean_name(payload.full_name)
        profile.profile_picture_path = payload.profile_picture_path

        self.db.add(profile)
        await self.db.commit()
        await self.db.refresh(profile)

        return {
            "message": "Passenger profile updated successfully.",
            "profile": {
                "id": profile.id,
                "user_id": profile.user_id,
                "full_name": profile.full_name,
                "profile_picture_path": profile.profile_picture_path,
                "created_at": profile.created_at,
                "updated_at": profile.updated_at,
            },
        }
    
    async def list_traveller_profiles(
        self,
        current_user: User,
        *,
        active_only: bool = True,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        filters = [PassengerTravellerProfile.owner_user_id == current_user.id]
        if active_only:
            filters.append(PassengerTravellerProfile.is_active.is_(True))

        stmt = (
            select(PassengerTravellerProfile)
            .where(*filters)
            .order_by(
                PassengerTravellerProfile.is_self.desc(),
                PassengerTravellerProfile.created_at.asc(),
            )
        )
        result = await self.db.execute(stmt)
        profiles = list(result.scalars().all())

        return {
            "items": [
                self._serialize_traveller_profile(profile)
                for profile in profiles
            ],
            "count": len(profiles),
        }

    async def create_traveller_profile(
        self,
        current_user: User,
        payload: PassengerTravellerProfileCreateRequest,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        full_name = self._clean_required_text(
            payload.full_name,
            field_name="full_name",
            max_length=120,
        )
        phone = self._clean_required_text(
            payload.phone,
            field_name="phone",
            max_length=20,
        )
        phone, email = self._normalize_traveller_contact(
            phone=phone,
            email=payload.email,
        )

        if not payload.is_self:
            self._ensure_explicit_traveller_is_not_account_owner(
                owner_user=current_user,
                traveller_email=email,
                seat_number=None,
            )

        await self._ensure_traveller_profile_phone_unique(
            owner_user_id=current_user.id,
            phone=phone,
        )

        if payload.is_self:
            await self._clear_existing_self_traveller_profile(current_user.id)

        profile = PassengerTravellerProfile(
            owner_user_id=current_user.id,
            full_name=full_name,
            phone=phone,
            email=email,
            relationship_label=self._clean_optional_text(payload.relationship_label),
            is_self=payload.is_self,
            is_active=True,
        )

        self.db.add(profile)
        await self.db.commit()
        await self.db.refresh(profile)

        return {
            "message": "Traveller profile created successfully.",
            "profile": self._serialize_traveller_profile(profile),
        }

    async def patch_traveller_profile(
        self,
        current_user: User,
        profile_id: str,
        payload: PassengerTravellerProfileUpdateRequest,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        profile = await self._get_traveller_profile_for_owner_or_404(
            owner_user_id=current_user.id,
            profile_id=profile_id,
        )

        if payload.full_name is not None:
            profile.full_name = self._clean_required_text(
                payload.full_name,
                field_name="full_name",
                max_length=120,
            )

        if payload.phone is not None:
            phone = self._clean_required_text(
                payload.phone,
                field_name="phone",
                max_length=20,
            )
            phone, _ = self._normalize_traveller_contact(
                phone=phone,
                email=None,
            )
            await self._ensure_traveller_profile_phone_unique(
                owner_user_id=current_user.id,
                phone=phone,
                except_profile_id=profile.id,
            )
            profile.phone = phone

        if payload.email is not None:
            _, profile.email = self._normalize_traveller_contact(
                phone=profile.phone,
                email=payload.email,
            )

        if payload.relationship_label is not None:
            profile.relationship_label = self._clean_optional_text(
                payload.relationship_label
            )

        if payload.is_active is not None:
            profile.is_active = payload.is_active

        if payload.is_self is not None:
            if payload.is_self:
                await self._clear_existing_self_traveller_profile(
                    current_user.id,
                    except_profile_id=profile.id,
                )
            profile.is_self = payload.is_self

        if not profile.is_self:
            self._ensure_explicit_traveller_is_not_account_owner(
                owner_user=current_user,
                traveller_email=profile.email,
                seat_number=None,
            )

        self.db.add(profile)
        await self.db.commit()
        await self.db.refresh(profile)

        return {
            "message": "Traveller profile updated successfully.",
            "profile": self._serialize_traveller_profile(profile),
        }

    async def delete_traveller_profile(
        self,
        current_user: User,
        profile_id: str,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        profile = await self._get_traveller_profile_for_owner_or_404(
            owner_user_id=current_user.id,
            profile_id=profile_id,
        )

        profile.is_active = False
        self.db.add(profile)
        await self.db.commit()
        await self.db.refresh(profile)

        return {
            "message": "Traveller profile deactivated successfully.",
            "profile": self._serialize_traveller_profile(profile),
        }

    async def _clear_existing_self_traveller_profile(
        self,
        owner_user_id: str,
        *,
        except_profile_id: str | None = None,
    ) -> None:
        filters = [
            PassengerTravellerProfile.owner_user_id == owner_user_id,
            PassengerTravellerProfile.is_self.is_(True),
        ]

        if except_profile_id is not None:
            filters.append(PassengerTravellerProfile.id != except_profile_id)

        stmt = select(PassengerTravellerProfile).where(*filters).with_for_update()
        result = await self.db.execute(stmt)
        profiles = list(result.scalars().all())

        for profile in profiles:
            profile.is_self = False
            self.db.add(profile)

        await self.db.flush()
    
    async def get_rfid_summary(
        self,
        current_user: User,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        me = await self.get_rfid_me(current_user)

        current_ride_stmt = (
            select(RFIDTripRide)
            .where(
                RFIDTripRide.passenger_user_id == current_user.id,
                RFIDTripRide.status == "boarded",
            )
            .order_by(RFIDTripRide.boarded_at.desc())
            .limit(1)
        )
        current_ride_result = await self.db.execute(current_ride_stmt)
        current_ride = current_ride_result.scalar_one_or_none()

        ledger_stmt = (
            select(RFIDLedgerEntry)
            .where(RFIDLedgerEntry.passenger_user_id == current_user.id)
            .order_by(RFIDLedgerEntry.created_at.desc())
            .limit(5)
        )
        ledger_result = await self.db.execute(ledger_stmt)
        recent_ledger_entries = list(ledger_result.scalars().all())

        recharge_stmt = (
            select(RFIDRecharge)
            .where(RFIDRecharge.passenger_user_id == current_user.id)
            .order_by(RFIDRecharge.created_at.desc())
            .limit(5)
        )
        recharge_result = await self.db.execute(recharge_stmt)
        recent_recharges = list(recharge_result.scalars().all())

        ride_stmt = (
            select(RFIDTripRide)
            .where(RFIDTripRide.passenger_user_id == current_user.id)
            .order_by(RFIDTripRide.created_at.desc())
            .limit(5)
        )
        ride_result = await self.db.execute(ride_stmt)
        recent_rides = list(ride_result.scalars().all())

        stop_ids: list[str] = []

        if current_ride is not None:
            stop_ids.append(current_ride.pickup_stop_id)
            if current_ride.dropoff_stop_id is not None:
                stop_ids.append(current_ride.dropoff_stop_id)

        for ride in recent_rides:
            stop_ids.append(ride.pickup_stop_id)
            if ride.dropoff_stop_id is not None:
                stop_ids.append(ride.dropoff_stop_id)

        stops_by_id = await self._get_stops_by_id(stop_ids)

        return {
            "me": me,
            "current_ride": None
            if current_ride is None
            else self._serialize_passenger_rfid_ride(
                current_ride,
                stops_by_id=stops_by_id,
            ),
            "recent_ledger_entries": [
                self._serialize_passenger_rfid_ledger_entry(entry)
                for entry in recent_ledger_entries
            ],
            "recent_recharges": [
                self._serialize_passenger_rfid_recharge(recharge)
                for recharge in recent_recharges
            ],
            "recent_rides": [
                self._serialize_passenger_rfid_ride(
                    ride,
                    stops_by_id=stops_by_id,
                )
                for ride in recent_rides
            ],
            "recent_ledger_entry_count": len(recent_ledger_entries),
            "recent_recharge_count": len(recent_recharges),
            "recent_ride_count": len(recent_rides),
        }
    
    async def get_rfid_me(self, current_user: User) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        card, account, assignment = await self._get_passenger_rfid_card_account(
            passenger_user_id=current_user.id
        )

        return {
            "has_assigned_card": card is not None and account is not None,
            "card": None if card is None else self._serialize_passenger_rfid_card(card),
            "account": None
            if account is None
            else self._serialize_passenger_rfid_account(account),
            "assignment": None
            if assignment is None
            else self._serialize_passenger_rfid_assignment(assignment),
        }

    async def list_rfid_ledger(
        self,
        current_user: User,
        *,
        page: int,
        page_size: int,
        entry_type: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        filters = [
            RFIDLedgerEntry.passenger_user_id == current_user.id,
        ]

        if entry_type is not None:
            filters.append(RFIDLedgerEntry.entry_type == entry_type)

        count_stmt = select(func.count(RFIDLedgerEntry.id)).where(*filters)
        list_stmt = (
            select(RFIDLedgerEntry)
            .where(*filters)
            .order_by(RFIDLedgerEntry.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        count_result = await self.db.execute(count_stmt)
        list_result = await self.db.execute(list_stmt)

        entries = list(list_result.scalars().all())

        return {
            "items": [
                self._serialize_passenger_rfid_ledger_entry(entry)
                for entry in entries
            ],
            "count": int(count_result.scalar_one() or 0),
        }

    async def create_rfid_recharge_order(
        self,
        current_user: User,
        payload: PassengerRFIDRechargeCreateOrderRequest,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        recharge_amount = self._quantize_money(payload.amount)

        if recharge_amount <= Decimal("0.00"):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "non_positive_rfid_recharge_amount",
                    "message": "RFID recharge amount must be greater than zero.",
                },
            )

        card, account, _assignment = await self._get_passenger_rfid_card_account(
            passenger_user_id=current_user.id
        )

        if card is None or account is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_not_assigned",
                    "message": "No RFID card is assigned to this passenger.",
                },
            )

        if not account.is_active:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_account_inactive",
                    "message": "RFID account is inactive.",
                },
            )

        if card.authorization_status != "allowed":
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_card_not_allowed",
                    "message": "RFID card is not allowed for recharge.",
                },
            )

        recharge = RFIDRecharge(
            id=new_id(),
            account_id=account.id,
            card_id=card.id,
            passenger_user_id=current_user.id,
            amount=recharge_amount,
            status=RFIDRechargeStatus.CREATED,
            source_type=RFIDRechargeSourceType.RAZORPAY_USER_RECHARGE,
            created_by_admin_id=None,
        )

        self.db.add(recharge)
        await self.db.flush()

        receipt = f"rfid_recharge_{recharge.id[:24]}"

        payment_order = await self._create_rfid_recharge_razorpay_order(
            amount=recharge_amount,
            receipt=receipt,
            notes={
                "purpose": "rfid_recharge",
                "rfid_recharge_id": recharge.id,
                "rfid_account_id": account.id,
                "rfid_card_id": card.id,
                "passenger_user_id": current_user.id,
            },
        )

        razorpay_order_id = payment_order.get("id")

        if not razorpay_order_id:
            recharge.status = RFIDRechargeStatus.FAILED
            recharge.provider_payload_json = json.dumps(
                payment_order,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )

            self.db.add(recharge)
            await self.db.flush()

            raise HTTPException(
                status_code=502,
                detail={
                    "error": "razorpay_order_id_missing",
                    "message": "Razorpay did not return an order id.",
                    "provider_response": payment_order,
                },
            )

        recharge.razorpay_order_id = razorpay_order_id
        recharge.razorpay_amount = recharge_amount
        recharge.provider_payload_json = json.dumps(
            payment_order,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )

        self.db.add(recharge)
        await self.db.flush()

        return {
            "message": "RFID recharge order created.",
            "recharge": self._serialize_passenger_rfid_recharge(recharge),
            "payment_order": payment_order,
        }

    async def verify_rfid_recharge_payment(
        self,
        current_user: User,
        *,
        recharge_id: str,
        payload: PassengerRFIDRechargeVerifyPaymentRequest,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        recharge = await self._get_rfid_recharge_for_update_or_404(
            recharge_id=recharge_id,
            passenger_user_id=current_user.id,
        )

        if recharge.status == RFIDRechargeStatus.CREDITED:
            account = await self._get_rfid_account_for_update_or_404(
                card_id=recharge.card_id,
                passenger_user_id=current_user.id,
            )

            return {
                "message": "RFID recharge was already credited.",
                "recharge": self._serialize_passenger_rfid_recharge(recharge),
                "account": self._serialize_passenger_rfid_account(account),
            }

        if recharge.status not in {
            RFIDRechargeStatus.CREATED,
            RFIDRechargeStatus.PAID,
        }:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_recharge_not_payable",
                    "message": "RFID recharge is not in a payable state.",
                    "status": recharge.status.value,
                },
            )

        if recharge.razorpay_order_id != payload.razorpay_order_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_recharge_order_mismatch",
                    "message": "Payment order does not match this RFID recharge.",
                },
            )

        self._verify_rfid_recharge_signature(
            razorpay_order_id=payload.razorpay_order_id,
            razorpay_payment_id=payload.razorpay_payment_id,
            razorpay_signature=payload.razorpay_signature,
        )

        account = await self._get_rfid_account_for_update_or_404(
            card_id=recharge.card_id,
            passenger_user_id=current_user.id,
        )

        now = utcnow()
        recharge_amount = self._quantize_money(Decimal(recharge.amount or 0))
        current_balance_before = self._quantize_money(
            Decimal(account.current_balance or 0)
        )
        held_balance = self._quantize_money(Decimal(account.held_balance or 0))
        current_balance_after = self._quantize_money(
            current_balance_before + recharge_amount
        )

        account.current_balance = current_balance_after

        recharge.status = RFIDRechargeStatus.CREDITED
        recharge.razorpay_payment_id = payload.razorpay_payment_id
        recharge.razorpay_signature = payload.razorpay_signature
        recharge.razorpay_status = "paid"
        recharge.razorpay_amount = recharge_amount
        recharge.paid_at = recharge.paid_at or now
        recharge.credited_at = recharge.credited_at or now

        funding_lot = RFIDFundingLot(
            id=new_id(),
            recharge_id=recharge.id,
            account_id=account.id,
            card_id=recharge.card_id,
            source_amount=recharge_amount,
            remaining_amount=recharge_amount,
            razorpay_payment_id=payload.razorpay_payment_id,
            source_type=RFIDFundingLotSourceType.RAZORPAY_PAYMENT,
            status=RFIDFundingLotStatus.AVAILABLE,
        )

        ledger_entry = RFIDLedgerEntry(
            id=new_id(),
            account_id=account.id,
            card_id=recharge.card_id,
            passenger_user_id=current_user.id,
            entry_type=RFIDLedgerEntryType.RECHARGE_CREDIT,
            amount_delta=recharge_amount,
            held_delta=Decimal("0.00"),
            balance_after=current_balance_after,
            held_balance_after=held_balance,
            source_recharge_id=recharge.id,
            razorpay_order_id=payload.razorpay_order_id,
            razorpay_payment_id=payload.razorpay_payment_id,
            note="RFID wallet recharge credited.",
            created_at=now,
        )

        recharge.credited_ledger_entry_id = ledger_entry.id

        self.db.add(account)
        self.db.add(recharge)
        self.db.add(funding_lot)
        self.db.add(ledger_entry)

        await self.db.flush()

        return {
            "message": "RFID recharge credited successfully.",
            "recharge": self._serialize_passenger_rfid_recharge(recharge),
            "account": self._serialize_passenger_rfid_account(account),
        }

    async def list_rfid_recharges(
        self,
        current_user: User,
        *,
        page: int,
        page_size: int,
        status: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        filters = [
            RFIDRecharge.passenger_user_id == current_user.id,
        ]

        if status is not None:
            filters.append(RFIDRecharge.status == status)

        count_stmt = select(func.count(RFIDRecharge.id)).where(*filters)
        list_stmt = (
            select(RFIDRecharge)
            .where(*filters)
            .order_by(RFIDRecharge.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        count_result = await self.db.execute(count_stmt)
        list_result = await self.db.execute(list_stmt)

        recharges = list(list_result.scalars().all())

        return {
            "items": [
                self._serialize_passenger_rfid_recharge(recharge)
                for recharge in recharges
            ],
            "count": int(count_result.scalar_one() or 0),
        }

    async def list_rfid_rides(
        self,
        current_user: User,
        *,
        page: int,
        page_size: int,
        status: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        filters = [
            RFIDTripRide.passenger_user_id == current_user.id,
        ]

        if status is not None:
            filters.append(RFIDTripRide.status == status)

        count_stmt = select(func.count(RFIDTripRide.id)).where(*filters)
        list_stmt = (
            select(RFIDTripRide)
            .where(*filters)
            .order_by(RFIDTripRide.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        count_result = await self.db.execute(count_stmt)
        list_result = await self.db.execute(list_stmt)

        rides = list(list_result.scalars().all())

        stop_ids: list[str] = []
        for ride in rides:
            stop_ids.append(ride.pickup_stop_id)
            if ride.dropoff_stop_id is not None:
                stop_ids.append(ride.dropoff_stop_id)

        stops_by_id = await self._get_stops_by_id(stop_ids)

        return {
            "items": [
                self._serialize_passenger_rfid_ride(
                    ride,
                    stops_by_id=stops_by_id,
                )
                for ride in rides
            ],
            "count": int(count_result.scalar_one() or 0),
        }

    async def get_rfid_ride_detail(
        self,
        current_user: User,
        rfid_ride_id: str,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        ride_stmt = (
            select(RFIDTripRide)
            .where(
                RFIDTripRide.id == rfid_ride_id,
                RFIDTripRide.passenger_user_id == current_user.id,
            )
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

        stop_ids = [ride.pickup_stop_id]
        if ride.dropoff_stop_id is not None:
            stop_ids.append(ride.dropoff_stop_id)

        stops_by_id = await self._get_stops_by_id(stop_ids)

        ledger_stmt = (
            select(RFIDLedgerEntry)
            .where(
                RFIDLedgerEntry.rfid_ride_id == ride.id,
                RFIDLedgerEntry.passenger_user_id == current_user.id,
            )
            .order_by(RFIDLedgerEntry.created_at.asc())
        )
        ledger_result = await self.db.execute(ledger_stmt)
        ledger_entries = list(ledger_result.scalars().all())

        recharge_ids = [
            entry.source_recharge_id
            for entry in ledger_entries
            if entry.source_recharge_id is not None
        ]

        recharges: list[RFIDRecharge] = []

        if recharge_ids:
            recharge_stmt = (
                select(RFIDRecharge)
                .where(
                    RFIDRecharge.id.in_(list(set(recharge_ids))),
                    RFIDRecharge.passenger_user_id == current_user.id,
                )
                .order_by(RFIDRecharge.created_at.asc())
            )
            recharge_result = await self.db.execute(recharge_stmt)
            recharges = list(recharge_result.scalars().all())

        return {
            "ride": self._serialize_passenger_rfid_ride(
                ride,
                stops_by_id=stops_by_id,
            ),
            "ledger_entries": [
                self._serialize_passenger_rfid_ledger_entry(entry)
                for entry in ledger_entries
            ],
            "recharges": [
                self._serialize_passenger_rfid_recharge(recharge)
                for recharge in recharges
            ],
        }

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------

    async def discover_route_trip_options(
        self,
        *,
        from_stop_id: str,
        to_stop_id: str,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
    ) -> dict[str, Any]:
        if from_stop_id == to_stop_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "same_pickup_dropoff",
                    "message": "From stop and to stop must be different.",
                },
            )

        normalized_from_time = self._normalize_optional_datetime(from_time)
        normalized_to_time = self._normalize_optional_datetime(to_time)

        if (
            normalized_from_time is not None
            and normalized_to_time is not None
            and normalized_from_time > normalized_to_time
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_time_window",
                    "message": "from_time cannot be greater than to_time.",
                },
            )

        await self._get_stop_obj_or_raise(from_stop_id)
        await self._get_stop_obj_or_raise(to_stop_id)

        lower_bound = utcnow()
        if normalized_from_time is not None and normalized_from_time > lower_bound:
            lower_bound = normalized_from_time

        stmt = (
            select(RouteFare)
            .join(Route, Route.id == RouteFare.route_id)
            .where(
                RouteFare.pickup_stop_id == from_stop_id,
                RouteFare.dropoff_stop_id == to_stop_id,
                RouteFare.is_active.is_(True),
                Route.is_active.is_(True),
            )
            .options(
                selectinload(RouteFare.pickup_stop),
                selectinload(RouteFare.dropoff_stop),
                selectinload(RouteFare.route)
                .selectinload(Route.route_stops)
                .selectinload(RouteStop.stop),
                selectinload(RouteFare.route)
                .selectinload(Route.scheduled_trips)
                .selectinload(ScheduledTrip.vehicle),
                selectinload(RouteFare.route)
                .selectinload(Route.scheduled_trips)
                .selectinload(ScheduledTrip.driver),
                selectinload(RouteFare.route)
                .selectinload(Route.scheduled_trips)
                .selectinload(ScheduledTrip.trip_events)
                .selectinload(TripEvent.stop),
            )
            .order_by(Route.name.asc(), RouteFare.amount.asc())
        )

        result = await self.db.execute(stmt)
        segment_fares = result.scalars().unique().all()

        items: list[dict[str, Any]] = []

        for segment_fare in segment_fares:
            route = segment_fare.route

            route_stop_by_stop_id = {
                route_stop.stop_id: route_stop
                for route_stop in route.route_stops
            }

            pickup_route_stop = route_stop_by_stop_id.get(from_stop_id)
            dropoff_route_stop = route_stop_by_stop_id.get(to_stop_id)

            if pickup_route_stop is None or dropoff_route_stop is None:
                continue

            if pickup_route_stop.sequence_no >= dropoff_route_stop.sequence_no:
                continue

            if not pickup_route_stop.boarding_allowed:
                continue

            if not dropoff_route_stop.deboarding_allowed:
                continue

            upcoming_scheduled_trips: list[dict[str, Any]] = []

            for trip in sorted(route.scheduled_trips, key=lambda item: item.planned_start_at):
                if trip.status != ScheduledTripStatus.SCHEDULED:
                    continue

                pickup_planned_time = self._get_route_stop_planned_time(
                    trip=trip,
                    target_sequence_no=pickup_route_stop.sequence_no,
                )

                if pickup_planned_time < lower_bound:
                    continue

                if normalized_to_time is not None and pickup_planned_time > normalized_to_time:
                    continue

                upcoming_scheduled_trips.append(
                    await self._serialize_route_trip_discovery_trip(
                        trip=trip,
                        pickup_route_stop=pickup_route_stop,
                        dropoff_route_stop=dropoff_route_stop,
                    )
                )

            configured_fare_amount = self._quantize_money(segment_fare.amount)
            gst_breakdown = await self._build_gst_breakdown(
                configured_fare_amount,
                is_ac=route.has_ac,
            )

            items.append(
                {
                    "route": self._serialize_route(route),
                    "pickup_stop": self._serialize_stop_brief(pickup_route_stop.stop),
                    "dropoff_stop": self._serialize_stop_brief(dropoff_route_stop.stop),
                    "pickup_sequence_no": pickup_route_stop.sequence_no,
                    "dropoff_sequence_no": dropoff_route_stop.sequence_no,
                    **self._gst_breakdown_public_fields(
                        gst_breakdown,
                        configured_fare_amount=configured_fare_amount,
                    ),
                    "upcoming_scheduled_trips": upcoming_scheduled_trips,
                    "upcoming_scheduled_trip_count": len(upcoming_scheduled_trips),
                }
            )

        return {
            "from_stop_id": from_stop_id,
            "to_stop_id": to_stop_id,
            "from_time": normalized_from_time,
            "to_time": normalized_to_time,
            "items": items,
            "count": len(items),
        }

    async def discover_rfid_route_trip_options(
        self,
        current_user: User,
        *,
        from_stop_id: str,
        to_stop_id: str,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        card, account, _assignment = (
            await self._ensure_active_passenger_rfid_card_account(current_user)
        )

        available_balance = self._available_rfid_balance(account)

        base_response = await self.discover_route_trip_options(
            from_stop_id=from_stop_id,
            to_stop_id=to_stop_id,
            from_time=from_time,
            to_time=to_time,
        )

        base_items = list(base_response.get("items", []))

        if not base_items:
            return {
                "from_stop_id": from_stop_id,
                "to_stop_id": to_stop_id,
                "from_time": from_time,
                "to_time": to_time,
                "has_active_rfid": True,
                "rfid_card_id": card.id,
                "rfid_account_id": account.id,
                "rfid_available_balance": available_balance,
                "items": [],
                "count": 0,
            }

        options_by_route_id: dict[str, dict[str, Any]] = {}

        for option in base_items:
            route_id = option["route"]["id"]
            option["rfid_in_progress_trips"] = []
            option["rfid_in_progress_trip_count"] = 0
            options_by_route_id[route_id] = option

        route_ids = list(options_by_route_id.keys())

        trip_stmt = (
            select(ScheduledTrip)
            .where(
                ScheduledTrip.route_id.in_(route_ids),
                ScheduledTrip.status == ScheduledTripStatus.IN_PROGRESS,
            )
            .options(
                selectinload(ScheduledTrip.route)
                .selectinload(Route.route_stops)
                .selectinload(RouteStop.stop),
                selectinload(ScheduledTrip.vehicle),
                selectinload(ScheduledTrip.driver),
                selectinload(ScheduledTrip.trip_events).selectinload(TripEvent.stop),
            )
            .order_by(ScheduledTrip.planned_start_at.asc())
        )

        if from_time is not None:
            trip_stmt = trip_stmt.where(ScheduledTrip.planned_end_at >= from_time)

        if to_time is not None:
            trip_stmt = trip_stmt.where(ScheduledTrip.planned_start_at <= to_time)

        trip_result = await self.db.execute(trip_stmt)
        in_progress_trips = list(trip_result.scalars().unique().all())

        for trip in in_progress_trips:
            option = options_by_route_id.get(trip.route_id)

            if option is None:
                continue

            pickup_sequence_no = int(option["pickup_sequence_no"])
            dropoff_sequence_no = int(option["dropoff_sequence_no"])

            active_event = self._get_loaded_active_trip_event(trip)

            if active_event is None:
                continue

            active_route_stop = self._get_loaded_route_stop_by_stop_id(
                trip,
                active_event.stop_id,
            )

            if active_route_stop is None:
                continue

            current_sequence_no = int(active_route_stop.sequence_no)

            if current_sequence_no > pickup_sequence_no:
                continue

            pickup_route_stop = self._get_loaded_route_stop_by_stop_id(
                trip,
                from_stop_id,
            )
            dropoff_route_stop = self._get_loaded_route_stop_by_stop_id(
                trip,
                to_stop_id,
            )

            if pickup_route_stop is None or dropoff_route_stop is None:
                continue

            if not pickup_route_stop.boarding_allowed:
                continue

            if not dropoff_route_stop.deboarding_allowed:
                continue

            rfid_capacity = await self._get_rfid_capacity_for_in_progress_discovery(
                trip=trip,
            )

            reserved_capacity = int(rfid_capacity["rfid_reserved_seat_count"])
            occupied_count = int(rfid_capacity["rfid_occupied_seat_count"])
            available_seat_count = int(rfid_capacity["rfid_available_seat_count"])
            seat_available = bool(rfid_capacity["rfid_seat_available"])

            required_hold_amount = await self._get_rfid_max_downstream_fare_from_stop(
                route_id=trip.route_id,
                pickup_stop_id=from_stop_id,
                pickup_sequence_no=pickup_sequence_no,
                is_ac=None if trip.route is None else trip.route.has_ac,
            )

            selected_fare_amount = self._quantize_money(
                Decimal(option["fare_amount"] or 0)
            )
            selected_taxable_amount = self._quantize_money(
                Decimal(option.get("taxable_amount") or selected_fare_amount)
            )
            selected_cgst_amount = self._quantize_money(
                Decimal(option.get("cgst_amount") or 0)
            )
            selected_sgst_amount = self._quantize_money(
                Decimal(option.get("sgst_amount") or 0)
            )
            selected_igst_amount = self._quantize_money(
                Decimal(option.get("igst_amount") or 0)
            )
            selected_total_tax_amount = self._quantize_money(
                Decimal(option.get("total_tax_amount") or 0)
            )

            if required_hold_amount is None:
                balance_shortfall = Decimal("0.00")
                balance_sufficient = False
            else:
                balance_shortfall = self._quantize_money(
                    max(required_hold_amount - available_balance, Decimal("0.00"))
                )
                balance_sufficient = available_balance >= required_hold_amount

            already_boarded_on_trip = await self._has_open_rfid_ride_for_card_on_trip(
                card_id=card.id,
                scheduled_trip_id=trip.id,
            )

            unavailable_reason = None

            if already_boarded_on_trip:
                unavailable_reason = "already_boarded_on_trip"
            elif not seat_available:
                unavailable_reason = "rfid_seat_pool_full"
            elif required_hold_amount is None:
                unavailable_reason = "rfid_downstream_fare_not_configured"
            elif not balance_sufficient:
                unavailable_reason = "rfid_insufficient_balance_for_max_route_fare"

            rfid_can_avail = unavailable_reason is None
            rfid_can_board_now = (
                rfid_can_avail and current_sequence_no == pickup_sequence_no
            )

            vehicle_payload = None
            if trip.vehicle is not None:
                vehicle_payload = {
                    "id": trip.vehicle.id,
                    "registration_number": trip.vehicle.registration_number,
                    "vehicle_name": trip.vehicle.vehicle_name,
                    "vehicle_model": trip.vehicle.vehicle_model,
                    "color": trip.vehicle.color,
                    "seat_count": trip.vehicle.seat_count,
                    "rfid_reserved_seat_count": reserved_capacity,
                    "app_bookable_seat_count": await self._get_app_bookable_capacity_for_trip(trip),
                    "has_ac": trip.vehicle.has_ac,
                }

            driver_payload = None
            if trip.driver is not None:
                driver_payload = {
                    "id": trip.driver.id,
                    "email": trip.driver.email,
                }

            current_stop = active_event.stop or active_route_stop.stop

            option["rfid_in_progress_trips"].append(
                {
                    "scheduled_trip_id": trip.id,
                    "route_id": trip.route_id,
                    "status": trip.status,
                    "planned_start_at": trip.planned_start_at,
                    "planned_end_at": trip.planned_end_at,
                    "actual_start_at": trip.actual_start_at,
                    "actual_end_at": trip.actual_end_at,
                    "pickup_stop": option["pickup_stop"],
                    "dropoff_stop": option["dropoff_stop"],
                    "current_stop": None
                    if current_stop is None
                    else self._serialize_stop_brief(current_stop),
                    "pickup_sequence_no": pickup_sequence_no,
                    "dropoff_sequence_no": dropoff_sequence_no,
                    "current_sequence_no": current_sequence_no,
                    "selected_fare_amount": selected_fare_amount,
                    "selected_taxable_amount": selected_taxable_amount,
                    "selected_cgst_amount": selected_cgst_amount,
                    "selected_sgst_amount": selected_sgst_amount,
                    "selected_igst_amount": selected_igst_amount,
                    "selected_total_tax_amount": selected_total_tax_amount,
                    "required_hold_amount": required_hold_amount,
                    "available_balance": available_balance,
                    "balance_shortfall": balance_shortfall,
                    "minimum_recharge_amount": balance_shortfall,
                    "rfid_seat_policy": rfid_capacity["rfid_seat_policy"],
                    "rfid_physical_seat_check_required": rfid_capacity[
                        "rfid_physical_seat_check_required"
                    ],
                    "rfid_reserved_seat_count": reserved_capacity,
                    "rfid_occupied_seat_count": occupied_count,
                    "rfid_available_seat_count": available_seat_count,
                    "rfid_seat_available": seat_available,
                    "rfid_balance_sufficient": balance_sufficient,
                    "rfid_can_avail": rfid_can_avail,
                    "rfid_can_board_now": rfid_can_board_now,
                    "rfid_unavailable_reason": unavailable_reason,
                    "vehicle": vehicle_payload,
                    "driver": driver_payload,
                }
            )

        for option in base_items:
            option["rfid_in_progress_trip_count"] = len(
                option["rfid_in_progress_trips"]
            )

        return {
            "from_stop_id": from_stop_id,
            "to_stop_id": to_stop_id,
            "from_time": from_time,
            "to_time": to_time,
            "has_active_rfid": True,
            "rfid_card_id": card.id,
            "rfid_account_id": account.id,
            "rfid_available_balance": available_balance,
            "items": base_items,
            "count": len(base_items),
        }

    async def list_stops(self, *, active_only: bool = True) -> dict[str, Any]:
        stmt = select(Stop).order_by(Stop.name.asc())

        if active_only:
            stmt = stmt.where(Stop.is_active.is_(True))

        result = await self.db.execute(stmt)
        stops = result.scalars().all()

        return {
            "items": [self._serialize_stop_brief(stop) for stop in stops],
            "count": len(stops),
        }
    
    async def list_routes(
    self,
    *,
    active_only: bool = True,
    has_ac: bool | None = None,
) -> dict[str, Any]:
        stmt = (
            select(Route)
            .options(selectinload(Route.route_stops).selectinload(RouteStop.stop))
            .order_by(Route.name.asc())
        )

        if active_only:
            stmt = stmt.where(Route.is_active.is_(True))

        if has_ac is not None:
            stmt = stmt.where(Route.has_ac.is_(has_ac))

        result = await self.db.execute(stmt)
        routes = result.scalars().unique().all()

        return {
            "items": [self._serialize_route(route) for route in routes],
            "count": len(routes),
        }

    async def get_route_detail(self, route_id: str) -> dict[str, Any]:
        route = await self._get_route_obj(route_id)
        return self._serialize_route(route)

    async def list_scheduled_trips(
        self,
        *,
        route_id: str | None = None,
        only_future: bool = True,
    ) -> dict[str, Any]:
        stmt = (
            select(ScheduledTrip)
                .options(
                selectinload(ScheduledTrip.route)
                .selectinload(Route.route_stops)
                .selectinload(RouteStop.stop),
                selectinload(ScheduledTrip.vehicle),
                selectinload(ScheduledTrip.driver),
                selectinload(ScheduledTrip.trip_events).selectinload(TripEvent.stop),
            )
            .order_by(ScheduledTrip.planned_start_at.asc())
        )

        if route_id:
            stmt = stmt.where(ScheduledTrip.route_id == route_id)

        if only_future:
            stmt = stmt.where(
                ScheduledTrip.planned_start_at >= utcnow(),
                ScheduledTrip.status == ScheduledTripStatus.SCHEDULED,
            )

        result = await self.db.execute(stmt)
        trips = result.scalars().unique().all()

        items = []
        for trip in trips:
            items.append(await self._serialize_trip(trip))

        return {"items": items, "count": len(items)}

    async def get_scheduled_trip_detail(self, trip_id: str) -> dict[str, Any]:
        trip = await self._get_trip_obj(trip_id)
        return await self._serialize_trip(trip)
    
    async def get_scheduled_trip_driver_vehicle_info(
        self,
        trip_id: str,
    ) -> dict[str, Any]:
        trip_stmt = (
            select(ScheduledTrip)
            .where(ScheduledTrip.id == trip_id)
            .options(
                selectinload(ScheduledTrip.vehicle),
                selectinload(ScheduledTrip.driver).selectinload(User.driver_profile),
            )
        )
        trip_result = await self.db.execute(trip_stmt)
        trip = trip_result.scalar_one_or_none()

        if trip is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "scheduled_trip_not_found",
                    "message": "Scheduled trip not found.",
                },
            )

        rating_stmt = select(
            cast(func.avg(BookingRating.driver_rating), Numeric(3, 2)),
            func.count(BookingRating.id),
        ).where(
            BookingRating.driver_user_id == trip.driver_user_id
        )
        rating_result = await self.db.execute(rating_stmt)
        avg_rating, rating_count = rating_result.one()

        driver_profile = None
        if trip.driver is not None:
            driver_profile = trip.driver.driver_profile

        return {
            "scheduled_trip_id": trip.id,
            "driver_user_id": trip.driver_user_id,
            "driver_name": None if driver_profile is None else driver_profile.full_name,
            "driver_average_rating": avg_rating,
            "driver_rating_count": int(rating_count or 0),
            "vehicle_registration_number": None if trip.vehicle is None else trip.vehicle.registration_number,
            "vehicle_name": None if trip.vehicle is None else trip.vehicle.vehicle_name,
            "vehicle_model": None if trip.vehicle is None else trip.vehicle.vehicle_model,
            "vehicle_color": None if trip.vehicle is None else trip.vehicle.color,
            "vehicle_total_seat": None if trip.vehicle.seat_count is None else trip.vehicle.seat_count
        }

    async def preview_fare(self, payload: FarePreviewRequest) -> dict[str, Any]:
        route = await self._get_route_obj(payload.route_id)
        fare, pickup_route_stop, dropoff_route_stop = await self._resolve_fare(
            route_id=payload.route_id,
            pickup_stop_id=payload.pickup_stop_id,
            dropoff_stop_id=payload.dropoff_stop_id,
        )

        pickup_stop = next(
            rs.stop for rs in route.route_stops if rs.stop_id == payload.pickup_stop_id
        )
        dropoff_stop = next(
            rs.stop for rs in route.route_stops if rs.stop_id == payload.dropoff_stop_id
        )
        gst_breakdown = await self._build_gst_breakdown(
            self._quantize_money(fare.amount),
            is_ac=route.has_ac,
        )

        return {
            "route_id": route.id,
            "route_name": route.name,
            "route_code": route.code,
            "has_ac": route.has_ac,
            "pickup_stop": self._serialize_stop_brief(pickup_stop),
            "dropoff_stop": self._serialize_stop_brief(dropoff_stop),
            "pickup_sequence_no": pickup_route_stop.sequence_no,
            "dropoff_sequence_no": dropoff_route_stop.sequence_no,
            "amount": gst_breakdown.gross_amount,
            "configured_fare_amount": self._quantize_money(fare.amount),
            "taxable_amount": gst_breakdown.taxable_amount,
            "cgst_rate_percent": gst_breakdown.cgst_rate_percent,
            "cgst_amount": gst_breakdown.cgst_amount,
            "sgst_rate_percent": gst_breakdown.sgst_rate_percent,
            "sgst_amount": gst_breakdown.sgst_amount,
            "igst_rate_percent": gst_breakdown.igst_rate_percent,
            "igst_amount": gst_breakdown.igst_amount,
            "total_tax_amount": gst_breakdown.total_tax_amount,
            "gst_enabled": gst_breakdown.gst_enabled,
            "gst_applicable": gst_breakdown.gst_applicable,
            "gst_inclusive": gst_breakdown.gst_inclusive,
        }
    
    async def get_leg_available_seats(
        self,
        current_user: User,
        trip_id: str,
        payload: LegAvailableSeatsRequest,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        trip = await self._get_trip_obj(trip_id)

        if payload.route_id != trip.route_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "route_trip_mismatch",
                    "message": "Provided route does not match the scheduled trip.",
                },
            )

        fare, pickup_route_stop, dropoff_route_stop = await self._resolve_fare(
            route_id=payload.route_id,
            pickup_stop_id=payload.pickup_stop_id,
            dropoff_stop_id=payload.dropoff_stop_id,
        )

        seat_capacity = await self._get_app_bookable_capacity_for_trip(trip)

        overlapping_active_bookings = await self._count_overlapping_active_trip_bookings(
            scheduled_trip_id=trip.id,
            pickup_sequence_no=pickup_route_stop.sequence_no,
            dropoff_sequence_no=dropoff_route_stop.sequence_no,
        )

        occupied_seat_numbers = await self._get_occupied_app_seat_numbers_for_leg(
            scheduled_trip_id=trip.id,
            pickup_sequence_no=pickup_route_stop.sequence_no,
            dropoff_sequence_no=dropoff_route_stop.sequence_no,
        )

        available_seat_numbers = [
            seat_number
            for seat_number in range(1, seat_capacity + 1)
            if seat_number not in occupied_seat_numbers
        ]

        available_seats = len(available_seat_numbers)

        requested_seat_available = None
        if payload.seat_number is not None:
            requested_seat_available = payload.seat_number in available_seat_numbers

        trip_bookable = (
            trip.status == ScheduledTripStatus.SCHEDULED
            and trip.planned_start_at > utcnow()
            and available_seats > 0
        )

        return {
            "scheduled_trip_id": trip.id,
            "route_id": trip.route_id,
            "pickup_stop_id": payload.pickup_stop_id,
            "dropoff_stop_id": payload.dropoff_stop_id,
            "pickup_sequence_no": pickup_route_stop.sequence_no,
            "dropoff_sequence_no": dropoff_route_stop.sequence_no,
            "seat_capacity": seat_capacity,
            "overlapping_active_bookings": overlapping_active_bookings,
            "available_seats": available_seats,
            "occupied_seat_numbers": sorted(occupied_seat_numbers),
            "available_seat_numbers": available_seat_numbers,
            "requested_seat_available": requested_seat_available,
            "trip_bookable": trip_bookable,
        }

    # ------------------------------------------------------------------
    # razorpay helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _get_razorpay_key_id() -> str:
        value = os.getenv("RAZORPAY_KEY_ID", "").strip()
        if not value:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "missing_razorpay_key_id",
                    "message": "RAZORPAY_KEY_ID is not configured.",
                },
            )
        return value

    @staticmethod
    def _get_razorpay_key_secret() -> str:
        value = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
        if not value:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "missing_razorpay_key_secret",
                    "message": "RAZORPAY_KEY_SECRET is not configured.",
                },
            )
        return value

    @staticmethod
    def _get_razorpay_base_url() -> str:
        return os.getenv("RAZORPAY_BASE_URL", "https://api.razorpay.com/v1").strip().rstrip("/")

    async def _razorpay_request(
        self,
        *,
        method: str,
        path: str,
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        key_id = self._get_razorpay_key_id()
        key_secret = self._get_razorpay_key_secret()
        base_url = self._get_razorpay_base_url()

        url = f"{base_url}{path}"

        async with httpx.AsyncClient(auth=(key_id, key_secret), timeout=20.0) as client:
            response = await client.request(method=method, url=url, json=json_payload)

        if response.status_code >= 400:
            try:
                error_payload = response.json()
            except Exception:
                error_payload = {"raw": response.text}

            raise HTTPException(
                status_code=502,
                detail={
                    "error": "razorpay_request_failed",
                    "message": "Razorpay request failed.",
                    "provider_status_code": response.status_code,
                    "provider_response": error_payload,
                },
            )

        return response.json()
    
    async def _create_booking_session_razorpay_order(
        self,
        *,
        booking_session: BookingSession,
        amount: Decimal,
    ) -> dict[str, Any]:
        amount_subunits = self._to_subunits(amount)
        receipt = f"booking_session_{booking_session.id.replace('-', '')[:20]}"

        payload = {
            "amount": amount_subunits,
            "currency": "INR",
            "receipt": receipt,
            "notes": {
                "booking_session_id": booking_session.id,
                "scheduled_trip_id": booking_session.scheduled_trip_id,
                "owner_user_id": booking_session.owner_user_id,
            },
        }

        return await self._razorpay_request(
            method="POST",
            path="/orders",
            json_payload=payload,
        )

    def _build_booking_session_payment_order_response(
        self,
        *,
        booking_session: BookingSession,
        razorpay_order_id: str,
        currency: str = "INR",
        receipt: str | None = None,
    ) -> dict[str, Any]:
        return {
            "provider": "razorpay",
            "razorpay_key_id": self._get_razorpay_key_id(),
            "razorpay_order_id": razorpay_order_id,
            "amount": booking_session.total_fare_amount,
            "amount_subunits": self._to_subunits(booking_session.total_fare_amount),
            "currency": currency or "INR",
            "receipt": receipt,
        }

    @staticmethod
    def _verify_razorpay_webhook_signature(
        raw_body: bytes,
        received_signature: str | None,
    ) -> None:
        secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()
        if not secret:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "razorpay_webhook_not_configured",
                    "message": "RAZORPAY_WEBHOOK_SECRET is not configured.",
                },
            )

        signature = (received_signature or "").strip()
        generated_signature = hmac.new(
            secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        if not signature or not hmac.compare_digest(
            generated_signature,
            signature,
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_razorpay_webhook_signature",
                    "message": "Razorpay webhook signature verification failed.",
                },
            )

    async def handle_booking_session_payment_webhook(
        self,
        *,
        raw_body: bytes,
        received_signature: str | None,
    ) -> dict[str, Any]:
        self._verify_razorpay_webhook_signature(
            raw_body,
            received_signature,
        )

        try:
            event_payload = json.loads(raw_body)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_razorpay_webhook_payload",
                    "message": "Razorpay webhook body must be valid JSON.",
                },
            ) from exc

        if not isinstance(event_payload, dict):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_razorpay_webhook_payload",
                    "message": "Razorpay webhook body must be a JSON object.",
                },
            )

        event_name = str(event_payload.get("event") or "").strip().lower()
        supported_events = {
            "order.paid",
            "payment.authorized",
            "payment.captured",
            "payment.failed",
        }
        if event_name not in supported_events:
            return {
                "message": "Webhook event ignored.",
                "event": event_name,
                "outcome": "ignored_unsupported_event",
            }

        payload = event_payload.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        payment_container = payload.get("payment")
        payment_entity = (
            payment_container.get("entity")
            if isinstance(payment_container, dict)
            else None
        )
        if not isinstance(payment_entity, dict):
            payment_entity = {}
        order_container = payload.get("order")
        order_entity = (
            order_container.get("entity")
            if isinstance(order_container, dict)
            else None
        )
        if not isinstance(order_entity, dict):
            order_entity = {}

        razorpay_order_id = str(
            payment_entity.get("order_id") or order_entity.get("id") or ""
        ).strip()
        if not razorpay_order_id:
            return {
                "message": "Webhook event ignored because it has no order id.",
                "event": event_name,
                "outcome": "ignored_missing_order_id",
            }

        payment_lookup_result = await self.db.execute(
            select(BookingSessionPayment.booking_session_id)
            .where(
                BookingSessionPayment.razorpay_order_id == razorpay_order_id
            )
        )
        booking_session_id = payment_lookup_result.scalar_one_or_none()
        if booking_session_id is None:
            await self.db.rollback()
            return {
                "message": "Webhook event does not belong to a booking session.",
                "event": event_name,
                "outcome": "ignored_unknown_booking_session_order",
            }

        session_result = await self.db.execute(
            select(BookingSession)
            .where(BookingSession.id == booking_session_id)
            .with_for_update()
        )
        booking_session = session_result.scalar_one_or_none()
        if booking_session is None:
            await self.db.rollback()
            return {
                "message": "Webhook booking session no longer exists.",
                "event": event_name,
                "outcome": "ignored_missing_booking_session",
            }

        payments = await self._list_booking_session_payments_for_update(
            booking_session.id
        )
        payment = self._get_booking_session_payment_by_order_id(
            payments,
            razorpay_order_id=razorpay_order_id,
        )
        if payment is None:
            await self.db.rollback()
            return {
                "message": "Webhook payment order no longer exists.",
                "event": event_name,
                "outcome": "ignored_missing_booking_session_payment",
            }

        provider_status = str(
            payment_entity.get("status") or ""
        ).strip().lower()
        captured = bool(payment_entity.get("captured", False))
        is_success = (
            event_name in {"order.paid", "payment.captured"}
            or provider_status == "captured"
            or captured
        )

        if not is_success:
            if event_name == "payment.failed" and (
                booking_session.status == BookingSessionStatus.PENDING_PAYMENT
            ):
                # A failed payment attempt does not make the Razorpay order
                # unusable. Keep the order locally retryable.
                payment.status = BookingPaymentStatus.CREATED
                self.db.add(payment)
                await self.db.commit()
            else:
                await self.db.rollback()
            return {
                "message": "Webhook event recorded; no terminal payment state change.",
                "event": event_name,
                "booking_session_id": booking_session.id,
                "outcome": f"pending_with_{provider_status or event_name.replace('.', '_')}",
            }

        try:
            provider_amount = int(payment_entity.get("amount"))
        except (TypeError, ValueError) as exc:
            await self.db.rollback()
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_razorpay_webhook_amount",
                    "message": "Captured Razorpay webhook did not contain a valid amount.",
                },
            ) from exc

        if provider_amount != self._to_subunits(payment.amount):
            await self.db.rollback()
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "payment_amount_mismatch",
                    "message": (
                        "Captured webhook amount does not match the booking "
                        "session payment."
                    ),
                },
            )

        razorpay_payment_id = str(payment_entity.get("id") or "").strip()
        if not razorpay_payment_id:
            await self.db.rollback()
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_razorpay_payment_id",
                    "message": "Captured webhook did not contain a payment id.",
                },
            )

        bookings = await self._list_booking_session_bookings_for_update(
            booking_session.id
        )
        hold_expired = (
            booking_session.payment_hold_expires_at is not None
            and booking_session.payment_hold_expires_at <= utcnow()
        )

        if booking_session.status == BookingSessionStatus.PENDING_PAYMENT:
            if hold_expired:
                await self._mark_booking_session_paid_but_expired(
                    booking_session=booking_session,
                    payment=payment,
                    bookings=bookings,
                    payments=payments,
                    razorpay_payment_id=razorpay_payment_id,
                )
                outcome = "captured_after_hold_expiry"
            else:
                await self._mark_booking_session_paid_and_confirmed(
                    booking_session=booking_session,
                    payment=payment,
                    bookings=bookings,
                    razorpay_payment_id=razorpay_payment_id,
                )
                await self._queue_booking_session_traveller_notifications(
                    booking_session=booking_session,
                    bookings=bookings,
                    event_type="traveller_seat_confirmed",
                )
                outcome = "confirmed_from_webhook"
        elif booking_session.status in {
            BookingSessionStatus.EXPIRED,
            BookingSessionStatus.CANCELLED,
        }:
            late_payment_already_handled = (
                payment.razorpay_payment_id == razorpay_payment_id
                and payment.status
                in {
                    BookingPaymentStatus.PAID,
                    BookingPaymentStatus.REFUNDED,
                }
                and payment.refund_requested_at is not None
            )
            if late_payment_already_handled:
                outcome = "late_payment_already_handled"
            else:
                await self._mark_booking_session_paid_but_expired(
                    booking_session=booking_session,
                    payment=payment,
                    bookings=bookings,
                    payments=payments,
                    razorpay_payment_id=razorpay_payment_id,
                )
                outcome = "captured_after_session_closed"
        else:
            payment.razorpay_payment_id = (
                payment.razorpay_payment_id or razorpay_payment_id
            )
            payment.status = BookingPaymentStatus.PAID
            self.db.add(payment)
            for confirmed_booking in bookings:
                if confirmed_booking.booking_status in {
                    BookingStatus.BOOKED,
                    BookingStatus.BOARDED,
                    BookingStatus.COMPLETED,
                }:
                    await self._queue_invoice_email_delivery(confirmed_booking)
            outcome = "already_confirmed"

        await self.db.commit()

        if outcome == "confirmed_from_webhook":
            await self._notify_user(
                user_id=booking_session.owner_user_id,
                title="Payment verified",
                message="Your booking session is confirmed.",
                data={
                    "type": "booking_session_confirmed",
                    "booking_session_id": booking_session.id,
                    "scheduled_trip_id": booking_session.scheduled_trip_id,
                    "refresh": [
                        "bookings_list",
                        "booking_session_detail",
                        "current_booking",
                        "seatmap",
                    ],
                },
            )
        elif outcome in {
            "captured_after_hold_expiry",
            "captured_after_session_closed",
        }:
            await self._notify_user(
                user_id=booking_session.owner_user_id,
                title="Late payment refund requested",
                message=(
                    "The payment arrived after the booking session closed. "
                    "The seats remain released and a refund was requested."
                ),
                data={
                    "type": "booking_session_late_payment_refund_requested",
                    "booking_session_id": booking_session.id,
                    "scheduled_trip_id": booking_session.scheduled_trip_id,
                    "refresh": [
                        "bookings_list",
                        "booking_session_detail",
                        "seatmap",
                    ],
                },
            )

        if outcome not in {
            "already_confirmed",
            "late_payment_already_handled",
        }:
            await self._broadcast_seatmap_snapshots_for_trip(
                scheduled_trip_id=booking_session.scheduled_trip_id,
                reason=f"booking_session_payment_webhook:{outcome}",
            )

        return {
            "message": "Booking session payment webhook processed.",
            "event": event_name,
            "booking_session_id": booking_session.id,
            "scheduled_trip_id": booking_session.scheduled_trip_id,
            "owner_user_id": booking_session.owner_user_id,
            "route_id": booking_session.route_id,
            "outcome": outcome,
        }

    async def _create_razorpay_order(
        self,
        *,
        booking: TripBooking,
        amount: Decimal,
    ) -> dict[str, Any]:
        amount_subunits = self._to_subunits(amount)
        receipt = f"booking_{booking.id.replace('-', '')[:24]}"

        payload = {
            "amount": amount_subunits,
            "currency": "INR",
            "receipt": receipt,
            "notes": {
                "booking_id": booking.id,
                "scheduled_trip_id": booking.scheduled_trip_id,
                "passenger_user_id": booking.passenger_user_id,
            },
        }
        return await self._razorpay_request(method="POST", path="/orders", json_payload=payload)

    async def _fetch_razorpay_payment(self, payment_id: str) -> dict[str, Any]:
        return await self._razorpay_request(method="GET", path=f"/payments/{payment_id}")
    
    async def _fetch_razorpay_order_payments(self, order_id: str) -> list[dict[str, Any]]:
        payload = await self._razorpay_request(
            method="GET",
            path=f"/orders/{order_id}/payments",
        )

        items = payload.get("items")
        if not isinstance(items, list):
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "invalid_razorpay_order_payments_response",
                    "message": "Razorpay order payments response did not contain a valid items list.",
                },
            )

        return items

    @staticmethod
    def _select_best_razorpay_order_payment(
        items: list[dict[str, Any]],
        *,
        expected_order_id: str,
        expected_amount_subunits: int,
    ) -> dict[str, Any] | None:
        priority = {
            "captured": 50,
            "authorized": 40,
            "created": 30,
            "failed": 20,
            "refunded": 10,
        }

        candidates: list[tuple[int, int, dict[str, Any]]] = []

        for item in items:
            order_id = str(item.get("order_id") or "").strip()
            if order_id != expected_order_id:
                continue

            try:
                amount = int(item.get("amount"))
            except (TypeError, ValueError):
                continue

            if amount != expected_amount_subunits:
                continue

            status = str(item.get("status") or "").strip().lower()

            try:
                created_at = int(item.get("created_at") or 0)
            except (TypeError, ValueError):
                created_at = 0

            candidates.append((priority.get(status, 0), created_at, item))

        if not candidates:
            return None

        candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return candidates[0][2]

    async def _mark_booking_paid_and_booked(
        self,
        booking: TripBooking,
        payment: BookingPayment,
        *,
        razorpay_payment_id: str | None,
        razorpay_signature: str | None = None,
    ) -> None:
        await self._ensure_booking_commission_snapshot(booking)

        if razorpay_payment_id:
            payment.razorpay_payment_id = razorpay_payment_id
        if razorpay_signature:
            payment.razorpay_signature = razorpay_signature

        payment.status = BookingPaymentStatus.PAID
        booking.booking_status = BookingStatus.BOOKED
        booking.payment_hold_expires_at = None

        self.db.add(payment)
        self.db.add(booking)
        await self._queue_invoice_email_delivery(booking)
        await self.db.flush()

    async def _mark_booking_paid_but_expired(
        self,
        booking: TripBooking,
        payment: BookingPayment,
        *,
        razorpay_payment_id: str | None,
    ) -> None:
        await self._ensure_booking_commission_snapshot(booking)

        if razorpay_payment_id:
            payment.razorpay_payment_id = razorpay_payment_id

        payment.status = BookingPaymentStatus.PAID
        booking.booking_status = BookingStatus.CANCELLED
        self._set_cancellation_metadata(
            booking,
            reason=(
                "Payment was captured after the booking hold expired; "
                "the booking remains cancelled and requires reconciliation."
            ),
            source="system",
            cancelled_by_user_id=None,
        )
        booking.payment_hold_expires_at = None

        self.db.add(payment)
        self.db.add(booking)
        await self.db.flush()

    async def reconcile_pending_booking_payment(
        self,
        booking: TripBooking,
    ) -> str:
        if booking.booking_status != BookingStatus.PENDING_PAYMENT:
            return "skip_non_pending"

        hold_expired = (
            booking.payment_hold_expires_at is not None
            and booking.payment_hold_expires_at <= utcnow()
        )

        payment_candidates = sorted(
            [
                payment
                for payment in booking.payments
                if payment.status in (BookingPaymentStatus.CREATED, BookingPaymentStatus.PAID)
            ],
            key=lambda item: item.created_at,
            reverse=True,
        )

        if not payment_candidates:
            if hold_expired:
                await self._expire_pending_booking_hold(booking)
                return "expired_without_local_payment"
            return "pending_without_local_payment"

        payment = payment_candidates[0]

        if payment.status == BookingPaymentStatus.PAID:
            if hold_expired:
                await self._mark_booking_paid_but_expired(
                    booking,
                    payment,
                    razorpay_payment_id=payment.razorpay_payment_id,
                )
                return "paid_after_hold_expiry"

            await self._mark_booking_paid_and_booked(
                booking,
                payment,
                razorpay_payment_id=payment.razorpay_payment_id,
                razorpay_signature=payment.razorpay_signature,
            )
            return "promoted_local_paid"

        expected_amount_subunits = self._to_subunits(payment.amount)

        provider_items = await self._fetch_razorpay_order_payments(
            payment.razorpay_order_id
        )
        provider_payment = self._select_best_razorpay_order_payment(
            provider_items,
            expected_order_id=payment.razorpay_order_id,
            expected_amount_subunits=expected_amount_subunits,
        )

        if provider_payment is None:
            if hold_expired:
                await self._expire_pending_booking_hold(booking)
                return "expired_without_provider_payment"
            return "pending_without_provider_payment"

        provider_status = str(provider_payment.get("status") or "").strip().lower()
        provider_payment_id = str(provider_payment.get("id") or "").strip() or None

        if provider_status == "captured":
            if hold_expired:
                await self._mark_booking_paid_but_expired(
                    booking,
                    payment,
                    razorpay_payment_id=provider_payment_id,
                )
                return "captured_after_hold_expiry"

            await self._mark_booking_paid_and_booked(
                booking,
                payment,
                razorpay_payment_id=provider_payment_id,
            )
            return "booked_from_captured_payment"

        if provider_status == "authorized":
            if hold_expired:
                await self._expire_pending_booking_hold(booking)
                return "expired_with_authorized_payment"

            if not provider_payment_id:
                return "pending_authorized_without_payment_id"

            captured_payment = await self._capture_razorpay_payment(
                provider_payment_id,
                expected_amount_subunits,
            )
            captured_status = str(captured_payment.get("status") or "").strip().lower()
            captured_flag = bool(captured_payment.get("captured", False))

            if captured_status == "captured" or captured_flag:
                await self._mark_booking_paid_and_booked(
                    booking,
                    payment,
                    razorpay_payment_id=provider_payment_id,
                )
                return "booked_after_capture"

            return f"pending_after_capture_attempt_{captured_status or 'unknown'}"

        if hold_expired:
            await self._expire_pending_booking_hold(booking)
            return f"expired_with_{provider_status or 'unknown'}_payment"

        return f"pending_with_{provider_status or 'unknown'}_payment"

    async def _capture_razorpay_payment(self, payment_id: str, amount_subunits: int) -> dict[str, Any]:
        payload = {
            "amount": amount_subunits,
            "currency": "INR",
        }
        return await self._razorpay_request(
            method="POST",
            path=f"/payments/{payment_id}/capture",
            json_payload=payload,
        )

    def _verify_razorpay_signature(
        self,
        *,
        order_id: str,
        payment_id: str,
        received_signature: str,
    ) -> None:
        secret = self._get_razorpay_key_secret()
        message = f"{order_id}|{payment_id}".encode()
        generated_signature = hmac.new(
            secret.encode("utf-8"),
            message,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(generated_signature, received_signature):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_payment_signature",
                    "message": "Razorpay payment signature verification failed.",
                },
            )

    def _build_payment_order_response(
        self,
        *,
        booking: TripBooking,
        razorpay_order_id: str,
        currency: str = "INR",
        receipt: str | None = None,
    ) -> dict[str, Any]:
        return {
            "provider": "razorpay",
            "razorpay_key_id": self._get_razorpay_key_id(),
            "razorpay_order_id": razorpay_order_id,
            "amount": booking.fare_amount,
            "amount_subunits": self._to_subunits(booking.fare_amount),
            "currency": currency or "INR",
            "receipt": receipt,
        }

    async def _build_create_booking_response(
        self,
        *,
        booking_id: str,
        passenger_user_id: str,
        message: str,
        payment_order: dict[str, Any] | None,
    ) -> dict[str, Any]:
        booking = await self._get_booking_obj(
            booking_id=booking_id,
            passenger_user_id=passenger_user_id,
        )
        return {
            "message": message,
            "booking": self._serialize_booking(booking),
            "payment_order": payment_order,
        }

    def _get_latest_booking_payment(
        self,
        booking: TripBooking,
    ) -> BookingPayment | None:
        if not booking.payments:
            return None
        return max(booking.payments, key=lambda item: item.created_at)

    async def _create_payment_attempt_for_booking(
        self,
        booking: TripBooking,
    ) -> tuple[BookingPayment, dict[str, Any]]:
        booking.payment_hold_expires_at = self._get_payment_hold_expires_at()
        self.db.add(booking)
        await self.db.flush()

        order_payload = await self._create_razorpay_order(
            booking=booking,
            amount=booking.fare_amount,
        )

        payment = BookingPayment(
            booking_id=booking.id,
            razorpay_order_id=order_payload["id"],
            amount=booking.fare_amount,
            status=BookingPaymentStatus.CREATED,
        )
        self._apply_booking_tax_fields_to_payment(payment, booking)
        self.db.add(payment)
        await self.db.flush()

        return payment, order_payload

    @staticmethod
    def _emails_match(
        left: str | None,
        right: str | None,
    ) -> bool:
        cleaned_left = (left or "").strip()
        cleaned_right = (right or "").strip()

        if not cleaned_left or not cleaned_right:
            return False

        return cleaned_left.casefold() == cleaned_right.casefold()

    def _ensure_explicit_traveller_is_not_account_owner(
        self,
        *,
        owner_user: User,
        traveller_email: str | None,
        seat_number: int | None,
    ) -> None:
        if self._emails_match(traveller_email, owner_user.email):
            detail = {
                "error": "traveller_matches_account_owner",
                "message": "A traveller booked as someone else cannot use the account owner's email. Omit traveller details when booking for yourself.",
            }
            if seat_number is not None:
                detail["seat_number"] = seat_number
            raise HTTPException(
                status_code=400,
                detail=detail,
            )

    async def _ensure_guest_does_not_match_saved_traveller(
        self,
        *,
        owner_user_id: str,
        phone: str,
        seat_number: int,
    ) -> None:
        try:
            normalized_guest_phone = normalize_phone_for_identity(phone)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_traveller_phone",
                    "message": "Traveller phone must contain at least one digit.",
                    "seat_number": seat_number,
                },
            ) from exc

        stmt = select(PassengerTravellerProfile).where(
            PassengerTravellerProfile.owner_user_id == owner_user_id,
        )
        result = await self.db.execute(stmt)

        for profile in result.scalars().all():
            try:
                normalized_profile_phone = normalize_phone_for_identity(
                    profile.phone
                )
            except ValueError:
                continue

            if normalized_profile_phone != normalized_guest_phone:
                continue

            raise HTTPException(
                status_code=409,
                detail={
                    "error": "guest_matches_saved_traveller",
                    "message": "This phone belongs to a saved traveller. Use or reactivate that traveller profile instead of entering guest details.",
                    "seat_number": seat_number,
                    "traveller_profile_id": profile.id,
                    "traveller_profile_is_active": profile.is_active,
                },
            )
    
    async def _resolve_booking_session_traveller_snapshot(
        self,
        *,
        owner_user: User,
        owner_profile: PassengerProfile,
        seat_number: int,
        traveller_profile_id: str | None,
        guest_traveller,
    ) -> dict[str, str | None]:
        if traveller_profile_id is not None:
            profile = await self._get_traveller_profile_for_owner_or_404(
                owner_user_id=owner_user.id,
                profile_id=traveller_profile_id,
            )

            if not profile.is_active:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "traveller_profile_inactive",
                        "message": "Selected traveller profile is inactive.",
                    },
                )

            if not profile.is_self:
                self._ensure_explicit_traveller_is_not_account_owner(
                    owner_user=owner_user,
                    traveller_email=profile.email,
                    seat_number=seat_number,
                )

            return {
                "traveller_profile_id": profile.id,
                "traveller_identity_key": (
                    build_self_traveller_identity(owner_user.id)
                    if profile.is_self
                    else build_profile_traveller_identity(profile.id)
                ),
                "traveller_name_snapshot": profile.full_name,
                "traveller_phone_snapshot": profile.phone,
                "traveller_email_snapshot": None
                if self._emails_match(profile.email, owner_user.email)
                else profile.email,
                "traveller_relationship_label_snapshot": profile.relationship_label,
            }

        if guest_traveller is not None:
            self._ensure_explicit_traveller_is_not_account_owner(
                owner_user=owner_user,
                traveller_email=guest_traveller.email,
                seat_number=seat_number,
            )
            await self._ensure_guest_does_not_match_saved_traveller(
                owner_user_id=owner_user.id,
                phone=guest_traveller.phone,
                seat_number=seat_number,
            )

            return {
                "traveller_profile_id": None,
                "traveller_identity_key": build_guest_traveller_identity(
                    owner_user.id,
                    guest_traveller.phone,
                ),
                "traveller_name_snapshot": guest_traveller.full_name,
                "traveller_phone_snapshot": guest_traveller.phone,
                "traveller_email_snapshot": guest_traveller.email,
                "traveller_relationship_label_snapshot": guest_traveller.relationship_label,
            }

        return {
            "traveller_profile_id": None,
            "traveller_identity_key": build_self_traveller_identity(
                owner_user.id
            ),
            "traveller_name_snapshot": owner_profile.full_name,
            "traveller_phone_snapshot": None,
            "traveller_email_snapshot": None,
            "traveller_relationship_label_snapshot": "Self",
        }

    # ------------------------------------------------------------------
    # bookings
    # ------------------------------------------------------------------

    async def _get_booking_session_obj(
        self,
        *,
        booking_session_id: str,
        owner_user_id: str,
    ) -> BookingSession:
        stmt = (
            select(BookingSession)
            .where(
                BookingSession.id == booking_session_id,
                BookingSession.owner_user_id == owner_user_id,
            )
            .options(
                selectinload(BookingSession.bookings),
                selectinload(BookingSession.payments),
            )
        )
        result = await self.db.execute(stmt)
        booking_session = result.scalar_one_or_none()

        if booking_session is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "booking_session_not_found",
                    "message": "Booking session not found.",
                },
            )

        return booking_session
    
    async def _get_latest_booking_seat_refund_requests_by_booking_id(
        self,
        *,
        booking_session_ids: list[str],
    ) -> dict[str, BookingSeatRefundRequest]:
        cleaned_session_ids = [
            session_id
            for session_id in booking_session_ids
            if session_id
        ]

        if not cleaned_session_ids:
            return {}

        stmt = (
            select(BookingSeatRefundRequest)
            .where(
                BookingSeatRefundRequest.booking_session_id.in_(
                    cleaned_session_ids
                )
            )
            .order_by(
                BookingSeatRefundRequest.booking_id.asc(),
                BookingSeatRefundRequest.created_at.desc(),
            )
        )

        result = await self.db.execute(stmt)
        refund_requests = list(result.scalars().all())

        latest_by_booking_id: dict[str, BookingSeatRefundRequest] = {}

        for refund_request in refund_requests:
            if refund_request.booking_id not in latest_by_booking_id:
                latest_by_booking_id[refund_request.booking_id] = refund_request

        return latest_by_booking_id
    
    async def create_booking_session(
        self,
        current_user: User,
        payload: CreateBookingSessionRequest,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        profile = await self._get_profile_obj(current_user.id)
        if profile is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "profile_required",
                    "message": "Create passenger profile before booking a trip.",
                },
            )

        trip = await self._get_trip_obj_for_booking_update(payload.scheduled_trip_id)

        if trip.status != ScheduledTripStatus.SCHEDULED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "trip_not_bookable",
                    "message": "This scheduled trip is not open for booking.",
                },
            )

        if trip.actual_start_at is not None and trip.actual_start_at <= utcnow():
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "trip_already_started",
                    "message": "This scheduled trip can no longer be booked.",
                },
            )

        expired_pending_booking_count = await self._expire_stale_pending_bookings_for_trip(
            trip.id
        )

        fare, pickup_route_stop, dropoff_route_stop = await self._resolve_fare(
            route_id=trip.route_id,
            pickup_stop_id=payload.pickup_stop_id,
            dropoff_stop_id=payload.dropoff_stop_id,
        )

        seat_count = await self._get_app_bookable_capacity_for_trip(trip)

        requested_seat_numbers = [seat.seat_number for seat in payload.seats]
        requested_seat_number_set = set(requested_seat_numbers)

        if len(requested_seat_numbers) != len(requested_seat_number_set):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "duplicate_seat_numbers",
                    "message": "Seat numbers must be unique within one booking session.",
                },
            )

        for seat_number in requested_seat_numbers:
            if seat_number < 1 or seat_number > seat_count:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "invalid_seat_number",
                        "message": "Selected seat is outside the app-bookable seat range for this trip.",
                        "seat_number": seat_number,
                        "seat_capacity": seat_count,
                    },
                )

        occupied_seat_numbers = await self._get_occupied_app_seat_numbers_for_leg(
            scheduled_trip_id=trip.id,
            pickup_sequence_no=pickup_route_stop.sequence_no,
            dropoff_sequence_no=dropoff_route_stop.sequence_no,
        )

        unavailable_seat_numbers = sorted(
            requested_seat_number_set.intersection(occupied_seat_numbers)
        )

        if unavailable_seat_numbers:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "seat_unavailable",
                    "message": "One or more selected seats are already occupied for the selected route segment.",
                    "seat_numbers": unavailable_seat_numbers,
                },
            )

        overlapping_active_booking_count = await self._count_overlapping_active_trip_bookings(
            scheduled_trip_id=trip.id,
            pickup_sequence_no=pickup_route_stop.sequence_no,
            dropoff_sequence_no=dropoff_route_stop.sequence_no,
        )

        available_seat_count = max(seat_count - overlapping_active_booking_count, 0)

        if len(requested_seat_numbers) > available_seat_count:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "trip_segment_full",
                    "message": "Not enough seats are available for the selected trip segment.",
                    "requested_seat_count": len(requested_seat_numbers),
                    "available_seat_count": available_seat_count,
                },
            )

        per_seat_gst_breakdown = await self._build_gst_breakdown(
            self._quantize_money(fare.amount),
            is_ac=None if trip.route is None else trip.route.has_ac,
        )
        session_gst_breakdown = per_seat_gst_breakdown.multiplied(
            len(requested_seat_numbers)
        )
        normalized_fare_amount = per_seat_gst_breakdown.gross_amount
        total_fare_amount = session_gst_breakdown.gross_amount

        current_commission_percent = await self._get_current_commission_percent()
        (
            commission_percent_snapshot,
            commission_amount,
            driver_payout_amount,
        ) = self._build_booking_commission_snapshot(
            fare_amount=per_seat_gst_breakdown.taxable_amount,
            commission_percent=current_commission_percent,
        )

        payment_hold_expires_at = self._get_payment_hold_expires_at()

        traveller_snapshots: dict[int, dict[str, str | None]] = {}

        for seat in payload.seats:
            traveller_snapshots[seat.seat_number] = (
                await self._resolve_booking_session_traveller_snapshot(
                    owner_user=current_user,
                    owner_profile=profile,
                    seat_number=seat.seat_number,
                    traveller_profile_id=seat.traveller_profile_id,
                    guest_traveller=seat.traveller,
                )
            )

        await self._ensure_traveller_bookings_do_not_conflict(
            trip=trip,
            pickup_stop_id=payload.pickup_stop_id,
            dropoff_stop_id=payload.dropoff_stop_id,
            pickup_sequence_no=pickup_route_stop.sequence_no,
            dropoff_sequence_no=dropoff_route_stop.sequence_no,
            traveller_requests=[
                (
                    str(
                        traveller_snapshots[seat.seat_number][
                            "traveller_identity_key"
                        ]
                    ),
                    seat.seat_number,
                )
                for seat in payload.seats
            ],
        )

        try:
            booking_session = BookingSession(
                owner_user_id=current_user.id,
                scheduled_trip_id=trip.id,
                route_id=trip.route_id,
                pickup_stop_id=payload.pickup_stop_id,
                dropoff_stop_id=payload.dropoff_stop_id,
                pickup_sequence_no_snapshot=pickup_route_stop.sequence_no,
                dropoff_sequence_no_snapshot=dropoff_route_stop.sequence_no,
                status=BookingSessionStatus.PENDING_PAYMENT,
                total_fare_amount=total_fare_amount,
                total_taxable_amount=session_gst_breakdown.taxable_amount,
                total_cgst_amount=session_gst_breakdown.cgst_amount,
                total_sgst_amount=session_gst_breakdown.sgst_amount,
                total_igst_amount=session_gst_breakdown.igst_amount,
                total_tax_amount=session_gst_breakdown.total_tax_amount,
                gst_enabled_snapshot=(
                    session_gst_breakdown.gst_enabled
                    and session_gst_breakdown.gst_applicable
                ),
                gst_inclusive_snapshot=session_gst_breakdown.gst_inclusive,
                cgst_rate_percent_snapshot=session_gst_breakdown.cgst_rate_percent,
                sgst_rate_percent_snapshot=session_gst_breakdown.sgst_rate_percent,
                igst_rate_percent_snapshot=session_gst_breakdown.igst_rate_percent,
                payment_hold_expires_at=payment_hold_expires_at,
            )
            self.db.add(booking_session)
            await self.db.flush()

            for seat in payload.seats:
                snapshot = traveller_snapshots[seat.seat_number]

                booking = TripBooking(
                    booking_session_id=booking_session.id,
                    passenger_user_id=current_user.id,
                    booked_by_user_id=current_user.id,
                    traveller_profile_id=snapshot["traveller_profile_id"],
                    traveller_identity_key=snapshot[
                        "traveller_identity_key"
                    ],
                    traveller_name_snapshot=snapshot["traveller_name_snapshot"],
                    traveller_phone_snapshot=snapshot["traveller_phone_snapshot"],
                    traveller_email_snapshot=snapshot["traveller_email_snapshot"],
                    traveller_relationship_label_snapshot=snapshot[
                        "traveller_relationship_label_snapshot"
                    ],
                    otp=self._generate_booking_otp(),
                    scheduled_trip_id=trip.id,
                    route_id=trip.route_id,
                    pickup_stop_id=payload.pickup_stop_id,
                    dropoff_stop_id=payload.dropoff_stop_id,
                    seat_number=seat.seat_number,
                    booking_status=BookingStatus.PENDING_PAYMENT,
                    fare_amount=normalized_fare_amount,
                    taxable_amount=per_seat_gst_breakdown.taxable_amount,
                    cgst_rate_percent_snapshot=(
                        per_seat_gst_breakdown.cgst_rate_percent
                    ),
                    cgst_amount=per_seat_gst_breakdown.cgst_amount,
                    sgst_rate_percent_snapshot=(
                        per_seat_gst_breakdown.sgst_rate_percent
                    ),
                    sgst_amount=per_seat_gst_breakdown.sgst_amount,
                    igst_rate_percent_snapshot=(
                        per_seat_gst_breakdown.igst_rate_percent
                    ),
                    igst_amount=per_seat_gst_breakdown.igst_amount,
                    total_tax_amount=per_seat_gst_breakdown.total_tax_amount,
                    gst_enabled_snapshot=(
                        per_seat_gst_breakdown.gst_enabled
                        and per_seat_gst_breakdown.gst_applicable
                    ),
                    gst_inclusive_snapshot=per_seat_gst_breakdown.gst_inclusive,
                    pickup_sequence_no_snapshot=pickup_route_stop.sequence_no,
                    dropoff_sequence_no_snapshot=dropoff_route_stop.sequence_no,
                    payment_hold_expires_at=payment_hold_expires_at,
                    commission_percent_snapshot=commission_percent_snapshot,
                    commission_amount=commission_amount,
                    driver_payout_amount=driver_payout_amount,
                )
                self.db.add(booking)

            await self.db.flush()

            order_payload = await self._create_booking_session_razorpay_order(
                booking_session=booking_session,
                amount=booking_session.total_fare_amount,
            )

            payment = BookingSessionPayment(
                booking_session_id=booking_session.id,
                razorpay_order_id=order_payload["id"],
                amount=booking_session.total_fare_amount,
                taxable_amount=booking_session.total_taxable_amount,
                cgst_amount=booking_session.total_cgst_amount,
                sgst_amount=booking_session.total_sgst_amount,
                igst_amount=booking_session.total_igst_amount,
                total_tax_amount=booking_session.total_tax_amount,
                status=BookingPaymentStatus.CREATED,
            )
            self.db.add(payment)

            await self.db.commit()

            await self._broadcast_seatmap_snapshots_for_trip(
                scheduled_trip_id=trip.id,
                reason="booking_session_created",
            )

            if expired_pending_booking_count > 0:
                await self._broadcast_seatmap_snapshots_for_trip(
                    scheduled_trip_id=trip.id,
                    reason="payment_hold_expired",
                )

        except HTTPException:
            await self.db.rollback()
            raise
        except IntegrityError:
            await self.db.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "booking_session_conflict",
                    "message": "Could not create booking session because one or more seats are no longer available.",
                },
            )
        except Exception:
            await self.db.rollback()
            raise

        booking_session = await self._get_booking_session_obj(
            booking_session_id=booking_session.id,
            owner_user_id=current_user.id,
        )

        return {
            "message": "Booking session created. Payment is pending.",
            "booking_session": await self._serialize_booking_session_with_refunds(
                booking_session
            ),
            "payment_order": self._build_booking_session_payment_order_response(
                booking_session=booking_session,
                razorpay_order_id=order_payload["id"],
                currency=order_payload.get("currency", "INR"),
                receipt=order_payload.get("receipt"),
            ),
        }

    async def retry_booking_session_payment(
        self,
        current_user: User,
        booking_session_id: str,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        booking_session = await self._get_booking_session_for_update_or_404(
            booking_session_id=booking_session_id,
            owner_user_id=current_user.id,
        )
        payments = await self._list_booking_session_payments_for_update(
            booking_session.id
        )
        bookings = await self._list_booking_session_bookings_for_update(
            booking_session.id
        )

        if not bookings:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "booking_session_empty",
                    "message": "Booking session has no seat bookings.",
                },
            )

        if booking_session.status == BookingSessionStatus.CONFIRMED:
            await self.db.rollback()
            booking_session = await self._get_booking_session_obj(
                booking_session_id=booking_session.id,
                owner_user_id=current_user.id,
            )
            return {
                "message": "Booking session payment is already confirmed.",
                "booking_session": await self._serialize_booking_session_with_refunds(
                    booking_session
                ),
                "payment_order": None,
            }

        if booking_session.status != BookingSessionStatus.PENDING_PAYMENT:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "booking_session_not_retryable",
                    "message": "Payment can only be retried for a pending booking session.",
                    "status": booking_session.status.value,
                },
            )

        outcome = await self.reconcile_pending_booking_session_payment(
            booking_session,
            bookings=bookings,
            payments=payments,
        )

        confirmed_outcomes = {
            "promoted_local_paid",
            "confirmed_from_captured_payment",
            "confirmed_after_capture",
        }
        if outcome in confirmed_outcomes:
            await self.db.commit()
            booking_session = await self._get_booking_session_obj(
                booking_session_id=booking_session.id,
                owner_user_id=current_user.id,
            )
            await self._queue_booking_session_traveller_notifications(
                booking_session=booking_session,
                bookings=list(booking_session.bookings),
                event_type="traveller_seat_confirmed",
            )
            await self.db.commit()
            await self._broadcast_seatmap_snapshots_for_trip(
                scheduled_trip_id=booking_session.scheduled_trip_id,
                reason="booking_session_confirmed_during_payment_retry",
            )
            return {
                "message": "Payment was already successful. Booking session confirmed.",
                "booking_session": await self._serialize_booking_session_with_refunds(
                    booking_session
                ),
                "payment_order": None,
            }

        if outcome.startswith("expired_") or outcome in {
            "paid_after_hold_expiry",
            "captured_after_hold_expiry",
        }:
            await self.db.commit()
            await self._broadcast_seatmap_snapshots_for_trip(
                scheduled_trip_id=booking_session.scheduled_trip_id,
                reason="booking_session_payment_hold_expired",
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "payment_hold_expired",
                    "message": (
                        "Payment hold expired. The seats were released and "
                        "payment can no longer be retried."
                    ),
                    "reconciliation_outcome": outcome,
                },
            )

        if "authorized" in outcome or outcome.startswith(
            "pending_after_capture_attempt_"
        ):
            await self.db.commit()
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "payment_processing",
                    "message": (
                        "A payment is already being processed for this "
                        "booking session. Please wait for confirmation."
                    ),
                    "reconciliation_outcome": outcome,
                },
            )

        if not payments:
            await self.db.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "booking_session_payment_not_found",
                    "message": "No Razorpay order exists for this booking session.",
                },
            )

        payment = max(payments, key=lambda item: item.created_at)
        payment.status = BookingPaymentStatus.CREATED
        payment.razorpay_payment_id = None
        payment.razorpay_signature = None
        self.db.add(payment)
        await self.db.commit()

        booking_session = await self._get_booking_session_obj(
            booking_session_id=booking_session.id,
            owner_user_id=current_user.id,
        )
        return {
            "message": "Payment can be retried using the existing Razorpay order.",
            "booking_session": await self._serialize_booking_session_with_refunds(
                booking_session
            ),
            "payment_order": self._build_booking_session_payment_order_response(
                booking_session=booking_session,
                razorpay_order_id=payment.razorpay_order_id,
            ),
        }
    
    async def verify_booking_session_payment(
        self,
        current_user: User,
        booking_session_id: str,
        payload: VerifyBookingSessionPaymentRequest,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        booking_session = await self._get_booking_session_for_update_or_404(
            booking_session_id=booking_session_id,
            owner_user_id=current_user.id,
        )

        payments = await self._list_booking_session_payments_for_update(
            booking_session.id
        )
        bookings = await self._list_booking_session_bookings_for_update(
            booking_session.id
        )

        if not bookings:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "booking_session_empty",
                    "message": "Booking session has no seat bookings.",
                },
            )

        payment = self._get_booking_session_payment_by_order_id(
            payments,
            razorpay_order_id=payload.razorpay_order_id,
        )

        if payment is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "booking_session_payment_not_found",
                    "message": "Payment order was not found for this booking session.",
                },
            )

        already_confirmed = (
            booking_session.status == BookingSessionStatus.CONFIRMED
            and payment.status == BookingPaymentStatus.PAID
            and all(
                booking.booking_status
                in (
                    BookingStatus.BOOKED,
                    BookingStatus.BOARDED,
                    BookingStatus.COMPLETED,
                )
                for booking in bookings
            )
        )

        if already_confirmed:
            booking_session = await self._get_booking_session_obj(
                booking_session_id=booking_session.id,
                owner_user_id=current_user.id,
            )
            await self._queue_booking_session_traveller_notifications(
                booking_session=booking_session,
                bookings=list(booking_session.bookings),
                event_type="traveller_seat_confirmed",
            )
            for confirmed_booking in booking_session.bookings:
                await self._queue_invoice_email_delivery(confirmed_booking)
            await self.db.commit()
            return {
                "message": "Payment already verified successfully.",
                "booking_session": await self._serialize_booking_session_with_refunds(
                    booking_session
                ),
            }

        if booking_session.status != BookingSessionStatus.PENDING_PAYMENT:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "booking_session_not_pending_payment",
                    "message": "This booking session is not awaiting payment verification.",
                },
            )

        hold_expired = (
            booking_session.payment_hold_expires_at is not None
            and booking_session.payment_hold_expires_at <= utcnow()
        )

        if hold_expired:
            await self._expire_pending_booking_session(
                booking_session=booking_session,
                bookings=bookings,
                payments=payments,
            )
            await self.db.commit()

            await self._broadcast_seatmap_snapshots_for_trip(
                scheduled_trip_id=booking_session.scheduled_trip_id,
                reason="booking_session_payment_hold_expired",
            )

            await self._notify_user(
                user_id=current_user.id,
                title="Booking session expired",
                message="Your payment window expired, so the selected seats were released.",
                data={
                    "type": "booking_session_expired",
                    "booking_session_id": booking_session.id,
                    "scheduled_trip_id": booking_session.scheduled_trip_id,
                    "refresh": [
                        "bookings_list",
                        "booking_session_detail",
                        "seatmap",
                    ],
                },
            )

            raise HTTPException(
                status_code=409,
                detail={
                    "error": "payment_hold_expired",
                    "message": "Payment hold expired. Booking session was expired and seats were released.",
                },
            )

        if payment.status == BookingPaymentStatus.PAID:
            await self._mark_booking_session_paid_and_confirmed(
                booking_session=booking_session,
                payment=payment,
                bookings=bookings,
                razorpay_payment_id=payload.razorpay_payment_id,
                razorpay_signature=payload.razorpay_signature,
            )
            await self._queue_booking_session_traveller_notifications(
                booking_session=booking_session,
                bookings=bookings,
                event_type="traveller_seat_confirmed",
            )
            await self.db.commit()

            booking_session = await self._get_booking_session_obj(
                booking_session_id=booking_session.id,
                owner_user_id=current_user.id,
            )

            await self._broadcast_seatmap_snapshots_for_trip(
                scheduled_trip_id=booking_session.scheduled_trip_id,
                reason="booking_session_confirmed",
            )

            await self._notify_user(
                user_id=current_user.id,
                title="Payment verified",
                message="Your booking session is confirmed.",
                data={
                    "type": "booking_session_confirmed",
                    "booking_session_id": booking_session.id,
                    "scheduled_trip_id": booking_session.scheduled_trip_id,
                    "refresh": [
                        "bookings_list",
                        "booking_session_detail",
                        "current_booking",
                        "seatmap",
                    ],
                },
            )

            return {
                "message": "Payment verified successfully.",
                "booking_session": await self._serialize_booking_session_with_refunds(
                    booking_session
                ),
            }

        self._verify_razorpay_signature(
            order_id=payload.razorpay_order_id,
            payment_id=payload.razorpay_payment_id,
            received_signature=payload.razorpay_signature,
        )

        fetched_payment = await self._fetch_razorpay_payment(
            payload.razorpay_payment_id
        )

        fetched_order_id = fetched_payment.get("order_id")
        fetched_status = str(fetched_payment.get("status", "")).lower()
        fetched_captured = bool(fetched_payment.get("captured", False))
        fetched_amount = int(fetched_payment.get("amount", 0))

        expected_amount_subunits = self._to_subunits(payment.amount)

        if fetched_order_id != payment.razorpay_order_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "payment_order_mismatch",
                    "message": "Fetched payment order does not match stored booking session payment order.",
                },
            )

        if fetched_amount != expected_amount_subunits:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "payment_amount_mismatch",
                    "message": "Fetched payment amount does not match booking session amount.",
                },
            )

        if fetched_status == "authorized" and not fetched_captured:
            captured_payment = await self._capture_razorpay_payment(
                payload.razorpay_payment_id,
                expected_amount_subunits,
            )
            fetched_status = str(captured_payment.get("status", "")).lower()
            fetched_captured = bool(captured_payment.get("captured", False))

        if fetched_status in {"failed", "refunded"}:
            payment.razorpay_payment_id = payload.razorpay_payment_id
            payment.razorpay_signature = payload.razorpay_signature
            payment.status = BookingPaymentStatus.FAILED
            self.db.add(payment)
            await self.db.commit()

            raise HTTPException(
                status_code=409,
                detail={
                    "error": "payment_not_successful",
                    "message": "Payment was not successful.",
                    "provider_status": fetched_status,
                },
            )

        if fetched_status != "captured" and not fetched_captured:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "payment_not_captured",
                    "message": "Payment has not been captured yet.",
                    "provider_status": fetched_status,
                },
            )

        await self._mark_booking_session_paid_and_confirmed(
            booking_session=booking_session,
            payment=payment,
            bookings=bookings,
            razorpay_payment_id=payload.razorpay_payment_id,
            razorpay_signature=payload.razorpay_signature,
        )
        await self._queue_booking_session_traveller_notifications(
            booking_session=booking_session,
            bookings=bookings,
            event_type="traveller_seat_confirmed",
        )

        await self.db.commit()

        booking_session = await self._get_booking_session_obj(
            booking_session_id=booking_session.id,
            owner_user_id=current_user.id,
        )

        await self._broadcast_seatmap_snapshots_for_trip(
            scheduled_trip_id=booking_session.scheduled_trip_id,
            reason="booking_session_confirmed",
        )

        await self._notify_user(
            user_id=current_user.id,
            title="Payment verified",
            message="Your booking session is confirmed.",
            data={
                "type": "booking_session_confirmed",
                "booking_session_id": booking_session.id,
                "scheduled_trip_id": booking_session.scheduled_trip_id,
                "refresh": [
                    "bookings_list",
                    "booking_session_detail",
                    "current_booking",
                    "seatmap",
                ],
            },
        )

        return {
            "message": "Payment verified successfully.",
            "booking_session": await self._serialize_booking_session_with_refunds(
                booking_session
            ),
        }

    async def cancel_booking_session(
        self,
        current_user: User,
        booking_session_id: str,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        booking_session = await self._get_booking_session_for_update_or_404(
            booking_session_id=booking_session_id,
            owner_user_id=current_user.id,
        )

        payments = await self._list_booking_session_payments_for_update(
            booking_session.id
        )
        bookings = await self._list_booking_session_bookings_for_update(
            booking_session.id
        )

        if not bookings:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "booking_session_empty",
                    "message": "Booking session has no seat bookings.",
                },
            )

        if booking_session.status == BookingSessionStatus.CANCELLED:
            booking_session = await self._get_booking_session_obj(
                booking_session_id=booking_session.id,
                owner_user_id=current_user.id,
            )
            return {
                "message": "Booking session is already cancelled.",
                "booking_session": await self._serialize_booking_session_with_refunds(
                    booking_session
                ),
            }

        if booking_session.status == BookingSessionStatus.EXPIRED:
            booking_session = await self._get_booking_session_obj(
                booking_session_id=booking_session.id,
                owner_user_id=current_user.id,
            )
            return {
                "message": "Booking session is already expired.",
                "booking_session": await self._serialize_booking_session_with_refunds(
                    booking_session
                ),
            }

        if booking_session.status == BookingSessionStatus.CONFIRMED:
            await self._ensure_confirmed_booking_session_cancellable(
                booking_session=booking_session,
                bookings=bookings,
            )

            paid_payment = self._get_paid_booking_session_payment(payments)

            if paid_payment is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "paid_booking_session_payment_not_found",
                        "message": "Confirmed booking session does not have a paid session payment.",
                    },
                )

            await self._cancel_confirmed_booking_session_and_request_refund(
                booking_session=booking_session,
                bookings=bookings,
                payment=paid_payment,
                cancelled_by_user_id=current_user.id,
            )

            await self.db.commit()

            booking_session = await self._get_booking_session_obj(
                booking_session_id=booking_session.id,
                owner_user_id=current_user.id,
            )
            await self._queue_booking_session_traveller_notifications(
                booking_session=booking_session,
                bookings=list(booking_session.bookings),
                event_type="traveller_seat_cancelled",
            )
            await self.db.commit()

            await self._broadcast_seatmap_snapshots_for_trip(
                scheduled_trip_id=booking_session.scheduled_trip_id,
                reason="confirmed_booking_session_cancelled",
            )

            await self._notify_user(
                user_id=current_user.id,
                title="Booking session cancelled",
                message="Your booking session was cancelled. Refund has been requested.",
                data={
                    "type": "confirmed_booking_session_cancelled",
                    "booking_session_id": booking_session.id,
                    "scheduled_trip_id": booking_session.scheduled_trip_id,
                    "refresh": [
                        "bookings_list",
                        "booking_session_detail",
                        "seatmap",
                    ],
                },
            )

            return {
                "message": "Booking session cancelled successfully. Refund has been requested.",
                "booking_session": await self._serialize_booking_session_with_refunds(
                    booking_session
                ),
            }

        if booking_session.status != BookingSessionStatus.PENDING_PAYMENT:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "booking_session_not_cancellable",
                    "message": "This booking session cannot be cancelled.",
                    "status": booking_session.status.value,
                },
            )

        await self._cancel_pending_booking_session(
            booking_session=booking_session,
            bookings=bookings,
            payments=payments,
            cancelled_by_user_id=current_user.id,
        )

        await self.db.commit()

        booking_session = await self._get_booking_session_obj(
            booking_session_id=booking_session.id,
            owner_user_id=current_user.id,
        )

        await self._broadcast_seatmap_snapshots_for_trip(
            scheduled_trip_id=booking_session.scheduled_trip_id,
            reason="booking_session_cancelled",
        )

        await self._notify_user(
            user_id=current_user.id,
            title="Booking session cancelled",
            message="Your selected seats were released.",
            data={
                "type": "booking_session_cancelled",
                "booking_session_id": booking_session.id,
                "scheduled_trip_id": booking_session.scheduled_trip_id,
                "refresh": [
                    "bookings_list",
                    "booking_session_detail",
                    "seatmap",
                ],
            },
        )

        return {
            "message": "Booking session cancelled successfully.",
            "booking_session": await self._serialize_booking_session_with_refunds(
                booking_session
            ),
        }
    
    async def cancel_booking_session_seat(
        self,
        current_user: User,
        booking_session_id: str,
        booking_id: str,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        booking_session = await self._get_booking_session_for_update_or_404(
            booking_session_id=booking_session_id,
            owner_user_id=current_user.id,
        )

        payments = await self._list_booking_session_payments_for_update(
            booking_session.id
        )
        bookings = await self._list_booking_session_bookings_for_update(
            booking_session.id
        )

        if booking_session.status == BookingSessionStatus.PENDING_PAYMENT:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "pending_session_seat_cancellation_not_supported",
                    "message": "Cancel the pending booking session instead of cancelling an individual pending seat.",
                },
            )

        if booking_session.status in (
            BookingSessionStatus.CANCELLED,
            BookingSessionStatus.EXPIRED,
        ):
            booking_session = await self._get_booking_session_obj(
                booking_session_id=booking_session.id,
                owner_user_id=current_user.id,
            )
            return {
                "message": "Booking session is already closed.",
                "booking_session": await self._serialize_booking_session_with_refunds(
                    booking_session
                ),
            }

        if booking_session.status != BookingSessionStatus.CONFIRMED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "booking_session_not_confirmed",
                    "message": "Only confirmed booking sessions support per-seat cancellation.",
                    "status": booking_session.status.value
                    if hasattr(booking_session.status, "value")
                    else str(booking_session.status),
                },
            )

        booking = self._get_booking_from_session_bookings_or_404(
            bookings=bookings,
            booking_id=booking_id,
        )

        paid_payment = self._get_paid_booking_session_payment(payments)

        if paid_payment is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "paid_booking_session_payment_not_found",
                    "message": "Confirmed booking session does not have a paid session payment.",
                },
            )

        await self._ensure_confirmed_booking_session_seat_cancellable(
            booking_session=booking_session,
            booking=booking,
        )

        if booking.booking_status == BookingStatus.CANCELLED:
            booking_session = await self._get_booking_session_obj(
                booking_session_id=booking_session.id,
                owner_user_id=current_user.id,
            )
            return {
                "message": "Seat booking is already cancelled.",
                "booking_session": await self._serialize_booking_session_with_refunds(
                    booking_session
                ),
            }

        now = utcnow()

        booking.booking_status = BookingStatus.CANCELLED
        self._set_cancellation_metadata(
            booking,
            reason="Seat cancelled by passenger.",
            source="passenger",
            cancelled_by_user_id=current_user.id,
            cancelled_at=now,
        )
        booking.payment_hold_expires_at = None
        booking.refund_retry_after = booking.refund_retry_after or now
        booking.refund_attempt_count = booking.refund_attempt_count or 0
        self.db.add(booking)

        await self._ensure_booking_seat_refund_request(
            booking_session=booking_session,
            booking=booking,
            payment=paid_payment,
        )

        await self._sync_booking_session_status_after_seat_cancellation(
            booking_session=booking_session,
            bookings=bookings,
        )

        await self.db.commit()

        booking_session = await self._get_booking_session_obj(
            booking_session_id=booking_session.id,
            owner_user_id=current_user.id,
        )

        cancelled_booking = self._get_booking_from_session_bookings_or_404(
            bookings=list(booking_session.bookings),
            booking_id=booking_id,
        )

        await self._queue_booking_session_traveller_notifications(
            booking_session=booking_session,
            bookings=[cancelled_booking],
            event_type="traveller_seat_cancelled",
        )
        await self.db.commit()

        await self._broadcast_seatmap_snapshots_for_trip(
            scheduled_trip_id=booking_session.scheduled_trip_id,
            reason="booking_session_seat_cancelled",
        )

        await self._notify_user(
            user_id=current_user.id,
            title="Seat cancelled",
            message="Selected seat was cancelled. Refund has been requested.",
            data={
                "type": "booking_session_seat_cancelled",
                "booking_session_id": booking_session.id,
                "booking_id": booking_id,
                "scheduled_trip_id": booking_session.scheduled_trip_id,
                "refresh": [
                    "bookings_list",
                    "booking_session_detail",
                    "seatmap",
                ],
            },
        )

        booking_session = await self._get_booking_session_obj(
            booking_session_id=booking_session.id,
            owner_user_id=current_user.id,
        )

        return {
            "message": "Seat cancelled successfully. Refund has been requested.",
            "booking_session": await self._serialize_booking_session_with_refunds(
                booking_session
            ),
        }

    async def list_booking_sessions(
        self,
        current_user: User,
        *,
        status: BookingSessionStatus | None = None,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        filters = [
            BookingSession.owner_user_id == current_user.id,
        ]

        if status is not None:
            filters.append(BookingSession.status == status)

        stmt = (
            select(BookingSession)
            .where(*filters)
            .options(
                selectinload(BookingSession.bookings),
                selectinload(BookingSession.payments),
            )
            .order_by(BookingSession.created_at.desc())
        )

        result = await self.db.execute(stmt)
        booking_sessions = list(result.scalars().unique().all())

        return {
            "items": await self._serialize_booking_sessions_with_refunds(
                booking_sessions
            ),
            "count": len(booking_sessions),
        }

    async def get_booking_session_detail(
        self,
        current_user: User,
        booking_session_id: str,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        booking_session = await self._get_booking_session_obj(
            booking_session_id=booking_session_id,
            owner_user_id=current_user.id,
        )

        return await self._serialize_booking_session_with_refunds(
            booking_session
        )

    async def create_booking(
        self,
        current_user: User,
        payload: CreateBookingRequest,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        profile = await self._get_profile_obj(current_user.id)
        if profile is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "profile_required",
                    "message": "Create passenger profile before booking a trip.",
                },
            )

        trip = await self._get_trip_obj_for_booking_update(payload.scheduled_trip_id)

        if trip.status != ScheduledTripStatus.SCHEDULED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "trip_not_bookable",
                    "message": "This scheduled trip is not open for booking.",
                },
            )

        if trip.actual_start_at is not None and trip.actual_start_at <= utcnow():
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "trip_already_started",
                    "message": "This scheduled trip can no longer be booked.",
                },
            )

        expired_pending_booking_count = await self._expire_stale_pending_bookings_for_trip(
            trip.id
        )
        fare, pickup_route_stop, dropoff_route_stop = await self._resolve_fare(
            route_id=trip.route_id,
            pickup_stop_id=payload.pickup_stop_id,
            dropoff_stop_id=payload.dropoff_stop_id,
        )

        self_identity_key = build_self_traveller_identity(current_user.id)
        await self._lock_traveller_identity_keys([self_identity_key])

        existing_stmt = (
            select(TripBooking)
            .where(
                TripBooking.traveller_identity_key == self_identity_key,
                TripBooking.scheduled_trip_id == trip.id,
                TripBooking.booking_session_id.is_(None),
                TripBooking.booking_status.in_(
                    (
                        BookingStatus.PENDING_PAYMENT,
                        BookingStatus.BOOKED,
                        BookingStatus.BOARDED,
                    )
                ),
            )
            .options(selectinload(TripBooking.payments))
            .with_for_update()
        )
        existing_result = await self.db.execute(existing_stmt)
        existing_bookings = list(existing_result.scalars().unique().all())
        existing_booking = next(
            (
                booking
                for booking in existing_bookings
                if booking.pickup_stop_id == payload.pickup_stop_id
                and booking.dropoff_stop_id == payload.dropoff_stop_id
            ),
            None,
        )

        if existing_booking is not None:
            if existing_booking.seat_number != payload.seat_number:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "existing_booking_seat_mismatch",
                        "message": "Passenger already has a booking for this segment with a different seat.",
                        "existing_seat_number": existing_booking.seat_number,
                        "requested_seat_number": payload.seat_number,
                    },
                )

            if existing_booking.booking_status in (
                BookingStatus.BOOKED,
                BookingStatus.BOARDED,
            ):
                await self.db.commit()

                if expired_pending_booking_count > 0:
                    await self._broadcast_seatmap_snapshots_for_trip(
                        scheduled_trip_id=trip.id,
                        reason="payment_hold_expired",
                    )

                return await self._build_create_booking_response(
                    booking_id=existing_booking.id,
                    passenger_user_id=current_user.id,
                    message="Booking already exists.",
                    payment_order=None,
                )

            latest_payment = self._get_latest_booking_payment(existing_booking)

            if latest_payment is not None and latest_payment.status == BookingPaymentStatus.PAID:
                await self._mark_booking_paid_and_booked(
                    existing_booking,
                    latest_payment,
                    razorpay_payment_id=latest_payment.razorpay_payment_id,
                    razorpay_signature=latest_payment.razorpay_signature,
                )
                await self.db.commit()

                if expired_pending_booking_count > 0:
                    await self._broadcast_seatmap_snapshots_for_trip(
                        scheduled_trip_id=trip.id,
                        reason="payment_hold_expired",
                    )

                return await self._build_create_booking_response(
                    booking_id=existing_booking.id,
                    passenger_user_id=current_user.id,
                    message="Booking already exists and payment is already verified.",
                    payment_order=None,
                )

            if latest_payment is not None and latest_payment.status == BookingPaymentStatus.CREATED:
                expected_amount_subunits = self._to_subunits(latest_payment.amount)

                provider_items = await self._fetch_razorpay_order_payments(
                    latest_payment.razorpay_order_id
                )
                provider_payment = self._select_best_razorpay_order_payment(
                    provider_items,
                    expected_order_id=latest_payment.razorpay_order_id,
                    expected_amount_subunits=expected_amount_subunits,
                )

                if provider_payment is not None:
                    provider_status = str(provider_payment.get("status") or "").strip().lower()
                    provider_payment_id = str(provider_payment.get("id") or "").strip() or None

                    if provider_status == "captured":
                        await self._mark_booking_paid_and_booked(
                            existing_booking,
                            latest_payment,
                            razorpay_payment_id=provider_payment_id,
                        )
                        await self.db.commit()

                        if expired_pending_booking_count > 0:
                            await self._broadcast_seatmap_snapshots_for_trip(
                                scheduled_trip_id=trip.id,
                                reason="payment_hold_expired",
                            )

                        return await self._build_create_booking_response(
                            booking_id=existing_booking.id,
                            passenger_user_id=current_user.id,
                            message="Booking already exists and payment is already verified.",
                            payment_order=None,
                        )

                    if provider_status not in {"failed", "refunded"}:
                        await self.db.commit()

                        if expired_pending_booking_count > 0:
                            await self._broadcast_seatmap_snapshots_for_trip(
                                scheduled_trip_id=trip.id,
                                reason="payment_hold_expired",
                            )

                        return await self._build_create_booking_response(
                            booking_id=existing_booking.id,
                            passenger_user_id=current_user.id,
                            message="Booking already exists. Payment is pending.",
                            payment_order=self._build_payment_order_response(
                                booking=existing_booking,
                                razorpay_order_id=latest_payment.razorpay_order_id,
                            ),
                        )

                    latest_payment.status = BookingPaymentStatus.FAILED
                    if provider_payment_id:
                        latest_payment.razorpay_payment_id = provider_payment_id
                    self.db.add(latest_payment)
                    await self.db.flush()

            try:
                _, order_payload = await self._create_payment_attempt_for_booking(existing_booking)
                await self.db.commit()

                if expired_pending_booking_count > 0:
                    await self._broadcast_seatmap_snapshots_for_trip(
                        scheduled_trip_id=trip.id,
                        reason="payment_hold_expired",
                    )

            except HTTPException:
                await self.db.rollback()
                raise
            except Exception:
                await self.db.rollback()
                raise

            return await self._build_create_booking_response(
                booking_id=existing_booking.id,
                passenger_user_id=current_user.id,
                message="Booking already exists. A new payment attempt has been created.",
                payment_order=self._build_payment_order_response(
                    booking=existing_booking,
                    razorpay_order_id=order_payload["id"],
                    currency=order_payload.get("currency", "INR"),
                    receipt=order_payload.get("receipt"),
                ),
            )

        await self._ensure_traveller_bookings_do_not_conflict(
            trip=trip,
            pickup_stop_id=payload.pickup_stop_id,
            dropoff_stop_id=payload.dropoff_stop_id,
            pickup_sequence_no=pickup_route_stop.sequence_no,
            dropoff_sequence_no=dropoff_route_stop.sequence_no,
            traveller_requests=[(self_identity_key, payload.seat_number)],
        )

        overlapping_active_booking_count = await self._count_overlapping_active_trip_bookings(
            scheduled_trip_id=trip.id,
            pickup_sequence_no=pickup_route_stop.sequence_no,
            dropoff_sequence_no=dropoff_route_stop.sequence_no,
        )
        seat_count = await self._get_app_bookable_capacity_for_trip(trip)

        if overlapping_active_booking_count >= seat_count:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "trip_segment_full",
                    "message": "No seats are currently available for the selected trip segment.",
                },
            )

        await self._ensure_requested_seat_available_for_leg(
            scheduled_trip_id=trip.id,
            seat_number=payload.seat_number,
            seat_capacity=seat_count,
            pickup_sequence_no=pickup_route_stop.sequence_no,
            dropoff_sequence_no=dropoff_route_stop.sequence_no,
        )

        gst_breakdown = await self._build_gst_breakdown(
            self._quantize_money(fare.amount),
            is_ac=None if trip.route is None else trip.route.has_ac,
        )
        normalized_fare_amount = gst_breakdown.gross_amount
        current_commission_percent = await self._get_current_commission_percent()
        (
            commission_percent_snapshot,
            commission_amount,
            driver_payout_amount,
        ) = self._build_booking_commission_snapshot(
            fare_amount=gst_breakdown.taxable_amount,
            commission_percent=current_commission_percent,
        )

        try:
            booking = TripBooking(
                passenger_user_id=current_user.id,
                booked_by_user_id=current_user.id,
                traveller_identity_key=self_identity_key,
                traveller_name_snapshot=profile.full_name,
                traveller_relationship_label_snapshot="Self",
                otp=self._generate_booking_otp(),
                scheduled_trip_id=trip.id,
                route_id=trip.route_id,
                pickup_stop_id=payload.pickup_stop_id,
                dropoff_stop_id=payload.dropoff_stop_id,
                seat_number=payload.seat_number,
                booking_status=BookingStatus.PENDING_PAYMENT,
                fare_amount=normalized_fare_amount,
                taxable_amount=gst_breakdown.taxable_amount,
                cgst_rate_percent_snapshot=gst_breakdown.cgst_rate_percent,
                cgst_amount=gst_breakdown.cgst_amount,
                sgst_rate_percent_snapshot=gst_breakdown.sgst_rate_percent,
                sgst_amount=gst_breakdown.sgst_amount,
                igst_rate_percent_snapshot=gst_breakdown.igst_rate_percent,
                igst_amount=gst_breakdown.igst_amount,
                total_tax_amount=gst_breakdown.total_tax_amount,
                gst_enabled_snapshot=(
                    gst_breakdown.gst_enabled and gst_breakdown.gst_applicable
                ),
                gst_inclusive_snapshot=gst_breakdown.gst_inclusive,
                pickup_sequence_no_snapshot=pickup_route_stop.sequence_no,
                dropoff_sequence_no_snapshot=dropoff_route_stop.sequence_no,
                payment_hold_expires_at=self._get_payment_hold_expires_at(),
                commission_percent_snapshot=commission_percent_snapshot,
                commission_amount=commission_amount,
                driver_payout_amount=driver_payout_amount,
            )
            self.db.add(booking)
            await self.db.flush()

            order_payload = await self._create_razorpay_order(
                booking=booking,
                amount=booking.fare_amount,
            )

            payment = BookingPayment(
                booking_id=booking.id,
                razorpay_order_id=order_payload["id"],
                amount=booking.fare_amount,
                taxable_amount=booking.taxable_amount,
                cgst_amount=booking.cgst_amount,
                sgst_amount=booking.sgst_amount,
                igst_amount=booking.igst_amount,
                total_tax_amount=booking.total_tax_amount,
                status=BookingPaymentStatus.CREATED,
            )
            self.db.add(payment)

            await self.db.commit()

            await self._broadcast_seatmap_snapshots_for_trip(
                scheduled_trip_id=trip.id,
                reason="booking_created",
            )

        except IntegrityError:
            await self.db.rollback()

            existing_stmt = (
                select(TripBooking)
                .where(
                    TripBooking.traveller_identity_key == self_identity_key,
                    TripBooking.scheduled_trip_id == trip.id,
                    TripBooking.booking_session_id.is_(None),
                    TripBooking.booking_status.in_(
                        (
                            BookingStatus.PENDING_PAYMENT,
                            BookingStatus.BOOKED,
                            BookingStatus.BOARDED,
                        )
                    ),
                )
                .options(selectinload(TripBooking.payments))
            )
            existing_result = await self.db.execute(existing_stmt)
            existing_bookings = list(existing_result.scalars().unique().all())
            existing_booking = next(
                (
                    booking
                    for booking in existing_bookings
                    if booking.pickup_stop_id == payload.pickup_stop_id
                    and booking.dropoff_stop_id == payload.dropoff_stop_id
                ),
                None,
            )

            if (
                existing_booking is not None
                and existing_booking.pickup_stop_id == payload.pickup_stop_id
                and existing_booking.dropoff_stop_id == payload.dropoff_stop_id
            ):
                latest_payment = self._get_latest_booking_payment(existing_booking)

                payment_order = None
                if (
                    existing_booking.booking_status == BookingStatus.PENDING_PAYMENT
                    and latest_payment is not None
                    and latest_payment.status == BookingPaymentStatus.CREATED
                ):
                    payment_order = self._build_payment_order_response(
                        booking=existing_booking,
                        razorpay_order_id=latest_payment.razorpay_order_id,
                    )

                return await self._build_create_booking_response(
                    booking_id=existing_booking.id,
                    passenger_user_id=current_user.id,
                    message="Booking already exists.",
                    payment_order=payment_order,
                )

            raise HTTPException(
                status_code=409,
                detail={
                    "error": "duplicate_booking",
                    "message": "Passenger already has a booking for this scheduled trip.",
                },
            )

        except HTTPException:
            await self.db.rollback()
            raise
        except Exception:
            await self.db.rollback()
            raise

        return await self._build_create_booking_response(
            booking_id=booking.id,
            passenger_user_id=current_user.id,
            message="Booking created. Payment is pending.",
            payment_order=self._build_payment_order_response(
                booking=booking,
                razorpay_order_id=order_payload["id"],
                currency=order_payload.get("currency", "INR"),
                receipt=order_payload.get("receipt"),
            ),
        )

    async def verify_booking_payment(
        self,
        current_user: User,
        booking_id: str,
        payload: VerifyBookingPaymentRequest,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        booking = await self._get_booking_obj(
            booking_id=booking_id,
            passenger_user_id=current_user.id,
        )

        payment = next(
            (item for item in booking.payments if item.razorpay_order_id == payload.razorpay_order_id),
            None,
        )

        if (
            payment is not None
            and payment.status == BookingPaymentStatus.PAID
            and booking.booking_status in (
                BookingStatus.BOOKED,
                BookingStatus.BOARDED,
                BookingStatus.COMPLETED,
            )
        ):
            await self._queue_invoice_email_delivery(booking)
            await self.db.commit()
            return {
                "message": "Payment already verified successfully.",
                "booking": self._serialize_booking(booking),
            }

        if booking.booking_status != BookingStatus.PENDING_PAYMENT:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "booking_not_pending_payment",
                    "message": "This booking is not awaiting payment verification.",
                },
            )

        if (
            booking.payment_hold_expires_at is not None
            and booking.payment_hold_expires_at <= utcnow()
        ):
            await self._expire_pending_booking_hold(booking)
            await self.db.commit()

            await self._broadcast_seatmap_snapshots_for_trip(
                scheduled_trip_id=booking.scheduled_trip_id,
                reason="payment_hold_expired",
            )

            await self._notify_user(
                user_id=current_user.id,
                title="Booking cancelled",
                message="Your payment window expired, so the seat was released.",
                data=self._build_booking_notification_data(
                    booking,
                    refresh=["bookings_list", "booking_detail"],
                ),
            )

            raise HTTPException(
                status_code=409,
                detail={
                    "error": "payment_hold_expired",
                    "message": "Payment hold expired. Booking was cancelled and the seat was released.",
                },
            )

        if payment is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "payment_order_not_found",
                    "message": "Payment order was not found for this booking.",
                },
            )

        if payment.status == BookingPaymentStatus.PAID:
            await self._mark_booking_paid_and_booked(
            booking,
            payment,
            razorpay_payment_id=payload.razorpay_payment_id,
            razorpay_signature=payload.razorpay_signature,
        )
            await self.db.commit()

            booking = await self._get_booking_obj(
                booking_id=booking.id,
                passenger_user_id=current_user.id,
            )

            await self._notify_user(
                user_id=current_user.id,
                title="Payment verified",
                message="Your booking is confirmed.",
                data=self._build_booking_notification_data(
                    booking,
                    refresh=["bookings_list", "booking_detail", "current_booking"],
                ),
            )

            return {
                "message": "Payment verified successfully.",
                "booking": self._serialize_booking(booking),
            }

        self._verify_razorpay_signature(
            order_id=payload.razorpay_order_id,
            payment_id=payload.razorpay_payment_id,
            received_signature=payload.razorpay_signature,
        )

        fetched_payment = await self._fetch_razorpay_payment(payload.razorpay_payment_id)
        fetched_order_id = fetched_payment.get("order_id")
        fetched_status = str(fetched_payment.get("status", "")).lower()
        fetched_captured = bool(fetched_payment.get("captured", False))
        fetched_amount = int(fetched_payment.get("amount", 0))

        expected_amount_subunits = self._to_subunits(payment.amount)

        if fetched_order_id != payment.razorpay_order_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "payment_order_mismatch",
                    "message": "Fetched payment order does not match stored payment order.",
                },
            )

        if fetched_amount != expected_amount_subunits:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "payment_amount_mismatch",
                    "message": "Fetched payment amount does not match booking amount.",
                },
            )

        if fetched_status == "authorized" and not fetched_captured:
            captured_payment = await self._capture_razorpay_payment(
                payload.razorpay_payment_id,
                expected_amount_subunits,
            )
            fetched_status = str(captured_payment.get("status", "")).lower()
            fetched_captured = bool(captured_payment.get("captured", False))

        if fetched_status in {"failed", "refunded"}:
            payment.razorpay_payment_id = payload.razorpay_payment_id
            payment.razorpay_signature = payload.razorpay_signature
            payment.status = BookingPaymentStatus.FAILED
            self.db.add(payment)
            await self.db.commit()

            raise HTTPException(
                status_code=409,
                detail={
                    "error": "payment_failed",
                    "message": "Payment failed. Retry by creating payment again for the same booking.",
                    "provider_status": fetched_status,
                },
            )

        if fetched_status != "captured" or not fetched_captured:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "payment_not_captured",
                    "message": "Payment is not in captured state yet.",
                    "provider_status": fetched_status,
                },
            )

        await self._mark_booking_paid_and_booked(
            booking,
            payment,
            razorpay_payment_id=payload.razorpay_payment_id,
            razorpay_signature=payload.razorpay_signature,
        )
        await self.db.commit()

        booking = await self._get_booking_obj(
            booking_id=booking.id,
            passenger_user_id=current_user.id,
        )

        return {
            "message": "Payment verified successfully.",
            "booking": self._serialize_booking(booking),
        }

    async def list_bookings(
        self,
        current_user: User,
        *,
        status: BookingStatus | None = None,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        stmt = (
            select(TripBooking)
            .where(TripBooking.passenger_user_id == current_user.id)
            .options(
                selectinload(TripBooking.pickup_stop),
                selectinload(TripBooking.dropoff_stop),
                selectinload(TripBooking.payments),
                selectinload(TripBooking.rating),
                selectinload(TripBooking.scheduled_trip)
                .selectinload(ScheduledTrip.route)
                .selectinload(Route.route_stops)
                .selectinload(RouteStop.stop),
                selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.vehicle),
                selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.driver),
            )
            .order_by(TripBooking.created_at.desc())
        )

        if status is not None:
            stmt = stmt.where(TripBooking.booking_status == status)

        result = await self.db.execute(stmt)
        bookings = result.scalars().unique().all()

        return {
            "items": [self._serialize_booking(booking) for booking in bookings],
            "count": len(bookings),
        }

    async def get_booking_detail(self, current_user: User, booking_id: str) -> dict[str, Any]:
        self.ensure_passenger(current_user)
        booking = await self._get_booking_obj(
            booking_id=booking_id,
            passenger_user_id=current_user.id,
        )
        return await self._serialize_booking_detail(booking)

    def _build_invoice_passenger_party(
        self,
        *,
        booking: TripBooking,
        passenger_user: User,
        passenger_profile: PassengerProfile | None,
    ) -> dict[str, Any]:
        account_name = self._clean_optional_text(
            None if passenger_profile is None else passenger_profile.full_name
        )
        account_email = self._clean_optional_text(passenger_user.email)
        traveller_name = self._clean_optional_text(
            booking.traveller_name_snapshot
        )
        traveller_email = self._clean_optional_text(
            booking.traveller_email_snapshot
        )

        # Older bookings may predate traveller snapshots, while a passenger
        # profile itself is optional. Use the durable user/profile identity as
        # fallback so API invoices and emailed PDFs never lose known details.
        account_name = account_name or traveller_name
        account_email = account_email or traveller_email
        traveller_name = traveller_name or account_name
        traveller_email = traveller_email or account_email

        return {
            "user_id": passenger_user.id,
            "full_name": account_name,
            "email": account_email,
            "traveller_name": traveller_name,
            "traveller_phone": self._clean_optional_text(
                booking.traveller_phone_snapshot
            ),
            "traveller_email": traveller_email,
            "traveller_relationship_label": self._clean_optional_text(
                booking.traveller_relationship_label_snapshot
            ),
        }
    
    async def _build_booking_invoice_payload(
        self,
        *,
        booking: TripBooking,
        passenger_user: User,
        passenger_profile: PassengerProfile | None,
    ) -> dict[str, Any]:
        latest_paid_payment = self._get_latest_paid_invoice_payment(booking)
        if latest_paid_payment is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "paid_payment_not_found",
                    "message": "A paid payment record is required before an invoice can be generated.",
                },
            )

        platform_settings = await self._get_platform_settings_obj()
        invoice_profile = gst_invoice_profile_from_settings(platform_settings)
        route = booking.scheduled_trip.route
        is_ac = bool(route.has_ac) if route is not None else False

        return {
            "invoice_number": self._generate_invoice_number(booking),
            "booking_id": booking.id,
            "booking_created_at": booking.created_at,
            "invoice_generated_at": utcnow(),
            "invoice_status": "preview",
            "currency": "INR",
            "supplier_gstin": invoice_profile["gstin"],
            "supplier": {
                "gstin": invoice_profile["gstin"],
                "legal_name": invoice_profile["legal_name"],
                "trade_name": invoice_profile["trade_name"],
                "registered_address": invoice_profile["registered_address"],
                "state_name": invoice_profile["state_name"],
                "state_code": invoice_profile["state_code"],
                "postal_code": invoice_profile["postal_code"],
            },
            "service": {
                "sac_code": invoice_profile["sac_code"],
                "description": invoice_profile["service_description"],
                "quantity": 1,
                "unit": "ride",
            },
            "place_of_supply": {
                "name": invoice_profile["default_place_of_supply"],
                "state_code": invoice_profile[
                    "default_place_of_supply_state_code"
                ],
            },
            "compliance": {
                "reverse_charge_applicable": invoice_profile[
                    "reverse_charge_applicable"
                ],
                "digital_signature": None,
                "irn": None,
                "acknowledgement_number": None,
                "acknowledgement_date": None,
                "signed_qr_code": None,
            },
            "passenger": self._build_invoice_passenger_party(
                booking=booking,
                passenger_user=passenger_user,
                passenger_profile=passenger_profile,
            ),
            "trip": {
                "scheduled_trip_id": booking.scheduled_trip_id,
                "route_id": booking.route_id,
                "seat_number": booking.seat_number,
                "route_name": None if route is None else route.name,
                "route_code": None if route is None else route.code,
                "is_ac": is_ac,
                "pickup_stop": self._serialize_stop_brief(booking.pickup_stop),
                "dropoff_stop": self._serialize_stop_brief(booking.dropoff_stop),
                "planned_start_at": booking.scheduled_trip.planned_start_at,
                "planned_end_at": booking.scheduled_trip.planned_end_at,
                "actual_start_at": booking.scheduled_trip.actual_start_at,
                "actual_end_at": booking.scheduled_trip.actual_end_at,
                "completed_at": booking.completed_at,
            },
            "breakdown": self._build_invoice_breakdown(
                total_booking_amount=booking.fare_amount,
                is_ac=is_ac,
                booking=booking,
            ),
            "payment": self._serialize_invoice_payment(
                booking, latest_paid_payment
            ),
        }

    async def get_booking_invoice(self, current_user: User, booking_id: str) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        booking = await self._get_booking_obj(
            booking_id=booking_id,
            passenger_user_id=current_user.id,
        )

        booking_status = (
            booking.booking_status.value
            if hasattr(booking.booking_status, "value")
            else str(booking.booking_status)
        )
        trip_status = (
            booking.scheduled_trip.status.value
            if hasattr(booking.scheduled_trip.status, "value")
            else str(booking.scheduled_trip.status)
        )

        if booking.booking_status not in {
            BookingStatus.BOOKED,
            BookingStatus.BOARDED,
            BookingStatus.COMPLETED,
        }:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "invoice_not_available",
                    "message": "Invoice is available only after successful payment.",
                    "booking_status": booking_status,
                    "trip_status": trip_status,
                },
            )

        passenger_profile = await self._get_profile_obj(current_user.id)
        return await self._build_booking_invoice_payload(
            booking=booking,
            passenger_user=current_user,
            passenger_profile=passenger_profile,
        )

    async def cancel_booking(self, current_user: User, booking_id: str) -> dict[str, Any]:
        self.ensure_passenger(current_user)
        booking = await self._get_booking_obj(
            booking_id=booking_id,
            passenger_user_id=current_user.id,
        )

        if booking.booking_status != BookingStatus.BOOKED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "cancel_not_allowed",
                    "message": "Only paid booked bookings can be cancelled.",
                },
            )

        has_paid_payment = any(
            payment.status == BookingPaymentStatus.PAID
            and payment.razorpay_payment_id
            for payment in booking.payments
        )
        if not has_paid_payment:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "paid_payment_not_found",
                    "message": "This booking does not have a paid payment eligible for cancellation.",
                },
            )

        cancel_cutoff_at = booking.scheduled_trip.planned_start_at - timedelta(hours=1)
        if utcnow() > cancel_cutoff_at:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "cancellation_window_closed",
                    "message": "Booking can only be cancelled up to 1 hour before the scheduled trip start time.",
                    "scheduled_trip_start_at": booking.scheduled_trip.planned_start_at.isoformat(),
                    "cancel_cutoff_at": cancel_cutoff_at.isoformat(),
                },
            )

        booking.booking_status = BookingStatus.CANCELLED
        self._set_cancellation_metadata(
            booking,
            reason="Booking cancelled by passenger.",
            source="passenger",
            cancelled_by_user_id=current_user.id,
        )
        booking.payment_hold_expires_at = None

        self.db.add(booking)
        await self.db.commit()

        await self._broadcast_seatmap_snapshots_for_trip(
            scheduled_trip_id=booking.scheduled_trip_id,
            reason="booking_cancelled",
        )

        booking = await self._get_booking_obj(
            booking_id=booking.id,
            passenger_user_id=current_user.id,
        )

        await self._notify_user(
            user_id=current_user.id,
            title="Booking cancelled",
            message="Your booking has been cancelled successfully.",
            data=self._build_booking_notification_data(
                booking,
                refresh=["bookings_list", "booking_detail", "history"],
            ),
        )

        return {
            "message": "Booking cancelled successfully.",
            "booking": self._serialize_booking(booking),
        }

    async def list_upcoming_bookings(self, current_user: User) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        stmt = (
            select(TripBooking)
            .join(ScheduledTrip, ScheduledTrip.id == TripBooking.scheduled_trip_id)
            .where(
                TripBooking.passenger_user_id == current_user.id,
                TripBooking.booking_status == BookingStatus.BOOKED,
                ScheduledTrip.planned_start_at > utcnow(),
            )
            .options(
                selectinload(TripBooking.pickup_stop),
                selectinload(TripBooking.dropoff_stop),
                selectinload(TripBooking.payments),
                selectinload(TripBooking.rating),
                selectinload(TripBooking.scheduled_trip)
                .selectinload(ScheduledTrip.route)
                .selectinload(Route.route_stops)
                .selectinload(RouteStop.stop),
                selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.vehicle),
                selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.driver),
            )
            .order_by(ScheduledTrip.planned_start_at.asc())
        )
        result = await self.db.execute(stmt)
        bookings = result.scalars().unique().all()

        return {
            "items": [self._serialize_booking(booking) for booking in bookings],
            "count": len(bookings),
        }

    async def _serialize_current_booking_session(
        self,
        booking_session: BookingSession,
        bookings: list[TripBooking],
    ) -> dict[str, Any]:
        serialized_bookings: list[dict[str, Any]] = []

        for booking in bookings:
            serialized_bookings.append(
                await self._serialize_current_booking(booking)
            )

        return {
            "booking_session_id": booking_session.id,
            "owner_user_id": booking_session.owner_user_id,
            "scheduled_trip_id": booking_session.scheduled_trip_id,
            "route_id": booking_session.route_id,
            "pickup_stop_id": booking_session.pickup_stop_id,
            "dropoff_stop_id": booking_session.dropoff_stop_id,
            "status": booking_session.status,
            "total_fare_amount": booking_session.total_fare_amount,
            "total_taxable_amount": self._quantize_money(
                Decimal(getattr(booking_session, "total_taxable_amount", 0) or 0)
            ),
            "total_cgst_amount": self._quantize_money(
                Decimal(getattr(booking_session, "total_cgst_amount", 0) or 0)
            ),
            "total_sgst_amount": self._quantize_money(
                Decimal(getattr(booking_session, "total_sgst_amount", 0) or 0)
            ),
            "total_igst_amount": self._quantize_money(
                Decimal(getattr(booking_session, "total_igst_amount", 0) or 0)
            ),
            "total_tax_amount": self._quantize_money(
                Decimal(getattr(booking_session, "total_tax_amount", 0) or 0)
            ),
            "gst_enabled_snapshot": bool(
                getattr(booking_session, "gst_enabled_snapshot", False)
            ),
            "gst_inclusive_snapshot": bool(
                getattr(booking_session, "gst_inclusive_snapshot", True)
            ),
            "cgst_rate_percent_snapshot": Decimal(
                getattr(booking_session, "cgst_rate_percent_snapshot", 0) or 0
            ),
            "sgst_rate_percent_snapshot": Decimal(
                getattr(booking_session, "sgst_rate_percent_snapshot", 0) or 0
            ),
            "igst_rate_percent_snapshot": Decimal(
                getattr(booking_session, "igst_rate_percent_snapshot", 0) or 0
            ),
            "payment_hold_expires_at": booking_session.payment_hold_expires_at,
            "confirmed_at": booking_session.confirmed_at,
            "cancelled_at": booking_session.cancelled_at,
            "expired_at": booking_session.expired_at,
            "bookings": serialized_bookings,
            "booking_count": len(serialized_bookings),
            "created_at": booking_session.created_at,
            "updated_at": booking_session.updated_at,
        }

    async def list_current_booking_sessions(
        self,
        current_user: User,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)
        now = utcnow()

        session_stmt = (
            select(BookingSession)
            .join(
                TripBooking,
                TripBooking.booking_session_id == BookingSession.id,
            )
            .join(
                ScheduledTrip,
                ScheduledTrip.id == BookingSession.scheduled_trip_id,
            )
            .where(
                BookingSession.owner_user_id == current_user.id,
                TripBooking.booking_status.in_(
                    (
                        BookingStatus.BOOKED,
                        BookingStatus.BOARDED,
                    )
                ),
                TripBooking.completed_at.is_(None),
                self._current_trip_sql_filter(now),
            )
            .options(
                selectinload(BookingSession.bookings).selectinload(
                    TripBooking.pickup_stop
                ),
                selectinload(BookingSession.bookings).selectinload(
                    TripBooking.dropoff_stop
                ),
                selectinload(BookingSession.bookings).selectinload(
                    TripBooking.payments
                ),
                selectinload(BookingSession.bookings).selectinload(
                    TripBooking.rating
                ),
                selectinload(BookingSession.bookings).selectinload(
                    TripBooking.scan_events
                ),
                selectinload(BookingSession.bookings)
                .selectinload(TripBooking.scheduled_trip)
                .selectinload(ScheduledTrip.route)
                .selectinload(Route.route_stops)
                .selectinload(RouteStop.stop),
                selectinload(BookingSession.bookings)
                .selectinload(TripBooking.scheduled_trip)
                .selectinload(ScheduledTrip.vehicle),
                selectinload(BookingSession.bookings)
                .selectinload(TripBooking.scheduled_trip)
                .selectinload(ScheduledTrip.driver),
                selectinload(BookingSession.bookings)
                .selectinload(TripBooking.scheduled_trip)
                .selectinload(ScheduledTrip.trip_events)
                .selectinload(TripEvent.stop),
            )
            .order_by(BookingSession.created_at.desc())
        )

        result = await self.db.execute(session_stmt)
        booking_sessions = list(result.scalars().unique().all())

        items: list[dict[str, Any]] = []

        for booking_session in booking_sessions:
            current_bookings = [
                booking
                for booking in booking_session.bookings
                if booking.booking_status
                in (
                    BookingStatus.BOOKED,
                    BookingStatus.BOARDED,
                )
                and self._is_current_booking_for_passenger(
                    booking,
                    now,
                )
            ]

            current_bookings.sort(
                key=lambda booking: (
                    booking.seat_number,
                    booking.created_at,
                )
            )

            if not current_bookings:
                continue

            items.append(
                await self._serialize_current_booking_session(
                    booking_session,
                    current_bookings,
                )
            )

        return {
            "items": items,
            "count": len(items),
        }

    async def get_booking_session_current_trip_status(
        self,
        current_user: User,
        booking_session_id: str,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        bookings = await self._list_booking_session_current_trip_bookings(
            booking_session_id=booking_session_id,
            owner_user_id=current_user.id,
        )

        return {
            "booking_session_id": booking_session_id,
            "items": [
                self._serialize_current_trip_status(booking)
                for booking in bookings
            ],
            "count": len(bookings),
        }

    async def get_booking_session_live_location(
        self,
        current_user: User,
        booking_session_id: str,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        bookings = await self._list_booking_session_current_trip_bookings(
            booking_session_id=booking_session_id,
            owner_user_id=current_user.id,
        )

        return {
            "booking_session_id": booking_session_id,
            "items": [
                self._serialize_booking_live_location(booking)
                for booking in bookings
            ],
            "count": len(bookings),
        }

    async def list_current_bookings(self, current_user: User) -> dict[str, Any]:
        self.ensure_passenger(current_user)
        now = utcnow()

        stmt = (
            select(TripBooking)
            .join(ScheduledTrip, ScheduledTrip.id == TripBooking.scheduled_trip_id)
            .where(
                TripBooking.passenger_user_id == current_user.id,
                TripBooking.booking_status.in_((BookingStatus.BOOKED, BookingStatus.BOARDED)),
                TripBooking.completed_at.is_(None),
                self._current_trip_sql_filter(now),
            )
            .options(
                selectinload(TripBooking.pickup_stop),
                selectinload(TripBooking.dropoff_stop),
                selectinload(TripBooking.payments),
                selectinload(TripBooking.rating),
                selectinload(TripBooking.scheduled_trip)
                .selectinload(ScheduledTrip.route)
                .selectinload(Route.route_stops)
                .selectinload(RouteStop.stop),
                selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.vehicle),
                selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.driver),
                selectinload(TripBooking.scheduled_trip)
                .selectinload(ScheduledTrip.trip_events)
                .selectinload(TripEvent.stop),
            )
            .order_by(ScheduledTrip.planned_start_at.asc())
        )
        result = await self.db.execute(stmt)
        bookings = result.scalars().unique().all()

        items: list[dict[str, Any]] = []
        for booking in bookings:
            items.append(await self._serialize_current_booking(booking))

        return {
            "items": items,
            "count": len(items),
        }

    async def list_history(self, current_user: User) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        history_statuses = (
            BookingStatus.COMPLETED,
            BookingStatus.CANCELLED,
            BookingStatus.MISSED,
        )

        stmt = (
            select(TripBooking)
            .where(
                TripBooking.passenger_user_id == current_user.id,
                TripBooking.booking_status.in_(history_statuses),
            )
            .options(
                selectinload(TripBooking.pickup_stop),
                selectinload(TripBooking.dropoff_stop),
                selectinload(TripBooking.payments),
                selectinload(TripBooking.rating),
                selectinload(TripBooking.scheduled_trip)
                .selectinload(ScheduledTrip.route)
                .selectinload(Route.route_stops)
                .selectinload(RouteStop.stop),
                selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.vehicle),
                selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.driver),
            )
            .order_by(TripBooking.updated_at.desc())
        )
        result = await self.db.execute(stmt)
        bookings = result.scalars().unique().all()

        return {
            "items": [self._serialize_booking(booking) for booking in bookings],
            "count": len(bookings),
        }

    @staticmethod
    def _transaction_booking_load_options() -> list[Any]:
        booking = selectinload(BookingPayment.booking)
        trip = booking.selectinload(TripBooking.scheduled_trip)
        return [
            booking.selectinload(TripBooking.pickup_stop),
            booking.selectinload(TripBooking.dropoff_stop),
            booking.selectinload(TripBooking.route),
            booking.selectinload(TripBooking.payments),
            booking.selectinload(TripBooking.booking_session).selectinload(
                BookingSession.payments
            ),
            booking.selectinload(TripBooking.rating),
            booking.selectinload(TripBooking.scan_events),
            trip.selectinload(ScheduledTrip.route)
            .selectinload(Route.route_stops)
            .selectinload(RouteStop.stop),
            trip.selectinload(ScheduledTrip.vehicle),
            trip.selectinload(ScheduledTrip.driver),
            trip.selectinload(ScheduledTrip.trip_events).selectinload(
                TripEvent.stop
            ),
        ]

    @staticmethod
    def _transaction_session_load_options() -> list[Any]:
        session = selectinload(BookingSessionPayment.booking_session)
        bookings = session.selectinload(BookingSession.bookings)
        trip = session.selectinload(BookingSession.scheduled_trip)
        booking_trip = bookings.selectinload(TripBooking.scheduled_trip)
        return [
            session.selectinload(BookingSession.payments),
            session.selectinload(BookingSession.pickup_stop),
            session.selectinload(BookingSession.dropoff_stop),
            session.selectinload(BookingSession.route),
            trip.selectinload(ScheduledTrip.route)
            .selectinload(Route.route_stops)
            .selectinload(RouteStop.stop),
            trip.selectinload(ScheduledTrip.vehicle),
            trip.selectinload(ScheduledTrip.driver),
            trip.selectinload(ScheduledTrip.trip_events).selectinload(
                TripEvent.stop
            ),
            bookings.selectinload(TripBooking.pickup_stop),
            bookings.selectinload(TripBooking.dropoff_stop),
            bookings.selectinload(TripBooking.route),
            bookings.selectinload(TripBooking.payments),
            bookings.selectinload(TripBooking.rating),
            bookings.selectinload(TripBooking.scan_events),
            booking_trip.selectinload(ScheduledTrip.route)
            .selectinload(Route.route_stops)
            .selectinload(RouteStop.stop),
            booking_trip.selectinload(ScheduledTrip.vehicle),
            booking_trip.selectinload(ScheduledTrip.driver),
            booking_trip.selectinload(ScheduledTrip.trip_events).selectinload(
                TripEvent.stop
            ),
        ]

    async def _serialize_detailed_transaction(
        self,
        payment: BookingPayment | BookingSessionPayment,
        *,
        current_user: User,
        passenger_profile: PassengerProfile | None,
    ) -> dict[str, Any]:
        if isinstance(payment, BookingPayment):
            item = self._serialize_transaction(payment)
            related_bookings = [payment.booking]
        else:
            item = self._serialize_booking_session_transaction(payment)
            related_bookings = sorted(
                payment.booking_session.bookings,
                key=lambda booking: booking.seat_number,
            )

        # Failed/pending attempts intentionally expose only payment and failure
        # diagnostics. Successful captures reuse the existing booking-detail and
        # invoice payloads so the FE sees one canonical representation.
        if payment.status != BookingPaymentStatus.PAID:
            return item

        booking_details = [
            await self._serialize_booking_detail(booking)
            for booking in related_bookings
        ]
        item["bookings"] = booking_details
        if isinstance(payment, BookingPayment) and booking_details:
            item["booking"] = booking_details[0]

        try:
            invoices = [
                await self._build_booking_invoice_payload(
                    booking=booking,
                    passenger_user=current_user,
                    passenger_profile=passenger_profile,
                )
                for booking in related_bookings
            ]
        except (RuntimeError, ValueError):
            item["invoice_unavailable_reason"] = "invoice_configuration_invalid"
            return item

        item["invoices"] = invoices
        if isinstance(payment, BookingPayment) and invoices:
            item["invoice"] = invoices[0]
        return item
    
    async def list_transaction_history(
        self,
        current_user: User,
        *,
        status: BookingPaymentStatus | None = None,
        month: int | None = None,
        year: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        if month is not None and year is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "year_required_for_month_filter",
                    "message": "year is required when month filter is provided.",
                },
            )

        visible_statuses = (
            BookingPaymentStatus.PAID,
            BookingPaymentStatus.FAILED,
            BookingPaymentStatus.REFUNDED,
        )
        direct_index = (
            select(
                BookingPayment.id.label("payment_id"),
                literal("booking").label("payment_source"),
                BookingPayment.created_at.label("created_at"),
            )
            .join(TripBooking, TripBooking.id == BookingPayment.booking_id)
            .where(TripBooking.passenger_user_id == current_user.id)
        )
        session_index = (
            select(
                BookingSessionPayment.id.label("payment_id"),
                literal("booking_session").label("payment_source"),
                BookingSessionPayment.created_at.label("created_at"),
            )
            .join(
                BookingSession,
                BookingSession.id == BookingSessionPayment.booking_session_id,
            )
            .where(BookingSession.owner_user_id == current_user.id)
        )

        if status is not None:
            direct_index = direct_index.where(BookingPayment.status == status)
            session_index = session_index.where(
                BookingSessionPayment.status == status
            )
        else:
            direct_index = direct_index.where(
                BookingPayment.status.in_(visible_statuses)
            )
            session_index = session_index.where(
                BookingSessionPayment.status.in_(visible_statuses)
            )

        if year is not None:
            direct_index = direct_index.where(
                func.extract("year", BookingPayment.created_at) == year
            )
            session_index = session_index.where(
                func.extract("year", BookingSessionPayment.created_at) == year
            )
        if month is not None:
            direct_index = direct_index.where(
                func.extract("month", BookingPayment.created_at) == month
            )
            session_index = session_index.where(
                func.extract("month", BookingSessionPayment.created_at) == month
            )

        history_index = union_all(direct_index, session_index).subquery()
        page_result = await self.db.execute(
            select(
                history_index.c.payment_id,
                history_index.c.payment_source,
                history_index.c.created_at,
            )
            .order_by(
                history_index.c.created_at.desc(),
                history_index.c.payment_id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
        page_rows = page_result.all()

        count_result = await self.db.execute(
            select(func.count()).select_from(history_index)
        )
        total_count = int(count_result.scalar_one() or 0)

        direct_ids = [
            row.payment_id for row in page_rows if row.payment_source == "booking"
        ]
        session_ids = [
            row.payment_id
            for row in page_rows
            if row.payment_source == "booking_session"
        ]

        payments_by_key: dict[
            tuple[str, str], BookingPayment | BookingSessionPayment
        ] = {}
        if direct_ids:
            direct_result = await self.db.execute(
                select(BookingPayment)
                .where(BookingPayment.id.in_(direct_ids))
                .options(*self._transaction_booking_load_options())
            )
            for payment in direct_result.scalars().unique().all():
                payments_by_key[("booking", payment.id)] = payment

        if session_ids:
            session_result = await self.db.execute(
                select(BookingSessionPayment)
                .where(BookingSessionPayment.id.in_(session_ids))
                .options(*self._transaction_session_load_options())
            )
            for payment in session_result.scalars().unique().all():
                payments_by_key[("booking_session", payment.id)] = payment

        has_success = any(
            payment.status == BookingPaymentStatus.PAID
            for payment in payments_by_key.values()
        )
        passenger_profile = (
            await self._get_profile_obj(current_user.id) if has_success else None
        )

        items: list[dict[str, Any]] = []
        for row in page_rows:
            payment = payments_by_key.get((row.payment_source, row.payment_id))
            if payment is None:
                continue
            item = await self._serialize_detailed_transaction(
                payment,
                current_user=current_user,
                passenger_profile=passenger_profile,
            )
            items.append(item)

        return {"items": items, "count": total_count}
    
    @staticmethod
    def _normalize_optional_datetime(value: datetime | None) -> datetime | None:
        if value is None:
            return None

        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)

        return value.astimezone(timezone.utc)

    async def _get_stop_obj_or_raise(self, stop_id: str) -> Stop:
        stmt = select(Stop).where(Stop.id == stop_id)
        result = await self.db.execute(stmt)
        stop = result.scalar_one_or_none()

        if stop is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "stop_not_found",
                    "message": "Stop not found.",
                    "stop_id": stop_id,
                },
            )

        return stop

    async def _serialize_route_trip_discovery_trip(
        self,
        *,
        trip: ScheduledTrip,
        pickup_route_stop: RouteStop,
        dropoff_route_stop: RouteStop,
    ) -> dict[str, Any]:
        pickup_planned_time = self._get_route_stop_planned_time(
            trip=trip,
            target_sequence_no=pickup_route_stop.sequence_no,
        )
        dropoff_planned_time = self._get_route_stop_planned_time(
            trip=trip,
            target_sequence_no=dropoff_route_stop.sequence_no,
        )

        seat_capacity = await self._get_app_bookable_capacity_for_trip(trip)

        overlapping_active_bookings = await self._count_overlapping_active_trip_bookings(
            scheduled_trip_id=trip.id,
            pickup_sequence_no=pickup_route_stop.sequence_no,
            dropoff_sequence_no=dropoff_route_stop.sequence_no,
        )

        occupied_seat_numbers = await self._get_occupied_app_seat_numbers_for_leg(
            scheduled_trip_id=trip.id,
            pickup_sequence_no=pickup_route_stop.sequence_no,
            dropoff_sequence_no=dropoff_route_stop.sequence_no,
        )

        available_seat_numbers = [
            seat_number
            for seat_number in range(1, seat_capacity + 1)
            if seat_number not in occupied_seat_numbers
        ]

        available_seats = len(available_seat_numbers)

        trip_bookable = (
            trip.status == ScheduledTripStatus.SCHEDULED
            and pickup_planned_time > utcnow()
            and available_seats > 0
        )

        return {
            "scheduled_trip_id": trip.id,
            "route_id": trip.route_id,
            "status": trip.status,
            "planned_start_at": trip.planned_start_at,
            "planned_end_at": trip.planned_end_at,
            "pickup_stop": self._serialize_stop_brief(pickup_route_stop.stop),
            "dropoff_stop": self._serialize_stop_brief(dropoff_route_stop.stop),
            "pickup_sequence_no": pickup_route_stop.sequence_no,
            "dropoff_sequence_no": dropoff_route_stop.sequence_no,
            "pickup_planned_time": pickup_planned_time,
            "dropoff_planned_time": dropoff_planned_time,
            "seat_capacity": seat_capacity,
            "overlapping_active_bookings": overlapping_active_bookings,
            "available_seats": available_seats,
            "occupied_seat_numbers": sorted(occupied_seat_numbers),
            "available_seat_numbers": available_seat_numbers,
            "trip_bookable": trip_bookable,
            "vehicle": None if trip.vehicle is None else {
                "id": trip.vehicle.id,
                "registration_number": trip.vehicle.registration_number,
                "vehicle_name": trip.vehicle.vehicle_name,
                "vehicle_model": trip.vehicle.vehicle_model,
                "color": trip.vehicle.color,
                "seat_count": trip.vehicle.seat_count,
                "rfid_reserved_seat_count": trip.rfid_reserved_seat_count,
                "app_bookable_seat_count": seat_capacity,
                "has_ac": trip.vehicle.has_ac,
            },
            "driver": None if trip.driver is None else {
                "id": trip.driver.id,
                "email": trip.driver.email,
            },
        }

    # ------------------------------------------------------------------
    # QR
    # ------------------------------------------------------------------
    @staticmethod
    def _get_qr_secret() -> str:
        value = os.getenv("PASSENGER_QR_SECRET", "").strip()
        if not value:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "missing_passenger_qr_secret",
                    "message": "PASSENGER_QR_SECRET is not configured.",
                },
            )
        return value

    def _build_qr_token(self, booking: TripBooking) -> tuple[str, dict[str, Any]]:
        payload = {
            "booking_id": booking.id,
            "issued_at": int(utcnow().timestamp()),
            "expires_at": int((utcnow() + timedelta(hours=12)).timestamp()),
        }
        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        payload_bytes = payload_json.encode("utf-8")

        signature = hmac.new(
            self._get_qr_secret().encode("utf-8"),
            payload_bytes,
            hashlib.sha256,
        ).hexdigest()

        encoded_payload = base64.urlsafe_b64encode(payload_bytes).decode("utf-8").rstrip("=")
        token = f"{encoded_payload}.{signature}"
        return token, payload

    async def get_booking_qr(self, current_user: User, booking_id: str) -> dict[str, Any]:
        self.ensure_passenger(current_user)
        booking = await self._get_booking_obj(
            booking_id=booking_id,
            passenger_user_id=current_user.id,
        )

        if booking.booking_status not in (BookingStatus.BOOKED, BookingStatus.BOARDED):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "qr_not_available",
                    "message": "QR is only available for active booked rides.",
                },
            )

        token, payload = self._build_qr_token(booking)
        return {
            "booking_id": booking.id,
            "qr_token": token,
            "payload": payload,
        }
    
    async def get_current_trip_status(
        self,
        current_user: User,
        booking_id: str,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        booking = await self._get_booking_obj(
            booking_id=booking_id,
            passenger_user_id=current_user.id,
        )

        return self._serialize_current_trip_status(booking)
    

    def _serialize_booking_live_location(
        self,
        booking: TripBooking,
    ) -> dict[str, Any]:
        trip = booking.scheduled_trip
        now = utcnow()

        terminal_booking_statuses = (
            BookingStatus.CANCELLED,
            BookingStatus.COMPLETED,
            BookingStatus.MISSED,
        )
        terminal_trip_statuses = (
            ScheduledTripStatus.CANCELLED,
            ScheduledTripStatus.COMPLETED,
            ScheduledTripStatus.PREMATURE_END,
            ScheduledTripStatus.PREMATURED_END_REQUEST,
        )

        tracking_active = (
            booking.booking_status not in terminal_booking_statuses
            and trip.status not in terminal_trip_statuses
            and trip.planned_start_at <= now
            and booking.completed_at is None
        )

        return {
            "booking_id": booking.id,
            "booking_session_id": booking.booking_session_id,
            "passenger_user_id": booking.passenger_user_id,
            "booked_by_user_id": booking.booked_by_user_id,

            "traveller_profile_id": booking.traveller_profile_id,
            "traveller_name_snapshot": booking.traveller_name_snapshot,
            "traveller_phone_snapshot": booking.traveller_phone_snapshot,
            "traveller_email_snapshot": booking.traveller_email_snapshot,
            "traveller_relationship_label_snapshot": (
                booking.traveller_relationship_label_snapshot
            ),

            "scheduled_trip_id": booking.scheduled_trip_id,
            "booking_status": booking.booking_status,
            "trip_status": trip.status,
            "tracking_active": tracking_active,
            "last_lat": trip.last_lat if tracking_active else None,
            "last_lng": trip.last_lng if tracking_active else None,
            "planned_start_at": trip.planned_start_at,
            "completed_at": booking.completed_at,
            "actual_end_at": trip.actual_end_at,
            "updated_at": trip.updated_at,
        }

    async def get_booking_live_location(
        self,
        current_user: User,
        booking_id: str,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        booking = await self._get_booking_obj(
            booking_id=booking_id,
            passenger_user_id=current_user.id,
        )

        return self._serialize_booking_live_location(booking)

    # ------------------------------------------------------------------
    # rating
    # ------------------------------------------------------------------
    async def create_rating(
        self,
        current_user: User,
        booking_id: str,
        payload: CreateBookingRatingRequest,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)
        booking = await self._get_booking_obj(
            booking_id=booking_id,
            passenger_user_id=current_user.id,
        )

        if booking.booking_status != BookingStatus.COMPLETED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rating_not_allowed",
                    "message": "Rating is allowed only after trip completion.",
                },
            )

        trip_end_at = (
            booking.scheduled_trip.actual_end_at
            or booking.scheduled_trip.planned_end_at
        )

        if trip_end_at.tzinfo is None:
            trip_end_at = trip_end_at.replace(tzinfo=timezone.utc)

        rating_deadline_at = trip_end_at + timedelta(hours=48)

        if datetime.now(timezone.utc) > rating_deadline_at:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rating_window_expired",
                    "message": "Rating is allowed only within 48 hours after trip end.",
                    "trip_end_at": trip_end_at.isoformat(),
                    "rating_deadline_at": rating_deadline_at.isoformat(),
                },
            )

        if booking.rating is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rating_already_exists",
                    "message": "Rating already exists for this booking.",
                },
            )

        rating = BookingRating(
            booking_id=booking.id,
            passenger_user_id=current_user.id,
            driver_user_id=booking.scheduled_trip.driver_user_id,
            scheduled_trip_id=booking.scheduled_trip_id,
            trip_rating=payload.trip_rating,
            driver_rating=payload.driver_rating,
            review_text=payload.review_text.strip() if payload.review_text else None,
        )
        self.db.add(rating)
        await self.db.commit()
        await self.db.refresh(rating)

        return {
            "message": "Rating submitted successfully.",
            "rating": self._serialize_rating(rating),
        }

    async def get_rating(self, current_user: User, booking_id: str) -> dict[str, Any]:
        self.ensure_passenger(current_user)
        booking = await self._get_booking_obj(
            booking_id=booking_id,
            passenger_user_id=current_user.id,
        )

        if booking.rating is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "rating_not_found",
                    "message": "Rating not found for this booking.",
                },
            )

        return self._serialize_rating(booking.rating)
    
    async def create_support_ticket(
    self,
    current_user: User,
    *,
    subject: str,
    description: str,
    file: UploadFile | None = None,
) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        cleaned_subject = self._clean_support_text(subject, field_name="subject")
        cleaned_description = self._clean_support_text(description, field_name="description")

        attachment_path: str | None = None

        if file is not None:
            try:
                content = await file.read()
            finally:
                await file.close()

            if not content:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "empty_attachment",
                        "message": "Attachment file is empty.",
                    },
                )

            upload_dir = self._get_support_upload_dir()
            extension = self._guess_support_attachment_extension(file.filename)
            filename = f"support_ticket_{current_user.id}_{uuid4().hex}{extension}"

            disk_path = upload_dir / filename
            disk_path.write_bytes(content)

            attachment_path = f"/uploads/support/passenger/{filename}"

        ticket = SupportTicket(
            user_id=current_user.id,
            subject=cleaned_subject,
            description=cleaned_description,
            attachment_path=attachment_path,
        )

        self.db.add(ticket)
        await self.db.commit()
        await self.db.refresh(ticket)

        # notify passenger
        await self._notify_user(
            user_id=current_user.id,
            title="Support ticket created",
            message="Your support request has been recorded.",
            data=self._build_support_ticket_notification_data(ticket),
        )

        # notify all active admins
        admin_stmt = select(User.id).where(
            User.role == UserRole.ADMIN,
            User.is_active.is_(True),
        )
        admin_result = await self.db.execute(admin_stmt)
        admin_user_ids = list(admin_result.scalars().all())

        admin_notification_data = {
            "ticket_id": ticket.id,
            "user_id": ticket.user_id,
            "subject": ticket.subject,
            "status": ticket.status.value,
            "refresh": ["support_tickets", "support_ticket_detail"],
        }

        for admin_user_id in admin_user_ids:
            await self._notify_user(
                user_id=admin_user_id,
                title="New support ticket",
                message=f"A new passenger support ticket was created: {ticket.subject}",
                data=admin_notification_data,
            )

        return {
            "message": "Support ticket created successfully.",
            "ticket": self._serialize_support_ticket(ticket),
        }

    async def list_support_tickets(self, current_user: User) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        stmt = (
            select(SupportTicket)
            .where(SupportTicket.user_id == current_user.id)
            .order_by(SupportTicket.created_at.desc())
        )
        result = await self.db.execute(stmt)
        tickets = result.scalars().all()

        return {
            "items": [self._serialize_support_ticket(ticket) for ticket in tickets],
            "count": len(tickets),
        }

    async def get_support_ticket(
        self,
        current_user: User,
        ticket_id: str,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        stmt = select(SupportTicket).where(
            SupportTicket.id == ticket_id,
            SupportTicket.user_id == current_user.id,
        )
        result = await self.db.execute(stmt)
        ticket = result.scalar_one_or_none()

        if ticket is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "support_ticket_not_found",
                    "message": "Support ticket not found.",
                },
            )

        return self._serialize_support_ticket(ticket)
