from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.schema import (
    BookingPayment,
    BookingPaymentStatus,
    BookingStatus,
    BookingTransfer,
    BookingTransferStatus,
    DriverPayoutDetails,
    LinkedAccountStatus,
    PayoutAdjustment,
    PayoutAdjustmentApplication,
    PayoutAdjustmentDecision,
    PlatformSettings,
    RouteProductStatus,
    ScheduledTrip,
    TransferStatus,
    TripBooking,
    RFIDPayoutTransfer,
    RFIDPayoutTransferStatus,
    RFIDTripRide,
)

from app.notifications.hub import WSHub
from app.notifications.service import NotificationService

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RoutePayoutService:
    def __init__(
        self,
        db: AsyncSession,
        ws_hub: WSHub | None = None,
    ) -> None:
        self.db = db
        self.ws_hub = ws_hub

    # ---------------------------------------------------------
    # basic helpers
    # ---------------------------------------------------------
    @staticmethod
    def _quantize_money(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @classmethod
    def _to_subunits(cls, amount: Decimal) -> int:
        return int((cls._quantize_money(amount) * 100).to_integral_value(rounding=ROUND_HALF_UP))

    @staticmethod
    def _from_subunits(amount_subunits: int) -> Decimal:
        return (Decimal(amount_subunits) / Decimal("100")).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

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


    @staticmethod
    def _get_razorpay_api_root() -> str:
        raw = os.getenv("RAZORPAY_BASE_URL", "https://api.razorpay.com/v1").strip().rstrip("/")

        if raw.endswith("/v1") or raw.endswith("/v2"):
            return raw.rsplit("/", 1)[0]

        return raw
    
    def _get_notification_service(self) -> NotificationService:
        return NotificationService(
            db=self.db,
            ws_hub=self.ws_hub,
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

    @staticmethod
    def _build_transfer_notification_data(booking: TripBooking) -> dict[str, Any]:
        return {
            "booking_id": booking.id,
            "scheduled_trip_id": booking.scheduled_trip_id,
            "transfer_status": booking.transfer_status.value,
            "transfer_id": None if booking.transfer is None else booking.transfer.id,
            "refresh": ["driver_payouts"],
        }
    
    @staticmethod
    def _map_provider_linked_account_status(
        provider_status: str | None,
    ) -> LinkedAccountStatus:
        normalized = (provider_status or "").strip().lower()

        if normalized == "created":
            return LinkedAccountStatus.CREATED

        if normalized == "under_review":
            return LinkedAccountStatus.UNDER_REVIEW

        if normalized == "needs_clarification":
            return LinkedAccountStatus.NEEDS_CLARIFICATION

        if normalized in {"active", "activated"}:
            return LinkedAccountStatus.ACTIVE

        if normalized in {"blocked", "suspended"}:
            return LinkedAccountStatus.BLOCKED

        if normalized == "rejected":
            return LinkedAccountStatus.REJECTED

        if normalized in {"deleted", "closed"}:
            return LinkedAccountStatus.DELETED

        return LinkedAccountStatus.NOT_CREATED

    @staticmethod
    def _map_provider_route_product_status(
        provider_status: str | None,
    ) -> RouteProductStatus:
        normalized = (provider_status or "").strip().lower()

        if normalized == "requested":
            return RouteProductStatus.REQUESTED

        if normalized == "under_review":
            return RouteProductStatus.UNDER_REVIEW

        if normalized == "needs_clarification":
            return RouteProductStatus.NEEDS_CLARIFICATION

        if normalized == "activated":
            return RouteProductStatus.ACTIVATED

        if normalized == "suspended":
            return RouteProductStatus.SUSPENDED

        return RouteProductStatus.NOT_REQUESTED
    
    # ---------------------------------------------------------
    # db fetch helpers
    # ---------------------------------------------------------
    async def _get_booking_obj(self, booking_id: str) -> TripBooking:
        stmt = (
            select(TripBooking)
            .where(TripBooking.id == booking_id)
            .options(
                selectinload(TripBooking.payments),
                selectinload(TripBooking.transfer),
                selectinload(TripBooking.scheduled_trip),
                selectinload(TripBooking.applied_payout_adjustment_applications).selectinload(
                    PayoutAdjustmentApplication.adjustment
                ),
            )
        )
        result = await self.db.execute(stmt)
        booking = result.scalar_one_or_none()
        if booking is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "booking_not_found",
                    "message": "Booking not found.",
                },
            )
        return booking

    async def _get_driver_payout_details(
        self,
        driver_user_id: str,
    ) -> DriverPayoutDetails | None:
        stmt = select(DriverPayoutDetails).where(
            DriverPayoutDetails.driver_user_id == driver_user_id
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_default_commission_percent(self) -> Decimal:
        stmt = (
            select(PlatformSettings)
            .where(PlatformSettings.settings_key == "default")
            .limit(1)
        )
        result = await self.db.execute(stmt)
        settings = result.scalar_one_or_none()
        if settings is None:
            return Decimal("0.00")
        return self._quantize_money(settings.commission_percent)
    
    def _get_adjustment_applied_total(
    self,
    adjustment: PayoutAdjustment,
) -> Decimal:
        total = Decimal("0.00")
        for application in adjustment.applications:
            total += self._quantize_money(application.applied_amount)
        return self._quantize_money(total)


    def _get_adjustment_remaining_amount(
        self,
        adjustment: PayoutAdjustment,
    ) -> Decimal:
        remaining = self._quantize_money(adjustment.amount) - self._get_adjustment_applied_total(adjustment)
        if remaining < Decimal("0.00"):
            return Decimal("0.00")
        return self._quantize_money(remaining)


    @staticmethod
    def _normalize_adjustment_allocations(
        adjustments_to_apply: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for raw in adjustments_to_apply or []:
            adjustment_id = str(raw.get("adjustment_id") or "").strip()
            if not adjustment_id:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "missing_adjustment_id",
                        "message": "Each adjustment allocation must include adjustment_id.",
                    },
                )

            if adjustment_id in seen_ids:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "duplicate_adjustment_allocation",
                        "message": "The same adjustment cannot be allocated twice in one payout trigger.",
                        "adjustment_id": adjustment_id,
                    },
                )
            seen_ids.add(adjustment_id)

            try:
                applied_amount = Decimal(str(raw.get("applied_amount")))
            except Exception as exc:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "invalid_applied_amount",
                        "message": "Applied amount must be a valid decimal value.",
                        "adjustment_id": adjustment_id,
                    },
                ) from exc

            if applied_amount <= Decimal("0.00"):
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "non_positive_applied_amount",
                        "message": "Applied amount must be greater than 0.",
                        "adjustment_id": adjustment_id,
                    },
                )

            normalized.append(
                {
                    "adjustment_id": adjustment_id,
                    "applied_amount": applied_amount,
                }
            )

        return normalized


    async def _get_allocatable_adjustments(
        self,
        *,
        driver_user_id: str,
        adjustment_ids: list[str],
    ) -> dict[str, PayoutAdjustment]:
        if not adjustment_ids:
            return {}

        stmt = (
            select(PayoutAdjustment)
            .join(TripBooking, TripBooking.id == PayoutAdjustment.origin_booking_id)
            .join(ScheduledTrip, ScheduledTrip.id == TripBooking.scheduled_trip_id)
            .where(
                PayoutAdjustment.id.in_(adjustment_ids),
                ScheduledTrip.driver_user_id == driver_user_id,
            )
            .options(
                selectinload(PayoutAdjustment.applications),
                selectinload(PayoutAdjustment.origin_booking).selectinload(TripBooking.scheduled_trip),
            )
        )
        result = await self.db.execute(stmt)
        adjustments = result.scalars().unique().all()
        return {adjustment.id: adjustment for adjustment in adjustments}


    def _serialize_payout_adjustment_application(
        self,
        application: PayoutAdjustmentApplication,
    ) -> dict[str, Any]:
        return {
            "id": application.id,
            "payout_adjustment_id": application.payout_adjustment_id,
            "applied_on_booking_id": application.applied_on_booking_id,
            "booking_transfer_id": application.booking_transfer_id,
            "applied_by_admin_id": application.applied_by_admin_id,
            "applied_amount": application.applied_amount,
            "applied_at": application.applied_at,
            "created_at": application.created_at,
            "updated_at": application.updated_at,
        }


    async def _create_adjustment_applications(
        self,
        *,
        booking: TripBooking,
        booking_transfer_id: str | None,
        applied_by_admin_id: str,
        normalized_allocations: list[dict[str, Any]],
    ) -> list[PayoutAdjustmentApplication]:
        applications: list[PayoutAdjustmentApplication] = []

        for item in normalized_allocations:
            application = PayoutAdjustmentApplication(
                payout_adjustment_id=item["adjustment_id"],
                applied_on_booking_id=booking.id,
                booking_transfer_id=booking_transfer_id,
                applied_by_admin_id=applied_by_admin_id,
                applied_amount=self._quantize_money(item["applied_amount"]),
                applied_at=utcnow(),
            )
            self.db.add(application)
            applications.append(application)

        await self.db.flush()
        return applications
    
    # ---------------------------------------------------------
    # business helpers
    # ---------------------------------------------------------
    @staticmethod
    def _select_paid_source_payment(booking: TripBooking) -> BookingPayment:
        paid_payments = [
            payment
            for payment in booking.payments
            if payment.status == BookingPaymentStatus.PAID and payment.razorpay_payment_id
        ]

        if not paid_payments:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "paid_source_payment_not_found",
                    "message": "No paid source payment exists for this booking.",
                },
            )

        paid_payments.sort(key=lambda item: item.created_at, reverse=True)
        return paid_payments[0]

    def _ensure_transfer_trigger_allowed(
        self,
        booking: TripBooking,
        *,
        require_completed: bool,
    ) -> None:
        if require_completed and booking.booking_status != BookingStatus.COMPLETED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "booking_not_completed",
                    "message": "Transfer can only be triggered after booking completion.",
                },
            )

        if booking.booking_status == BookingStatus.CANCELLED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "booking_cancelled",
                    "message": "Cannot create transfer for a cancelled booking.",
                },
            )

        if booking.booking_status == BookingStatus.MISSED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "booking_missed",
                    "message": "Cannot create transfer for a missed booking.",
                },
            )

    async def _resolve_linked_account_id(
        self,
        *,
        driver_user_id: str,
        linked_account_id: str | None,
    ) -> tuple[str, DriverPayoutDetails | None]:
        cleaned_override = (linked_account_id or "").strip()
        payout_details = await self._get_driver_payout_details(driver_user_id)

        if cleaned_override:
            return cleaned_override, payout_details

        if payout_details is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "payout_details_not_found",
                    "message": "Driver payout details were not found.",
                },
            )

        if not payout_details.razorpay_linked_account_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "linked_account_not_available",
                    "message": "Driver linked payout account is not available.",
                },
            )

        if payout_details.linked_account_status != LinkedAccountStatus.ACTIVE:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "linked_account_not_active",
                    "message": "Driver linked payout account is not active.",
                    "linked_account_status": payout_details.linked_account_status.value,
                },
            )

        if payout_details.route_product_status != RouteProductStatus.ACTIVATED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "route_product_not_activated",
                    "message": "Driver Route product is not activated.",
                    "route_product_status": None
                    if payout_details.route_product_status is None
                    else payout_details.route_product_status.value,
                },
            )

        if not payout_details.is_payout_eligible:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "driver_not_payout_eligible",
                    "message": "Driver is not yet payout eligible.",
                },
            )

        return payout_details.razorpay_linked_account_id, payout_details

    async def _ensure_booking_snapshot_values(self, booking: TripBooking) -> None:
        commission_percent = self._quantize_money(booking.commission_percent_snapshot)

        if commission_percent == Decimal("0.00"):
            commission_percent = await self._get_default_commission_percent()

        fare_amount = self._quantize_money(booking.fare_amount)
        commission_amount = self._quantize_money(
            (fare_amount * commission_percent) / Decimal("100")
        )
        driver_payout_amount = self._quantize_money(fare_amount - commission_amount)

        if driver_payout_amount <= Decimal("0.00"):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "non_positive_driver_payout_amount",
                    "message": "Driver payout amount is not positive, so transfer cannot be created.",
                },
            )

        booking.commission_percent_snapshot = commission_percent
        booking.commission_amount = commission_amount
        booking.driver_payout_amount = driver_payout_amount

        if booking.transfer_status == TransferStatus.NOT_READY:
            booking.transfer_status = TransferStatus.READY
            booking.transfer_ready_at = utcnow()

        self.db.add(booking)
        await self.db.flush()

    @staticmethod
    def _map_provider_transfer_status(provider_status: str | None) -> tuple[BookingTransferStatus, TransferStatus]:
        normalized = (provider_status or "").strip().lower()

        if normalized == "processed":
            return BookingTransferStatus.PROCESSED, TransferStatus.TRANSFERRED

        if normalized in {"failed"}:
            return BookingTransferStatus.FAILED, TransferStatus.FAILED

        if normalized in {"reversed", "partially_reversed"}:
            return BookingTransferStatus.REVERSED, TransferStatus.REVERSED

        return BookingTransferStatus.CREATED, TransferStatus.READY

    # ---------------------------------------------------------
    # razorpay
    # ---------------------------------------------------------
    async def _razorpay_request(
        self,
        *,
        method: str,
        path: str,
        json_payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        key_id = self._get_razorpay_key_id()
        key_secret = self._get_razorpay_key_secret()
        base_url = self._get_razorpay_base_url()

        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = f"{base_url}{path}"

        async with httpx.AsyncClient(auth=(key_id, key_secret), timeout=20.0) as client:
            response = await client.request(
                method=method,
                url=url,
                json=json_payload,
                headers=headers,
            )

        if response.status_code >= 400:
            try:
                provider_error = response.json()
            except Exception:
                provider_error = {"raw": response.text}

            raise HTTPException(
                status_code=502,
                detail={
                    "error": "razorpay_route_request_failed",
                    "message": "Razorpay Route request failed.",
                    "provider_status_code": response.status_code,
                    "provider_response": provider_error,
                },
            )

        return response.json()

    async def _create_transfer_from_payment(
        self,
        *,
        razorpay_payment_id: str,
        linked_account_id: str,
        amount_subunits: int,
        booking: TripBooking,
    ) -> dict[str, Any]:
        if amount_subunits < 100:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "transfer_amount_too_small",
                    "message": "Transfer amount must be at least 100 subunits.",
                },
            )

        payload = {
            "transfers": [
                {
                    "account": linked_account_id,
                    "amount": amount_subunits,
                    "currency": "INR",
                    "notes": {
                        "booking_id": booking.id,
                        "scheduled_trip_id": booking.scheduled_trip_id,
                        "driver_user_id": booking.scheduled_trip.driver_user_id,
                    },
                    "linked_account_notes": [
                        "booking_id",
                        "scheduled_trip_id",
                    ],
                }
            ]
        }

        return await self._razorpay_request(
            method="POST",
            path=f"/payments/{razorpay_payment_id}/transfers",
            json_payload=payload,
        )
    
    async def _create_rfid_transfer_from_payment(
        self,
        *,
        razorpay_payment_id: str,
        linked_account_id: str,
        amount_subunits: int,
        transfer: RFIDPayoutTransfer,
    ) -> dict[str, Any]:
        if amount_subunits < 100:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_transfer_amount_too_small",
                    "message": "RFID transfer amount must be at least 100 subunits.",
                },
            )

        payload = {
            "transfers": [
                {
                    "account": linked_account_id,
                    "amount": amount_subunits,
                    "currency": "INR",
                    "notes": {
                        "rfid_payout_transfer_id": transfer.id,
                        "rfid_ride_id": transfer.rfid_ride_id,
                        "scheduled_trip_id": transfer.scheduled_trip_id,
                        "driver_user_id": transfer.driver_user_id,
                    },
                    "linked_account_notes": [
                        "rfid_payout_transfer_id",
                        "rfid_ride_id",
                        "scheduled_trip_id",
                    ],
                }
            ]
        }

        return await self._razorpay_request(
            method="POST",
            path=f"/payments/{razorpay_payment_id}/transfers",
            json_payload=payload,
        )

    @staticmethod
    def _extract_first_provider_transfer(
        provider_response: dict[str, Any],
    ) -> dict[str, Any]:
        items = provider_response.get("items")

        if isinstance(items, list) and items:
            first_item = items[0]
            if isinstance(first_item, dict):
                return first_item

        transfers = provider_response.get("transfers")

        if isinstance(transfers, list) and transfers:
            first_transfer = transfers[0]
            if isinstance(first_transfer, dict):
                return first_transfer

        return provider_response

    @staticmethod
    def _map_provider_rfid_transfer_status(
        provider_status: str | None,
    ) -> RFIDPayoutTransferStatus:
        normalized = (provider_status or "").strip().lower()

        if normalized == "processed":
            return RFIDPayoutTransferStatus.PROCESSED

        if normalized == "failed":
            return RFIDPayoutTransferStatus.FAILED

        if normalized in {"reversed", "partially_reversed"}:
            return RFIDPayoutTransferStatus.REVERSED

        return RFIDPayoutTransferStatus.CREATED

    async def _get_rfid_payout_transfer_for_update(
        self,
        transfer_id: str,
    ) -> RFIDPayoutTransfer:
        stmt = (
            select(RFIDPayoutTransfer)
            .where(RFIDPayoutTransfer.id == transfer_id)
            .with_for_update()
            .limit(1)
        )
        result = await self.db.execute(stmt)
        transfer = result.scalar_one_or_none()

        if transfer is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "rfid_payout_transfer_not_found",
                    "message": "RFID payout transfer not found.",
                },
            )

        return transfer

    async def _refresh_rfid_ride_transfer_status(
        self,
        rfid_ride_id: str,
    ) -> None:
        ride_stmt = (
            select(RFIDTripRide)
            .where(RFIDTripRide.id == rfid_ride_id)
            .with_for_update()
            .limit(1)
        )
        ride_result = await self.db.execute(ride_stmt)
        ride = ride_result.scalar_one_or_none()

        if ride is None:
            return

        transfer_stmt = select(RFIDPayoutTransfer.status).where(
            RFIDPayoutTransfer.rfid_ride_id == rfid_ride_id
        )
        transfer_result = await self.db.execute(transfer_stmt)
        statuses = list(transfer_result.scalars().all())

        if not statuses:
            return

        now = utcnow()

        if all(status == RFIDPayoutTransferStatus.PROCESSED for status in statuses):
            ride.transfer_status = RFIDPayoutTransferStatus.PROCESSED
            ride.transfer_processed_at = now
        elif any(status == RFIDPayoutTransferStatus.FAILED for status in statuses):
            ride.transfer_status = RFIDPayoutTransferStatus.FAILED
        elif any(status == RFIDPayoutTransferStatus.CREATED for status in statuses):
            ride.transfer_status = RFIDPayoutTransferStatus.CREATED
        elif any(status == RFIDPayoutTransferStatus.READY for status in statuses):
            ride.transfer_status = RFIDPayoutTransferStatus.READY
        elif any(status == RFIDPayoutTransferStatus.WITHHELD for status in statuses):
            ride.transfer_status = RFIDPayoutTransferStatus.WITHHELD
        elif all(status == RFIDPayoutTransferStatus.REVERSED for status in statuses):
            ride.transfer_status = RFIDPayoutTransferStatus.REVERSED

        self.db.add(ride)

    async def trigger_rfid_payout_transfer(
        self,
        transfer_id: str,
    ) -> dict[str, Any]:
        transfer = await self._get_rfid_payout_transfer_for_update(transfer_id)

        if transfer.status == RFIDPayoutTransferStatus.PROCESSED:
            return {
                "message": "RFID payout transfer already processed.",
                "transfer_id": transfer.id,
                "rfid_ride_id": transfer.rfid_ride_id,
                "razorpay_transfer_id": transfer.razorpay_transfer_id,
                "status": transfer.status,
                "amount": transfer.amount,
                "processed_at": transfer.processed_at,
            }

        if transfer.status != RFIDPayoutTransferStatus.READY:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rfid_payout_transfer_not_ready",
                    "message": "Only READY RFID payout transfers can be triggered.",
                    "transfer_status": transfer.status.value,
                },
            )

        if not transfer.source_razorpay_payment_id:
            transfer.status = RFIDPayoutTransferStatus.WITHHELD
            transfer.failure_reason = "rfid_source_razorpay_payment_missing"
            self.db.add(transfer)
            await self._refresh_rfid_ride_transfer_status(transfer.rfid_ride_id)
            await self.db.flush()

            return {
                "message": "RFID payout transfer withheld because source Razorpay payment is missing.",
                "transfer_id": transfer.id,
                "rfid_ride_id": transfer.rfid_ride_id,
                "status": transfer.status,
                "failure_reason": transfer.failure_reason,
            }

        if not transfer.linked_account_id:
            transfer.status = RFIDPayoutTransferStatus.WITHHELD
            transfer.failure_reason = "driver_linked_account_missing"
            self.db.add(transfer)
            await self._refresh_rfid_ride_transfer_status(transfer.rfid_ride_id)
            await self.db.flush()

            return {
                "message": "RFID payout transfer withheld because driver linked account is missing.",
                "transfer_id": transfer.id,
                "rfid_ride_id": transfer.rfid_ride_id,
                "status": transfer.status,
                "failure_reason": transfer.failure_reason,
            }

        amount_subunits = self._to_subunits(transfer.amount)

        try:
            provider_response = await self._create_rfid_transfer_from_payment(
                razorpay_payment_id=transfer.source_razorpay_payment_id,
                linked_account_id=transfer.linked_account_id,
                amount_subunits=amount_subunits,
                transfer=transfer,
            )
        except HTTPException as exc:
            transfer.status = RFIDPayoutTransferStatus.FAILED
            transfer.failure_reason = "razorpay_rfid_transfer_request_failed"
            transfer.provider_response_json = json.dumps(
                exc.detail,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )

            self.db.add(transfer)
            await self._refresh_rfid_ride_transfer_status(transfer.rfid_ride_id)
            await self.db.flush()

            return {
                "message": "RFID payout transfer failed at Razorpay.",
                "transfer_id": transfer.id,
                "rfid_ride_id": transfer.rfid_ride_id,
                "status": transfer.status,
                "failure_reason": transfer.failure_reason,
                "provider_error": exc.detail,
            }

        provider_transfer = self._extract_first_provider_transfer(provider_response)
        provider_status = provider_transfer.get("status")
        mapped_status = self._map_provider_rfid_transfer_status(provider_status)

        transfer.status = mapped_status
        transfer.razorpay_transfer_id = provider_transfer.get("id")
        transfer.provider_response_json = json.dumps(
            provider_response,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )

        if mapped_status == RFIDPayoutTransferStatus.PROCESSED:
            transfer.processed_at = utcnow()
            transfer.failure_reason = None
        elif mapped_status == RFIDPayoutTransferStatus.FAILED:
            transfer.failure_reason = "razorpay_rfid_transfer_failed"
        else:
            transfer.failure_reason = None

        self.db.add(transfer)
        await self._refresh_rfid_ride_transfer_status(transfer.rfid_ride_id)
        await self.db.flush()

        return {
            "message": "RFID payout transfer trigger completed.",
            "transfer_id": transfer.id,
            "rfid_ride_id": transfer.rfid_ride_id,
            "razorpay_transfer_id": transfer.razorpay_transfer_id,
            "status": transfer.status,
            "amount": transfer.amount,
            "linked_account_id": transfer.linked_account_id,
            "source_razorpay_payment_id": transfer.source_razorpay_payment_id,
            "processed_at": transfer.processed_at,
            "failure_reason": transfer.failure_reason,
            "provider_response": provider_response,
        }
    
    async def trigger_ready_rfid_payout_transfers(
        self,
        *,
        transfer_ids: list[str] | None = None,
        driver_user_id: str | None = None,
        scheduled_trip_id: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        normalized_limit = max(1, min(int(limit), 100))

        filters = [
            RFIDPayoutTransfer.status == RFIDPayoutTransferStatus.READY,
        ]

        requested_count = 0

        if transfer_ids:
            cleaned_transfer_ids = [
                str(transfer_id).strip()
                for transfer_id in transfer_ids
                if str(transfer_id).strip()
            ]

            if not cleaned_transfer_ids:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "empty_rfid_payout_transfer_ids",
                        "message": "At least one RFID payout transfer id is required.",
                    },
                )

            requested_count = len(cleaned_transfer_ids)
            filters.append(RFIDPayoutTransfer.id.in_(cleaned_transfer_ids))

        if driver_user_id is not None:
            filters.append(RFIDPayoutTransfer.driver_user_id == driver_user_id)

        if scheduled_trip_id is not None:
            filters.append(RFIDPayoutTransfer.scheduled_trip_id == scheduled_trip_id)

        stmt = (
            select(RFIDPayoutTransfer.id)
            .where(*filters)
            .order_by(RFIDPayoutTransfer.created_at.asc())
            .limit(normalized_limit)
        )
        result = await self.db.execute(stmt)
        selected_transfer_ids = list(result.scalars().all())

        items: list[dict[str, Any]] = []

        for selected_transfer_id in selected_transfer_ids:
            try:
                item = await self.trigger_rfid_payout_transfer(selected_transfer_id)
                await self.db.commit()

                items.append(
                    {
                        "transfer_id": selected_transfer_id,
                        "ok": True,
                        "result": item,
                        "error": None,
                    }
                )

            except HTTPException as exc:
                await self.db.rollback()

                items.append(
                    {
                        "transfer_id": selected_transfer_id,
                        "ok": False,
                        "result": None,
                        "error": exc.detail,
                    }
                )

        successful_count = sum(1 for item in items if item["ok"])
        failed_count = len(items) - successful_count

        return {
            "message": "RFID payout transfer bulk trigger completed.",
            "requested_count": requested_count,
            "selected_count": len(selected_transfer_ids),
            "successful_count": successful_count,
            "failed_count": failed_count,
            "items": items,
        }

    async def _fetch_razorpay_payment(
        self,
        razorpay_payment_id: str,
    ) -> dict[str, Any]:
        return await self._razorpay_request(
            method="GET",
            path=f"/payments/{razorpay_payment_id}",
        )

    async def _create_payment_refund(
        self,
        *,
        razorpay_payment_id: str,
        amount_subunits: int,
        idempotency_key: str,
        reverse_all: bool,
        receipt: str,
        notes: dict[str, str],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "amount": amount_subunits,
            "receipt": receipt,
            "notes": notes,
        }

        if reverse_all:
            payload["reverse_all"] = True

        return await self._razorpay_request(
            method="POST",
            path=f"/payments/{razorpay_payment_id}/refund",
            json_payload=payload,
            headers={
                "X-Refund-Idempotency": idempotency_key,
            },
        )

    @staticmethod
    def _is_provider_payment_fully_refunded(
        provider_payment: dict[str, Any],
        *,
        expected_amount_subunits: int,
    ) -> bool:
        provider_status = str(provider_payment.get("status") or "").strip().lower()
        refund_status = str(provider_payment.get("refund_status") or "").strip().lower()

        try:
            amount_refunded = int(provider_payment.get("amount_refunded") or 0)
        except (TypeError, ValueError):
            amount_refunded = 0

        return (
            provider_status == "refunded"
            or refund_status == "full"
            or amount_refunded >= expected_amount_subunits
        )
    
    async def _schedule_cancelled_booking_refund_retry(
        self,
        *,
        booking: TripBooking,
        delay_minutes: int,
    ) -> None:
        booking.refund_attempt_count = int(booking.refund_attempt_count or 0) + 1
        booking.refund_retry_after = utcnow() + timedelta(minutes=delay_minutes)

        self.db.add(booking)
        await self.db.flush()

    async def _clear_cancelled_booking_refund_retry(
        self,
        *,
        booking: TripBooking,
    ) -> None:
        booking.refund_retry_after = None
        self.db.add(booking)
        await self.db.flush()

    async def _mark_cancelled_booking_refunded_locally(
        self,
        *,
        booking: TripBooking,
        payment: BookingPayment,
        mark_transfer_reversed: bool,
    ) -> None:
        payment.status = BookingPaymentStatus.REFUNDED
        self.db.add(payment)

        booking.refund_retry_after = None
        self.db.add(booking)

        if mark_transfer_reversed and booking.transfer is not None:
            booking.transfer.status = BookingTransferStatus.REVERSED
            booking.transfer.reversed_at = booking.transfer.reversed_at or utcnow()
            booking.transfer.failure_reason = None

            booking.transfer_status = TransferStatus.REVERSED

            self.db.add(booking.transfer)
            self.db.add(booking)

        await self.db.flush()

    async def reconcile_cancelled_booking_refund(
        self,
        booking: TripBooking,
    ) -> str:
        if booking.booking_status != BookingStatus.CANCELLED:
            return "skip_non_cancelled"

        if (
            booking.refund_retry_after is not None
            and booking.refund_retry_after > utcnow()
        ):
            return "skip_retry_not_due"

        try:
            source_payment = self._select_paid_source_payment(booking)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            if detail.get("error") == "paid_source_payment_not_found":
                return "skip_no_paid_source_payment"
            raise

        if not source_payment.razorpay_payment_id:
            return "skip_missing_payment_id"

        expected_amount_subunits = self._to_subunits(source_payment.amount)

        provider_payment = await self._fetch_razorpay_payment(
            source_payment.razorpay_payment_id
        )

        transfer_was_processed = bool(
            booking.transfer is not None
            and booking.transfer.status == BookingTransferStatus.PROCESSED
        )

        if self._is_provider_payment_fully_refunded(
            provider_payment,
            expected_amount_subunits=expected_amount_subunits,
        ):
            await self._mark_cancelled_booking_refunded_locally(
                booking=booking,
                payment=source_payment,
                mark_transfer_reversed=transfer_was_processed,
            )
            return "already_refunded_on_provider"

        provider_status = str(provider_payment.get("status") or "").strip().lower()
        captured_flag = bool(provider_payment.get("captured", False))

        if provider_status != "captured" and not captured_flag:
            await self._schedule_cancelled_booking_refund_retry(
                booking=booking,
                delay_minutes=10,
            )
            return f"retry_provider_payment_{provider_status or 'unknown'}"

        idempotency_key = f"booking_refund_{source_payment.id.replace('-', '_')}"
        receipt = f"booking_refund_{booking.id.replace('-', '')[:24]}"

        try:
            refund_response = await self._create_payment_refund(
                razorpay_payment_id=source_payment.razorpay_payment_id,
                amount_subunits=expected_amount_subunits,
                idempotency_key=idempotency_key,
                reverse_all=transfer_was_processed,
                receipt=receipt,
                notes={
                    "booking_id": booking.id,
                    "scheduled_trip_id": booking.scheduled_trip_id,
                    "reason": "cancelled_booking_auto_refund",
                },
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            provider_status_code = detail.get("provider_status_code")

            refreshed_provider_payment = await self._fetch_razorpay_payment(
                source_payment.razorpay_payment_id
            )

            if self._is_provider_payment_fully_refunded(
                refreshed_provider_payment,
                expected_amount_subunits=expected_amount_subunits,
            ):
                await self._mark_cancelled_booking_refunded_locally(
                    booking=booking,
                    payment=source_payment,
                    mark_transfer_reversed=transfer_was_processed,
                )
                return "refund_already_processed_after_error"

            if provider_status_code == 409:
                await self._schedule_cancelled_booking_refund_retry(
                    booking=booking,
                    delay_minutes=5,
                )
                return "refund_in_progress"

            if provider_status_code in {408, 429, 500, 502, 503, 504}:
                await self._schedule_cancelled_booking_refund_retry(
                    booking=booking,
                    delay_minutes=10,
                )
                return "refund_retry_scheduled_after_provider_error"

            raise

        refund_status = str(refund_response.get("status") or "").strip().lower()

        if refund_status == "processed":
            await self._mark_cancelled_booking_refunded_locally(
                booking=booking,
                payment=source_payment,
                mark_transfer_reversed=transfer_was_processed,
            )
            return "refund_processed"

        if refund_status == "pending":
            await self._schedule_cancelled_booking_refund_retry(
                booking=booking,
                delay_minutes=5,
            )
            return "refund_pending"

        if refund_status == "failed":
            await self._schedule_cancelled_booking_refund_retry(
                booking=booking,
                delay_minutes=30,
            )
            return "refund_failed"

        refreshed_provider_payment = await self._fetch_razorpay_payment(
            source_payment.razorpay_payment_id
        )

        if self._is_provider_payment_fully_refunded(
            refreshed_provider_payment,
            expected_amount_subunits=expected_amount_subunits,
        ):
            await self._mark_cancelled_booking_refunded_locally(
                booking=booking,
                payment=source_payment,
                mark_transfer_reversed=transfer_was_processed,
            )
            return "refund_processed_after_fetch"

        await self._schedule_cancelled_booking_refund_retry(
            booking=booking,
            delay_minutes=15,
        )
        return f"refund_{refund_status or 'unknown'}"

    # ---------------------------------------------------------
    # public trigger
    # ---------------------------------------------------------
    async def trigger_transfer_for_booking(
    self,
    booking_id: str,
    *,
    linked_account_id: str | None = None,
    require_completed: bool = True,
    adjustments_to_apply: list[dict[str, Any]] | None = None,
    applied_by_admin_id: str | None = None,
) -> dict[str, Any]:
        booking = await self._get_booking_obj(booking_id)
        self._ensure_transfer_trigger_allowed(
            booking,
            require_completed=require_completed,
        )

        if booking.transfer is not None and booking.transfer.status == BookingTransferStatus.PROCESSED:
            return {
                "message": "Transfer already processed for this booking.",
                "booking_id": booking.id,
                "transfer_id": booking.transfer.id,
                "razorpay_transfer_id": booking.transfer.razorpay_transfer_id,
                "transfer_status": booking.transfer.status,
                "booking_transfer_status": booking.transfer_status,
                "amount": booking.transfer.amount,
                "linked_account_id": booking.transfer.linked_account_id,
                "processed_at": booking.transfer.processed_at,
            }

        source_payment = self._select_paid_source_payment(booking)

        resolved_linked_account_id, payout_details = await self._resolve_linked_account_id(
            driver_user_id=booking.scheduled_trip.driver_user_id,
            linked_account_id=linked_account_id,
        )

        await self._ensure_booking_snapshot_values(booking)

        normalized_allocations = self._normalize_adjustment_allocations(adjustments_to_apply)

        if normalized_allocations and not applied_by_admin_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "applied_by_admin_required",
                    "message": "Admin identity is required when applying payout adjustments.",
                },
            )

        selected_adjustment_total = Decimal("0.00")
        adjustment_map: dict[str, PayoutAdjustment] = {}

        if normalized_allocations:
            adjustment_map = await self._get_allocatable_adjustments(
                driver_user_id=booking.scheduled_trip.driver_user_id,
                adjustment_ids=[item["adjustment_id"] for item in normalized_allocations],
            )

            if len(adjustment_map) != len(normalized_allocations):
                found_ids = set(adjustment_map.keys())
                requested_ids = {item["adjustment_id"] for item in normalized_allocations}
                missing_ids = sorted(requested_ids - found_ids)
                raise HTTPException(
                    status_code=404,
                    detail={
                        "error": "payout_adjustment_not_found_for_driver",
                        "message": "One or more selected payout adjustments were not found for this driver.",
                        "missing_adjustment_ids": missing_ids,
                    },
                )

            for item in normalized_allocations:
                adjustment = adjustment_map[item["adjustment_id"]]

                if adjustment.decision_status != PayoutAdjustmentDecision.INCLUDED:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": "adjustment_not_included",
                            "message": "Only INCLUDED payout adjustments can be applied.",
                            "adjustment_id": adjustment.id,
                            "decision_status": adjustment.decision_status.value,
                        },
                    )

                remaining_amount = self._get_adjustment_remaining_amount(adjustment)
                applied_amount = self._quantize_money(item["applied_amount"])

                if applied_amount > remaining_amount:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": "applied_amount_exceeds_remaining_adjustment_balance",
                            "message": "Applied amount exceeds the remaining open balance for the selected adjustment.",
                            "adjustment_id": adjustment.id,
                            "remaining_amount": remaining_amount,
                            "requested_applied_amount": applied_amount,
                        },
                    )

                selected_adjustment_total += applied_amount

        selected_adjustment_total = self._quantize_money(selected_adjustment_total)
        gross_driver_payout_amount = self._quantize_money(booking.driver_payout_amount)

        if selected_adjustment_total > gross_driver_payout_amount:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "applied_adjustments_exceed_booking_payout",
                    "message": "Applied payout adjustments exceed this booking's gross driver payout amount.",
                    "gross_driver_payout_amount": gross_driver_payout_amount,
                    "applied_adjustment_amount": selected_adjustment_total,
                },
            )

        net_transfer_amount = self._quantize_money(
            gross_driver_payout_amount - selected_adjustment_total
        )

        if net_transfer_amount == Decimal("0.00"):
            if booking.transfer is not None:
                if booking.transfer.razorpay_transfer_id:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": "provider_transfer_already_started",
                            "message": "Cannot fully withhold this payout after a provider transfer has already been started.",
                        },
                    )
                await self.db.delete(booking.transfer)
                await self.db.flush()

            applications = []
            if normalized_allocations:
                applications = await self._create_adjustment_applications(
                    booking=booking,
                    booking_transfer_id=None,
                    applied_by_admin_id=applied_by_admin_id,  # type: ignore[arg-type]
                    normalized_allocations=normalized_allocations,
                )

            booking.transfer_status = TransferStatus.WITHHELD
            booking.transfer_ready_at = booking.transfer_ready_at or utcnow()
            booking.transfer_processed_at = None
            booking.withheld_at = utcnow()

            self.db.add(booking)
            await self.db.commit()

            return {
                "message": "Payout fully absorbed by applied adjustments.",
                "booking_id": booking.id,
                "driver_user_id": booking.scheduled_trip.driver_user_id,
                "source_booking_payment_id": source_payment.id,
                "source_razorpay_payment_id": source_payment.razorpay_payment_id,
                "linked_account_id": resolved_linked_account_id,
                "linked_account_source": (
                    "override"
                    if (linked_account_id or "").strip()
                    else "driver_payout_details"
                ),
                "payout_details_found": payout_details is not None,
                "commission_percent_snapshot": booking.commission_percent_snapshot,
                "commission_amount": booking.commission_amount,
                "driver_payout_amount": booking.driver_payout_amount,
                "applied_adjustment_amount": selected_adjustment_total,
                "net_transfer_amount": net_transfer_amount,
                "booking_transfer_status": booking.transfer_status,
                "transfer_row_status": None,
                "razorpay_transfer_id": None,
                "transfer_processed_at": None,
                "applied_adjustments": [
                    self._serialize_payout_adjustment_application(application)
                    for application in applications
                ],
            }

        transfer = booking.transfer
        if transfer is None:
            transfer = BookingTransfer(
                booking_id=booking.id,
                driver_user_id=booking.scheduled_trip.driver_user_id,
                source_booking_payment_id=source_payment.id,
                linked_account_id=resolved_linked_account_id,
                amount=net_transfer_amount,
                status=BookingTransferStatus.CREATED,
            )
            self.db.add(transfer)
        else:
            transfer.driver_user_id = booking.scheduled_trip.driver_user_id
            transfer.source_booking_payment_id = source_payment.id
            transfer.linked_account_id = resolved_linked_account_id
            transfer.amount = net_transfer_amount
            transfer.status = BookingTransferStatus.CREATED
            transfer.failure_reason = None
            transfer.razorpay_transfer_id = None
            transfer.processed_at = None
            self.db.add(transfer)

        # Keep current-session ORM state coherent.
        booking.transfer = transfer
        await self.db.flush()

        booking.withheld_at = None
        self.db.add(booking)

        # Persist local readiness before provider I/O.
        await self.db.commit()

        try:
            provider_response = await self._create_transfer_from_payment(
                razorpay_payment_id=source_payment.razorpay_payment_id,  # type: ignore[arg-type]
                linked_account_id=resolved_linked_account_id,
                amount_subunits=self._to_subunits(net_transfer_amount),
                booking=booking,
            )
        except HTTPException as exc:
            failure_reason = None
            if isinstance(exc.detail, dict):
                provider_error = exc.detail.get("provider_response")
                if isinstance(provider_error, dict):
                    nested_error = provider_error.get("error")
                    if isinstance(nested_error, dict):
                        failure_reason = (
                            nested_error.get("description")
                            or nested_error.get("reason")
                        )
                if not failure_reason:
                    failure_reason = exc.detail.get("message")

            if not failure_reason:
                failure_reason = str(exc.detail)

            transfer.status = BookingTransferStatus.FAILED
            transfer.failure_reason = str(failure_reason)

            booking.transfer = transfer
            booking.transfer_status = TransferStatus.FAILED

            self.db.add(transfer)
            self.db.add(booking)
            await self.db.commit()
            raise

        items = provider_response.get("items") or []
        if not items:
            transfer.status = BookingTransferStatus.FAILED
            transfer.failure_reason = "Provider response did not contain transfer items."

            booking.transfer = transfer
            booking.transfer_status = TransferStatus.FAILED

            self.db.add(transfer)
            self.db.add(booking)
            await self.db.commit()

            raise HTTPException(
                status_code=502,
                detail={
                    "error": "invalid_route_transfer_response",
                    "message": "Provider response did not contain transfer items.",
                },
            )

        provider_transfer = items[0]
        provider_transfer_id = provider_transfer.get("id")
        provider_transfer_status = provider_transfer.get("status")
        provider_error = provider_transfer.get("error")

        mapped_transfer_status, mapped_booking_status = self._map_provider_transfer_status(
            str(provider_transfer_status or "")
        )

        provider_error_text = None
        if provider_error:
            if isinstance(provider_error, dict):
                provider_error_text = (
                    provider_error.get("description")
                    or provider_error.get("reason")
                    or str(provider_error)
                )
            else:
                provider_error_text = str(provider_error)

        transfer.razorpay_transfer_id = provider_transfer_id
        transfer.status = mapped_transfer_status
        transfer.failure_reason = provider_error_text

        booking.transfer = transfer
        booking.transfer_status = mapped_booking_status

        if mapped_transfer_status == BookingTransferStatus.PROCESSED:
            transfer.processed_at = utcnow()
            booking.transfer_status = TransferStatus.TRANSFERRED
            booking.transfer_processed_at = transfer.processed_at

        created_applications: list[PayoutAdjustmentApplication] = []
        if normalized_allocations and mapped_transfer_status == BookingTransferStatus.PROCESSED:
            created_applications = await self._create_adjustment_applications(
                booking=booking,
                booking_transfer_id=transfer.id,
                applied_by_admin_id=applied_by_admin_id,  # type: ignore[arg-type]
                normalized_allocations=normalized_allocations,
            )

        self.db.add(transfer)
        self.db.add(booking)
        await self.db.commit()

        if mapped_transfer_status == BookingTransferStatus.PROCESSED:
            await self._notify_user(
                user_id=booking.scheduled_trip.driver_user_id,
                title="Payout processed",
                message="A booking payout has been processed to your linked account.",
                data=self._build_transfer_notification_data(booking),
            )

        return {
            "message": "Transfer trigger executed.",
            "booking_id": booking.id,
            "driver_user_id": booking.scheduled_trip.driver_user_id,
            "source_booking_payment_id": source_payment.id,
            "source_razorpay_payment_id": source_payment.razorpay_payment_id,
            "linked_account_id": resolved_linked_account_id,
            "linked_account_source": (
                "override"
                if (linked_account_id or "").strip()
                else "driver_payout_details"
            ),
            "payout_details_found": payout_details is not None,
            "commission_percent_snapshot": booking.commission_percent_snapshot,
            "commission_amount": booking.commission_amount,
            "driver_payout_amount": booking.driver_payout_amount,
            "applied_adjustment_amount": selected_adjustment_total,
            "net_transfer_amount": net_transfer_amount,
            "booking_transfer_status": booking.transfer_status,
            "transfer_row_status": booking.transfer.status,
            "razorpay_transfer_id": booking.transfer.razorpay_transfer_id,
            "transfer_processed_at": booking.transfer.processed_at,
            "applied_adjustments": [
                self._serialize_payout_adjustment_application(application)
                for application in created_applications
            ],
        }
    
    async def create_linked_account(
        self,
        *,
        email: str,
        phone: str,
        full_name: str,
        street1: str,
        street2: str | None = None,
        city: str = "Kolkata",
        state: str = "WEST BENGAL",
        postal_code: str = "700156",
        country: str = "IN",
    ) -> dict[str, Any]:
        cleaned_email = (email or "").strip()
        cleaned_phone = (phone or "").strip()
        cleaned_full_name = (full_name or "").strip()
        cleaned_street1 = (street1 or "").strip()
        cleaned_street2 = (street2 or "").strip()
        cleaned_city = (city or "").strip()
        cleaned_state = (state or "").strip()
        cleaned_postal_code = (postal_code or "").strip()
        cleaned_country = (country or "").strip().upper()

        if not cleaned_email:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_driver_email",
                    "message": "Driver email is required to create a linked account.",
                },
            )

        if not cleaned_phone:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_driver_phone",
                    "message": "Driver phone is required to create a linked account.",
                },
            )

        if not cleaned_full_name:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_driver_full_name",
                    "message": "Driver full name is required to create a linked account.",
                },
            )

        if not cleaned_street1:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_registered_street1",
                    "message": "Registered address street line 1 is required.",
                },
            )

        if not cleaned_city:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_registered_city",
                    "message": "Registered city is required.",
                },
            )

        if not cleaned_state:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_registered_state",
                    "message": "Registered state is required.",
                },
            )

        if not cleaned_postal_code:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_registered_postal_code",
                    "message": "Registered postal code is required.",
                },
            )

        if not cleaned_country:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_registered_country",
                    "message": "Registered country is required.",
                },
            )

        api_root = self._get_razorpay_api_root()

        payload = {
            "type": "route",
            "email": cleaned_email,
            "phone": cleaned_phone,
            "legal_business_name": cleaned_full_name,
            "business_type": "proprietorship",
            "contact_name": cleaned_full_name,
            "profile": {
                "addresses": {
                    "registered": {
                        "street1": cleaned_street1,
                        "street2": cleaned_street2 or None,
                        "city": cleaned_city,
                        "state": cleaned_state,
                        "postal_code": cleaned_postal_code,
                        "country": cleaned_country,
                    }
                },
                "category": "transport",
                "subcategory": "bus",
            },
        }

        return await self._razorpay_request(
            method="POST",
            path=f"{api_root}/v2/accounts",
            json_payload=payload,
        )
    
