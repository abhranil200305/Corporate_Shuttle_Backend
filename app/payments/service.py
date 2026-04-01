from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import select
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
    PlatformSettings,
    TransferStatus,
    TripBooking,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RoutePayoutService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

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
    ) -> dict[str, Any]:
        key_id = self._get_razorpay_key_id()
        key_secret = self._get_razorpay_key_secret()
        base_url = self._get_razorpay_base_url()
        url = f"{base_url}{path}"

        async with httpx.AsyncClient(auth=(key_id, key_secret), timeout=20.0) as client:
            response = await client.request(method=method, url=url, json=json_payload)

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

    # ---------------------------------------------------------
    # public trigger
    # ---------------------------------------------------------
    async def trigger_transfer_for_booking(
        self,
        booking_id: str,
        *,
        linked_account_id: str | None = None,
        require_completed: bool = True,
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

        transfer = booking.transfer
        if transfer is None:
            transfer = BookingTransfer(
                booking_id=booking.id,
                driver_user_id=booking.scheduled_trip.driver_user_id,
                source_booking_payment_id=source_payment.id,
                linked_account_id=resolved_linked_account_id,
                amount=booking.driver_payout_amount,
                status=BookingTransferStatus.CREATED,
            )
            self.db.add(transfer)
            await self.db.flush()
        else:
            transfer.driver_user_id = booking.scheduled_trip.driver_user_id
            transfer.source_booking_payment_id = source_payment.id
            transfer.linked_account_id = resolved_linked_account_id
            transfer.amount = booking.driver_payout_amount
            transfer.status = BookingTransferStatus.CREATED
            transfer.failure_reason = None
            self.db.add(transfer)
            await self.db.flush()

        # Persist the fact that this booking is now commercially ready before external I/O.
        await self.db.commit()

        try:
            provider_response = await self._create_transfer_from_payment(
                razorpay_payment_id=source_payment.razorpay_payment_id,  # type: ignore[arg-type]
                linked_account_id=resolved_linked_account_id,
                amount_subunits=self._to_subunits(booking.driver_payout_amount),
                booking=booking,
            )
        except HTTPException as exc:
            booking = await self._get_booking_obj(booking_id)
            if booking.transfer is not None:
                booking.transfer.status = BookingTransferStatus.FAILED
                booking.transfer.failure_reason = (
                    exc.detail.get("message")
                    if isinstance(exc.detail, dict)
                    else str(exc.detail)
                )
                self.db.add(booking.transfer)

            booking.transfer_status = TransferStatus.FAILED
            self.db.add(booking)
            await self.db.commit()
            raise

        items = provider_response.get("items") or []
        if not items:
            booking = await self._get_booking_obj(booking_id)
            if booking.transfer is not None:
                booking.transfer.status = BookingTransferStatus.FAILED
                booking.transfer.failure_reason = "Provider response did not contain transfer items."
                self.db.add(booking.transfer)

            booking.transfer_status = TransferStatus.FAILED
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

        booking = await self._get_booking_obj(booking_id)
        if booking.transfer is None:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "transfer_row_missing_after_provider_call",
                    "message": "Transfer row is missing after provider call.",
                },
            )

        mapped_transfer_status, mapped_booking_status = self._map_provider_transfer_status(
            str(provider_transfer_status or "")
        )

        booking.transfer.razorpay_transfer_id = provider_transfer_id
        booking.transfer.status = mapped_transfer_status
        booking.transfer.failure_reason = None if not provider_error else str(provider_error)

        booking.transfer_status = mapped_booking_status

        if mapped_transfer_status == BookingTransferStatus.PROCESSED:
            booking.transfer.processed_at = utcnow()
            booking.transfer_processed_at = booking.transfer.processed_at

        self.db.add(booking.transfer)
        self.db.add(booking)
        await self.db.commit()

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
            "booking_transfer_status": booking.transfer_status,
            "transfer_row_status": booking.transfer.status,
            "razorpay_transfer_id": booking.transfer.razorpay_transfer_id,
            "transfer_processed_at": booking.transfer.processed_at,
        }