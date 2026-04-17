from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.schema import (
    BookingPayment,
    BookingPaymentStatus,
    BookingStatus,
    PayoutAdjustment,
    PayoutAdjustmentDecision,
    PayoutAdjustmentType,
    PlatformSettings,
    ScheduledTrip,
    TripBooking,
    User,
    UserRole,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FineRegisterService:
    """
    Registers policy-derived fines into the payout adjustment register.

    Important:
    - This service ONLY creates pending PayoutAdjustment rows.
    - It does NOT include/exclude them.
    - It does NOT apply them.
    - Caller should commit once around the surrounding trip state change + registration.
    """

    RULES_COLUMN_ENV_NAME = "COMMERCIAL_RULES_SETTINGS_COLUMN"
    DEFAULT_SYSTEM_USER_EMAIL = "system-fine-register@internal.invalid"

    # Since the latest surfaced schema excerpt here does not show your newly-added
    # rules column yet, keep this resolver tolerant. If your field is named
    # differently, either:
    # 1) set COMMERCIAL_RULES_SETTINGS_COLUMN env var, or
    # 2) add the actual field name to this tuple.
    RULES_COLUMN_CANDIDATES = (
        "commercial_rules_json",
        "commercial_policy_json",
        "fine_rules_json",
        "rules_json",
        "commercial_rules",
        "commercial_policy",
    )

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ---------------------------------------------------------
    # basic helpers
    # ---------------------------------------------------------
    @staticmethod
    def _quantize_money(value: Decimal) -> Decimal:
        return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @staticmethod
    def _parse_decimal(value: Any, *, default: Decimal = Decimal("0.00")) -> Decimal:
        if value is None or value == "":
            return default
        try:
            return Decimal(str(value))
        except Exception:
            return default

    @staticmethod
    def _parse_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _normalize_text(value: Any) -> str | None:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    @classmethod
    def _get_system_user_email(cls) -> str:
        value = os.getenv(
            "SYSTEM_FINE_REGISTER_EMAIL",
            cls.DEFAULT_SYSTEM_USER_EMAIL,
        ).strip().lower()
        if not value:
            raise RuntimeError("SYSTEM_FINE_REGISTER_EMAIL cannot be empty.")
        return value

    @classmethod
    def _get_rules_column_candidates(cls) -> tuple[str, ...]:
        override = os.getenv(cls.RULES_COLUMN_ENV_NAME, "").strip()
        if override:
            return (override, *cls.RULES_COLUMN_CANDIDATES)
        return cls.RULES_COLUMN_CANDIDATES

    def _get_rules_column_name(self, settings: PlatformSettings) -> str:
        for candidate in self._get_rules_column_candidates():
            if hasattr(settings, candidate):
                return candidate

        raise HTTPException(
            status_code=500,
            detail={
                "error": "commercial_rules_column_not_found",
                "message": (
                    "Commercial rules column was not found on PlatformSettings. "
                    "Set COMMERCIAL_RULES_SETTINGS_COLUMN or align the field name."
                ),
            },
        )

    def _get_rules_column_value(self, settings: PlatformSettings) -> str | None:
        attr_name = self._get_rules_column_name(settings)
        value = getattr(settings, attr_name, None)
        return None if value is None else str(value)

    # ---------------------------------------------------------
    # settings / rules
    # ---------------------------------------------------------
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

    def _empty_rule_register(self) -> dict[str, Any]:
        return {"version": 1, "rules": []}

    def _load_rule_register(self, settings: PlatformSettings | None) -> dict[str, Any]:
        if settings is None:
            return self._empty_rule_register()

        raw = (self._get_rules_column_value(settings) or "").strip()
        if not raw:
            return self._empty_rule_register()

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "invalid_commercial_rule_register",
                    "message": "Commercial rule register JSON is invalid.",
                },
            ) from exc

        if not isinstance(parsed, dict):
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "invalid_commercial_rule_register_shape",
                    "message": "Commercial rule register must be a JSON object.",
                },
            )

        rules = parsed.get("rules")
        if not isinstance(rules, list):
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "invalid_commercial_rule_register_shape",
                    "message": "Commercial rule register must contain a rules list.",
                },
            )

        return parsed

    def _get_active_rules_by_type(
        self,
        register: dict[str, Any],
        *,
        rule_type: str,
    ) -> list[dict[str, Any]]:
        items = []
        for raw in register.get("rules", []):
            if not isinstance(raw, dict):
                continue
            if raw.get("rule_type") != rule_type:
                continue
            if not bool(raw.get("is_active", False)):
                continue
            if not isinstance(raw.get("config"), dict):
                continue
            items.append(raw)

        items.sort(
            key=lambda item: (
                int(item.get("priority", 100)),
                str(item.get("created_at") or ""),
                str(item.get("id") or ""),
            )
        )
        return items

    def _matches_closed_range(
        self,
        *,
        value: int,
        min_value: int | None,
        max_value: int | None,
    ) -> bool:
        if min_value is not None and value < min_value:
            return False
        if max_value is not None and value > max_value:
            return False
        return True

    def _match_cancellation_rule(
        self,
        rules: list[dict[str, Any]],
        *,
        minutes_before_start: int,
    ) -> dict[str, Any] | None:
        for rule in rules:
            config = rule.get("config") or {}
            min_minutes_before = self._parse_int(config.get("min_minutes_before"))
            max_minutes_before = self._parse_int(config.get("max_minutes_before"))
            if self._matches_closed_range(
                value=minutes_before_start,
                min_value=min_minutes_before,
                max_value=max_minutes_before,
            ):
                return rule
        return None

    def _match_latency_rule(
        self,
        rules: list[dict[str, Any]],
        *,
        minutes_late_after_grace: int,
    ) -> dict[str, Any] | None:
        for rule in rules:
            config = rule.get("config") or {}
            min_minutes_late = self._parse_int(config.get("min_minutes_late"))
            max_minutes_late = self._parse_int(config.get("max_minutes_late"))
            if self._matches_closed_range(
                value=minutes_late_after_grace,
                min_value=min_minutes_late,
                max_value=max_minutes_late,
            ):
                return rule
        return None

    # ---------------------------------------------------------
    # system actor
    # ---------------------------------------------------------
    async def _get_system_fine_register_user_id(self) -> str:
        system_email = self._get_system_user_email()

        stmt = (
            select(User.id)
            .where(
                User.email == system_email,
                User.role == UserRole.ADMIN,
            )
            .limit(1)
        )
        result = await self.db.execute(stmt)
        user_id = result.scalar_one_or_none()

        if user_id is None:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "system_fine_register_user_not_found",
                    "message": (
                        "System fine register user was not found. "
                        "Bootstrap that user before registering fines."
                    ),
                },
            )

        return user_id

    # ---------------------------------------------------------
    # trip / booking fetch
    # ---------------------------------------------------------
    async def _get_trip_obj_for_fine_registration(
        self,
        scheduled_trip_id: str,
    ) -> ScheduledTrip:
        stmt = (
            select(ScheduledTrip)
            .where(ScheduledTrip.id == scheduled_trip_id)
            .with_for_update()
            .options(
                selectinload(ScheduledTrip.bookings).selectinload(TripBooking.payments),
                selectinload(ScheduledTrip.bookings).selectinload(
                    TripBooking.originated_payout_adjustments
                ),
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

    @staticmethod
    def _ensure_trip_belongs_to_driver(
        trip: ScheduledTrip,
        *,
        driver_user_id: str,
    ) -> None:
        if trip.driver_user_id != driver_user_id:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "trip_driver_mismatch",
                    "message": "This trip does not belong to the current driver.",
                },
            )

    # ---------------------------------------------------------
    # booking selection / fine calculation
    # ---------------------------------------------------------
    @staticmethod
    def _booking_has_paid_payment(booking: TripBooking) -> bool:
        return any(
            payment.status == BookingPaymentStatus.PAID
            and bool(payment.razorpay_payment_id)
            for payment in getattr(booking, "payments", []) or []
        )

    def _is_cancellation_fine_candidate(self, booking: TripBooking) -> bool:
        if self._parse_decimal(getattr(booking, "fare_amount", None)) <= Decimal("0.00"):
            return False

        if self._booking_has_paid_payment(booking):
            return True

        return booking.booking_status in {
            BookingStatus.BOOKED,
            BookingStatus.BOARDED,
            BookingStatus.COMPLETED,
        }

    def _is_latency_fine_candidate(self, booking: TripBooking) -> bool:
        if self._parse_decimal(getattr(booking, "fare_amount", None)) <= Decimal("0.00"):
            return False

        return booking.booking_status in {
            BookingStatus.BOOKED,
            BookingStatus.BOARDED,
            BookingStatus.COMPLETED,
        }

    def _compute_fine_amount(
        self,
        *,
        booking: TripBooking,
        rule: dict[str, Any],
    ) -> Decimal:
        config = rule.get("config") or {}
        fine_mode = self._normalize_text(config.get("fine_mode")) or "flat_per_booking"
        fine_value = self._quantize_money(
            self._parse_decimal(config.get("fine_value"))
        )

        if fine_value <= Decimal("0.00"):
            return Decimal("0.00")

        fare_amount = self._quantize_money(
            self._parse_decimal(getattr(booking, "fare_amount", None))
        )
        driver_payout_amount = self._quantize_money(
            self._parse_decimal(getattr(booking, "driver_payout_amount", None))
        )

        if fine_mode == "flat_per_booking":
            return fine_value

        if fine_mode == "percent_of_fare":
            return self._quantize_money((fare_amount * fine_value) / Decimal("100"))

        if fine_mode == "percent_of_driver_payout":
            return self._quantize_money(
                (driver_payout_amount * fine_value) / Decimal("100")
            )

        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_commercial_rule_fine_mode",
                "message": f"Unsupported fine_mode: {fine_mode}",
                "rule_id": rule.get("id"),
                "rule_code": rule.get("code"),
            },
        )

    def _build_reason_code(
        self,
        *,
        event_type: str,
        rule: dict[str, Any],
    ) -> str:
        rule_code = self._normalize_text(rule.get("code")) or "rule"
        value = f"{event_type}:{rule_code}"
        return value[:64]

    def _build_reason_text(
        self,
        *,
        event_type: str,
        trip: ScheduledTrip,
        booking: TripBooking,
        rule: dict[str, Any],
        metric_value: int,
    ) -> str:
        rule_title = self._normalize_text(rule.get("title")) or "Commercial rule"

        if event_type == "driver_trip_cancel":
            return (
                f"{rule_title}. Auto-registered because driver cancelled trip "
                f"{trip.id} {metric_value} minute(s) before planned start. "
                f"Booking {booking.id} was affected."
            )

        if event_type == "trip_latency":
            return (
                f"{rule_title}. Auto-registered because trip {trip.id} ended "
                f"{metric_value} minute(s) late after grace. "
                f"Booking {booking.id} was affected."
            )

        return f"{rule_title}. Auto-registered from active commercial rule."

    async def _find_existing_adjustment(
        self,
        *,
        origin_booking_id: str,
        reason_code: str,
    ) -> PayoutAdjustment | None:
        stmt = (
            select(PayoutAdjustment)
            .where(
                PayoutAdjustment.origin_booking_id == origin_booking_id,
                PayoutAdjustment.reason_code == reason_code,
            )
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _register_adjustment_if_missing(
        self,
        *,
        booking: TripBooking,
        system_user_id: str,
        amount: Decimal,
        reason_code: str,
        reason_text: str,
        admin_note: str,
    ) -> tuple[PayoutAdjustment | None, bool]:
        if amount <= Decimal("0.00"):
            return None, False

        existing = await self._find_existing_adjustment(
            origin_booking_id=booking.id,
            reason_code=reason_code,
        )
        if existing is not None:
            return existing, False

        adjustment = PayoutAdjustment(
            origin_booking_id=booking.id,
            adjustment_type=PayoutAdjustmentType.FINE,
            amount=self._quantize_money(amount),
            reason_code=reason_code,
            reason_text=reason_text,
            admin_note=admin_note,
            decision_status=PayoutAdjustmentDecision.PENDING,
            created_by_admin_id=system_user_id,
        )
        self.db.add(adjustment)
        await self.db.flush()
        return adjustment, True

    # ---------------------------------------------------------
    # public methods
    # ---------------------------------------------------------
    async def register_driver_trip_cancellation_fines(
        self,
        *,
        scheduled_trip_id: str,
        driver_user_id: str,
        cancellation_reason: str | None,
        occurred_at: datetime | None = None,
    ) -> dict[str, Any]:
        event_time = occurred_at or utcnow()

        trip = await self._get_trip_obj_for_fine_registration(scheduled_trip_id)
        self._ensure_trip_belongs_to_driver(trip, driver_user_id=driver_user_id)

        settings = await self._get_platform_settings_obj()
        register = self._load_rule_register(settings)
        rules = self._get_active_rules_by_type(
            register,
            rule_type="driver_trip_cancel",
        )

        minutes_before_start = int(
            (trip.planned_start_at - event_time).total_seconds() // 60
        )

        matched_rule = self._match_cancellation_rule(
            rules,
            minutes_before_start=minutes_before_start,
        )
        if matched_rule is None:
            return {
                "message": "No active cancellation fine rule matched.",
                "scheduled_trip_id": trip.id,
                "driver_user_id": driver_user_id,
                "event_type": "driver_trip_cancel",
                "minutes_before_start": minutes_before_start,
                "matched_rule_id": None,
                "matched_rule_code": None,
                "registered_count": 0,
                "total_registered_amount": Decimal("0.00"),
                "items": [],
            }

        config = matched_rule.get("config") or {}
        if config.get("allowed") is False:
            return {
                "message": "Matched rule blocks cancellation, so no fine was registered.",
                "scheduled_trip_id": trip.id,
                "driver_user_id": driver_user_id,
                "event_type": "driver_trip_cancel",
                "minutes_before_start": minutes_before_start,
                "matched_rule_id": matched_rule.get("id"),
                "matched_rule_code": matched_rule.get("code"),
                "registered_count": 0,
                "total_registered_amount": Decimal("0.00"),
                "items": [],
            }

        system_user_id = await self._get_system_fine_register_user_id()
        reason_code = self._build_reason_code(
            event_type="driver_trip_cancel",
            rule=matched_rule,
        )
        admin_note = (
            "System-registered from active commercial rule after driver trip cancellation. "
            "Pending admin review."
        )

        items: list[dict[str, Any]] = []
        total_registered_amount = Decimal("0.00")

        for booking in trip.bookings:
            if not self._is_cancellation_fine_candidate(booking):
                continue

            amount = self._compute_fine_amount(
                booking=booking,
                rule=matched_rule,
            )
            reason_text = self._build_reason_text(
                event_type="driver_trip_cancel",
                trip=trip,
                booking=booking,
                rule=matched_rule,
                metric_value=minutes_before_start,
            )

            adjustment, created = await self._register_adjustment_if_missing(
                booking=booking,
                system_user_id=system_user_id,
                amount=amount,
                reason_code=reason_code,
                reason_text=reason_text,
                admin_note=admin_note,
            )
            if adjustment is None or not created:
                continue

            total_registered_amount += self._quantize_money(adjustment.amount)
            items.append(
                {
                    "booking_id": booking.id,
                    "payout_adjustment_id": adjustment.id,
                    "amount": adjustment.amount,
                }
            )

        total_registered_amount = self._quantize_money(total_registered_amount)

        return {
            "message": "Cancellation fine registration completed.",
            "scheduled_trip_id": trip.id,
            "driver_user_id": driver_user_id,
            "event_type": "driver_trip_cancel",
            "cancellation_reason": self._normalize_text(cancellation_reason),
            "minutes_before_start": minutes_before_start,
            "matched_rule_id": matched_rule.get("id"),
            "matched_rule_code": matched_rule.get("code"),
            "registered_count": len(items),
            "total_registered_amount": total_registered_amount,
            "items": items,
        }

    async def register_driver_trip_latency_fines(
        self,
        *,
        scheduled_trip_id: str,
        driver_user_id: str,
        actual_end_at: datetime,
        occurred_at: datetime | None = None,
    ) -> dict[str, Any]:
        _ = occurred_at  # reserved for symmetry / future audit use

        trip = await self._get_trip_obj_for_fine_registration(scheduled_trip_id)
        self._ensure_trip_belongs_to_driver(trip, driver_user_id=driver_user_id)

        settings = await self._get_platform_settings_obj()
        register = self._load_rule_register(settings)
        rules = self._get_active_rules_by_type(
            register,
            rule_type="trip_latency",
        )

        raw_minutes_late = int(
            (actual_end_at - trip.planned_end_at).total_seconds() // 60
        )
        raw_minutes_late = max(raw_minutes_late, 0)

        matched_rule: dict[str, Any] | None = None
        effective_minutes_late_after_grace = 0

        for rule in rules:
            config = rule.get("config") or {}
            grace_minutes = max(self._parse_int(config.get("grace_minutes")) or 0, 0)
            candidate_minutes = max(raw_minutes_late - grace_minutes, 0)

            if self._match_latency_rule([rule], minutes_late_after_grace=candidate_minutes):
                matched_rule = rule
                effective_minutes_late_after_grace = candidate_minutes
                break

        if matched_rule is None:
            return {
                "message": "No active latency fine rule matched.",
                "scheduled_trip_id": trip.id,
                "driver_user_id": driver_user_id,
                "event_type": "trip_latency",
                "raw_minutes_late": raw_minutes_late,
                "minutes_late_after_grace": 0,
                "matched_rule_id": None,
                "matched_rule_code": None,
                "registered_count": 0,
                "total_registered_amount": Decimal("0.00"),
                "items": [],
            }

        system_user_id = await self._get_system_fine_register_user_id()
        reason_code = self._build_reason_code(
            event_type="trip_latency",
            rule=matched_rule,
        )
        admin_note = (
            "System-registered from active commercial rule after late trip completion. "
            "Pending admin review."
        )

        items: list[dict[str, Any]] = []
        total_registered_amount = Decimal("0.00")

        for booking in trip.bookings:
            if not self._is_latency_fine_candidate(booking):
                continue

            amount = self._compute_fine_amount(
                booking=booking,
                rule=matched_rule,
            )
            reason_text = self._build_reason_text(
                event_type="trip_latency",
                trip=trip,
                booking=booking,
                rule=matched_rule,
                metric_value=effective_minutes_late_after_grace,
            )

            adjustment, created = await self._register_adjustment_if_missing(
                booking=booking,
                system_user_id=system_user_id,
                amount=amount,
                reason_code=reason_code,
                reason_text=reason_text,
                admin_note=admin_note,
            )
            if adjustment is None or not created:
                continue

            total_registered_amount += self._quantize_money(adjustment.amount)
            items.append(
                {
                    "booking_id": booking.id,
                    "payout_adjustment_id": adjustment.id,
                    "amount": adjustment.amount,
                }
            )

        total_registered_amount = self._quantize_money(total_registered_amount)

        return {
            "message": "Latency fine registration completed.",
            "scheduled_trip_id": trip.id,
            "driver_user_id": driver_user_id,
            "event_type": "trip_latency",
            "raw_minutes_late": raw_minutes_late,
            "minutes_late_after_grace": effective_minutes_late_after_grace,
            "matched_rule_id": matched_rule.get("id"),
            "matched_rule_code": matched_rule.get("code"),
            "registered_count": len(items),
            "total_registered_amount": total_registered_amount,
            "items": items,
        }