#     async def create_linked_account(
#     self,
#     *,
#     email: str,
#     phone: str,
#     full_name: str,
#     city: str = "Kolkata",
#     state: str = "WEST BENGAL",
#     country: str = "india",
# ) -> dict[str, Any]:
#         cleaned_email = (email or "").strip()
#         cleaned_phone = (phone or "").strip()
#         cleaned_full_name = (full_name or "").strip()

#         if not cleaned_email:
#             raise HTTPException(
#                 status_code=400,
#                 detail={
#                     "error": "missing_driver_email",
#                     "message": "Driver email is required to create a linked account.",
#                 },
#             )

#         if not cleaned_phone:
#             raise HTTPException(
#                 status_code=400,
#                 detail={
#                     "error": "missing_driver_phone",
#                     "message": "Driver phone is required to create a linked account.",
#                 },
#             )

#         if not cleaned_full_name:
#             raise HTTPException(
#                 status_code=400,
#                 detail={
#                     "error": "missing_driver_full_name",
#                     "message": "Driver full name is required to create a linked account.",
#                 },
#             )

#         api_root = self._get_razorpay_api_root()

#         payload = {
#             "type": "route",
#             "email": cleaned_email,
#             "phone": cleaned_phone,
#             "legal_business_name": cleaned_full_name,
#             "business_type": "individual",
#             "contact_name": cleaned_full_name,
#             "profile": {
#                 "addresses": {
#                     "registered": {
#                         "street1": "Mani Casadona",
#                         "street2": "Newtown Action Area II",
#                         "city": city,
#                         "state": state,
#                         "postal_code":"700156",
#                         "country": country,
#                     }
#                 },
#                 "category":"transport",
#                 "subcategory":"bus"
#             },
#         }

#         return await self._razorpay_request(
#             method="POST",
#             path=f"{api_root}/v2/accounts",
#             json_payload=payload,
#         )

    async def fetch_linked_account(
    self,
    linked_account_id: str,
) -> dict[str, Any]:
        cleaned_linked_account_id = (linked_account_id or "").strip()
        if not cleaned_linked_account_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_linked_account_id",
                    "message": "Linked account id is required.",
                },
            )

        api_root = self._get_razorpay_api_root()

        return await self._razorpay_request(
            method="GET",
            path=f"{api_root}/v2/accounts/{cleaned_linked_account_id}",
        )
    
    async def create_linked_account_stakeholder(
        self,
        *,
        linked_account_id: str,
        full_name: str,
        email: str,
        phone: str,
        pan_number: str,
        residential_street_line_1: str,
        residential_street_line_2: str | None = None,
        residential_city: str,
        residential_state: str,
        residential_postal_code: str,
        residential_country: str = "IN",
    ) -> dict[str, Any]:
        cleaned_linked_account_id = (linked_account_id or "").strip()
        cleaned_full_name = (full_name or "").strip()
        cleaned_email = (email or "").strip()
        cleaned_phone = (phone or "").strip()
        cleaned_pan_number = (pan_number or "").strip().upper()
        cleaned_street_line_1 = (residential_street_line_1 or "").strip()
        cleaned_street_line_2 = (residential_street_line_2 or "").strip()
        cleaned_city = (residential_city or "").strip()
        cleaned_state = (residential_state or "").strip()
        cleaned_postal_code = (residential_postal_code or "").strip()
        cleaned_country = (residential_country or "").strip().upper()

        if not cleaned_linked_account_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_linked_account_id",
                    "message": "Linked account id is required to create stakeholder.",
                },
            )

        if not cleaned_full_name:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_driver_full_name",
                    "message": "Driver full name is required to create stakeholder.",
                },
            )

        if not cleaned_email:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_driver_email",
                    "message": "Driver email is required to create stakeholder.",
                },
            )

        if not cleaned_phone:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_driver_phone",
                    "message": "Driver phone is required to create stakeholder.",
                },
            )

        if not cleaned_pan_number:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_driver_pan",
                    "message": "Driver PAN number is required to create stakeholder.",
                },
            )

        if not cleaned_street_line_1:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_residential_street_line_1",
                    "message": "Residential address street line 1 is required.",
                },
            )

        if not cleaned_city:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_residential_city",
                    "message": "Residential city is required.",
                },
            )

        if not cleaned_state:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_residential_state",
                    "message": "Residential state is required.",
                },
            )

        if not cleaned_postal_code:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_residential_postal_code",
                    "message": "Residential postal code is required.",
                },
            )

        api_root = self._get_razorpay_api_root()

        payload = {
            "name": cleaned_full_name,
            "email": cleaned_email,
            "percentage_ownership": 100,
            "relationship": {
                "executive": True,
            },
            "phone": {
                "primary": cleaned_phone,
            },
            "addresses": {
                "residential": {
                    "street": cleaned_street_line_1,
                    "city": cleaned_city,
                    "state": cleaned_state,
                    "postal_code": cleaned_postal_code,
                    "country": cleaned_country,
                }
            },
            "kyc": {
                "pan": cleaned_pan_number,
            },
        }

        return await self._razorpay_request(
            method="POST",
            path=f"{api_root}/v2/accounts/{cleaned_linked_account_id}/stakeholders",
            json_payload=payload,
        )
    
    async def request_route_product(
        self,
        *,
        linked_account_id: str,
    ) -> dict[str, Any]:
        cleaned_linked_account_id = (linked_account_id or "").strip()
        if not cleaned_linked_account_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_linked_account_id",
                    "message": "Linked account id is required to request Route product.",
                },
            )

        api_root = self._get_razorpay_api_root()

        return await self._razorpay_request(
            method="POST",
            path=f"{api_root}/v2/accounts/{cleaned_linked_account_id}/products",
            json_payload={
                "product_name": "route",
                "tnc_accepted": True,
            },
        )

    async def update_route_product(
        self,
        *,
        linked_account_id: str,
        product_id: str,
        beneficiary_name: str,
        account_number: str,
        ifsc_code: str,
    ) -> dict[str, Any]:
        cleaned_linked_account_id = (linked_account_id or "").strip()
        cleaned_product_id = (product_id or "").strip()
        cleaned_beneficiary_name = (beneficiary_name or "").strip()
        cleaned_account_number = (account_number or "").strip()
        cleaned_ifsc_code = (ifsc_code or "").strip().upper()

        if not cleaned_linked_account_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_linked_account_id",
                    "message": "Linked account id is required to update Route product.",
                },
            )

        if not cleaned_product_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_route_product_id",
                    "message": "Route product id is required.",
                },
            )

        if not cleaned_beneficiary_name:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_beneficiary_name",
                    "message": "Beneficiary name is required.",
                },
            )

        if not cleaned_account_number:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_bank_account_number",
                    "message": "Bank account number is required.",
                },
            )

        if not cleaned_ifsc_code:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_ifsc_code",
                    "message": "IFSC code is required.",
                },
            )

        api_root = self._get_razorpay_api_root()

        return await self._razorpay_request(
            method="PATCH",
            path=f"{api_root}/v2/accounts/{cleaned_linked_account_id}/products/{cleaned_product_id}",
            json_payload={
                "settlements": {
                    "account_number": cleaned_account_number,
                    "ifsc_code": cleaned_ifsc_code,
                    "beneficiary_name": cleaned_beneficiary_name,
                },
                "tnc_accepted": True,
            },
        )

    async def fetch_route_product(
        self,
        *,
        linked_account_id: str,
        product_id: str,
    ) -> dict[str, Any]:
        cleaned_linked_account_id = (linked_account_id or "").strip()
        cleaned_product_id = (product_id or "").strip()

        if not cleaned_linked_account_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_linked_account_id",
                    "message": "Linked account id is required to fetch Route product.",
                },
            )

        if not cleaned_product_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_route_product_id",
                    "message": "Route product id is required.",
                },
            )

        api_root = self._get_razorpay_api_root()

        return await self._razorpay_request(
            method="GET",
            path=f"{api_root}/v2/accounts/{cleaned_linked_account_id}/products/{cleaned_product_id}",
        )
    
    async def onboard_route_linked_account(
        self,
        *,
        linked_account_id: str | None,
        stakeholder_id: str | None,
        route_product_id: str | None,
        email: str,
        phone: str,
        full_name: str,
        pan_number: str,
        registered_street1: str,
        registered_street2: str | None,
        registered_city: str,
        registered_state: str,
        registered_postal_code: str,
        registered_country: str,
        residential_street_line_1: str,
        residential_street_line_2: str | None,
        residential_city: str,
        residential_state: str,
        residential_postal_code: str,
        residential_country: str,
        beneficiary_name: str,
        account_number: str,
        ifsc_code: str,
    ) -> dict[str, Any]:
        cleaned_linked_account_id = (linked_account_id or "").strip()
        cleaned_stakeholder_id = (stakeholder_id or "").strip()
        cleaned_route_product_id = (route_product_id or "").strip()

        if cleaned_linked_account_id:
            account = await self.fetch_linked_account(cleaned_linked_account_id)
        else:
            account = await self.create_linked_account(
                email=email,
                phone=phone,
                full_name=full_name,
                street1=registered_street1,
                street2=registered_street2,
                city=registered_city,
                state=registered_state,
                postal_code=registered_postal_code,
                country=registered_country,
            )

        resolved_linked_account_id = str(account.get("id") or "").strip()
        if not resolved_linked_account_id:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "missing_provider_linked_account_id",
                    "message": "Provider did not return linked account id.",
                },
            )

        stakeholder = None
        resolved_stakeholder_id = cleaned_stakeholder_id

        if not resolved_stakeholder_id:
            stakeholder = await self.create_linked_account_stakeholder(
                linked_account_id=resolved_linked_account_id,
                full_name=full_name,
                email=email,
                phone=phone,
                pan_number=pan_number,
                residential_street_line_1=residential_street_line_1,
                residential_street_line_2=residential_street_line_2,
                residential_city=residential_city,
                residential_state=residential_state,
                residential_postal_code=residential_postal_code,
                residential_country=residential_country,
            )
            resolved_stakeholder_id = str(stakeholder.get("id") or "").strip()

        if cleaned_route_product_id:
            product = await self.fetch_route_product(
                linked_account_id=resolved_linked_account_id,
                product_id=cleaned_route_product_id,
            )
        else:
            product = await self.request_route_product(
                linked_account_id=resolved_linked_account_id,
            )

        resolved_route_product_id = str(product.get("id") or "").strip()
        if not resolved_route_product_id:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "missing_provider_route_product_id",
                    "message": "Provider did not return Route product id.",
                },
            )

        product = await self.update_route_product(
            linked_account_id=resolved_linked_account_id,
            product_id=resolved_route_product_id,
            beneficiary_name=beneficiary_name,
            account_number=account_number,
            ifsc_code=ifsc_code,
        )

        account = await self.fetch_linked_account(resolved_linked_account_id)
        product = await self.fetch_route_product(
            linked_account_id=resolved_linked_account_id,
            product_id=resolved_route_product_id,
        )

        return {
            "linked_account_id": resolved_linked_account_id,
            "stakeholder_id": resolved_stakeholder_id or None,
            "route_product_id": resolved_route_product_id,
            "linked_account_status": self._map_provider_linked_account_status(
                account.get("status")
            ),
            "route_product_status": self._map_provider_route_product_status(
                product.get("activation_status") or product.get("status")
            ),
            "route_product_requirements": product.get("requirements"),
            "provider_account": account,
            "provider_stakeholder": stakeholder,
            "provider_product": product,
        }
  