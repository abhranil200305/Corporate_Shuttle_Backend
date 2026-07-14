from __future__ import annotations

import hashlib
import hmac
import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app.db.schema import (
    BookingPaymentStatus,
    BookingSessionStatus,
    UserRole,
)
from app.passenger.service import PassengerService


class BookingSessionPaymentReconciliationTests(
    unittest.IsolatedAsyncioTestCase
):
    def setUp(self) -> None:
        self.now = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)
        self.service = PassengerService(MagicMock())
        self.payment = SimpleNamespace(
            id="session-payment-1",
            booking_session_id="session-1",
            razorpay_order_id="order_1",
            razorpay_payment_id=None,
            razorpay_signature=None,
            amount=Decimal("500.00"),
            status=BookingPaymentStatus.CREATED,
            created_at=self.now,
        )
        self.booking = SimpleNamespace(id="booking-1")
        self.session = SimpleNamespace(
            id="session-1",
            status=BookingSessionStatus.PENDING_PAYMENT,
            payment_hold_expires_at=self.now + timedelta(minutes=5),
            bookings=[self.booking],
            payments=[self.payment],
        )

    async def test_captured_payment_confirms_active_session(self) -> None:
        self.service._fetch_razorpay_order_payments = AsyncMock(
            return_value=[
                {
                    "id": "pay_1",
                    "order_id": "order_1",
                    "amount": 50000,
                    "status": "captured",
                    "captured": True,
                }
            ]
        )
        self.service._mark_booking_session_paid_and_confirmed = AsyncMock()

        with patch("app.passenger.service.utcnow", return_value=self.now):
            outcome = (
                await self.service.reconcile_pending_booking_session_payment(
                    self.session
                )
            )

        self.assertEqual(outcome, "confirmed_from_captured_payment")
        self.service._mark_booking_session_paid_and_confirmed.assert_awaited_once_with(
            booking_session=self.session,
            payment=self.payment,
            bookings=[self.booking],
            razorpay_payment_id="pay_1",
        )

    async def test_captured_payment_after_hold_queues_expiry_refund(self) -> None:
        self.session.payment_hold_expires_at = self.now - timedelta(seconds=1)
        self.service._fetch_razorpay_order_payments = AsyncMock(
            return_value=[
                {
                    "id": "pay_late",
                    "order_id": "order_1",
                    "amount": 50000,
                    "status": "captured",
                }
            ]
        )
        self.service._mark_booking_session_paid_but_expired = AsyncMock()

        with patch("app.passenger.service.utcnow", return_value=self.now):
            outcome = (
                await self.service.reconcile_pending_booking_session_payment(
                    self.session
                )
            )

        self.assertEqual(outcome, "captured_after_hold_expiry")
        self.service._mark_booking_session_paid_but_expired.assert_awaited_once_with(
            booking_session=self.session,
            payment=self.payment,
            bookings=[self.booking],
            payments=[self.payment],
            razorpay_payment_id="pay_late",
        )

    async def test_failed_attempt_keeps_order_retryable_during_hold(self) -> None:
        self.service._fetch_razorpay_order_payments = AsyncMock(
            return_value=[
                {
                    "id": "pay_failed",
                    "order_id": "order_1",
                    "amount": 50000,
                    "status": "failed",
                }
            ]
        )

        with patch("app.passenger.service.utcnow", return_value=self.now):
            outcome = (
                await self.service.reconcile_pending_booking_session_payment(
                    self.session
                )
            )

        self.assertEqual(outcome, "pending_with_failed_payment")

    async def test_duplicate_late_capture_does_not_reopen_refunded_payment(
        self,
    ) -> None:
        self.payment.status = BookingPaymentStatus.REFUNDED
        self.payment.razorpay_payment_id = "pay_refunded"
        self.payment.refund_requested_at = self.now
        self.payment.refund_retry_after = None
        self.payment.refund_attempt_count = 1
        self.payment.refund_failure_reason = None
        self.service.db.flush = AsyncMock()
        self.service._expire_pending_booking_session = AsyncMock()
        self.service._ensure_booking_seat_refund_request = AsyncMock()

        await self.service._mark_booking_session_paid_but_expired(
            booking_session=self.session,
            payment=self.payment,
            bookings=[self.booking],
            payments=[self.payment],
            razorpay_payment_id="pay_refunded",
        )

        self.assertEqual(self.payment.status, BookingPaymentStatus.REFUNDED)
        self.assertIsNone(self.payment.refund_retry_after)


class BookingSessionPaymentRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_reuses_order_without_extending_hold(self) -> None:
        db = MagicMock()
        db.add = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        service = PassengerService(db)
        hold_expires_at = datetime(
            2026,
            7,
            14,
            10,
            5,
            tzinfo=timezone.utc,
        )
        payment = SimpleNamespace(
            razorpay_order_id="order_existing",
            razorpay_payment_id="pay_failed",
            razorpay_signature="failed_signature",
            amount=Decimal("500.00"),
            status=BookingPaymentStatus.FAILED,
            created_at=datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc),
        )
        booking = SimpleNamespace(id="booking-1")
        session = SimpleNamespace(
            id="session-1",
            owner_user_id="user-1",
            scheduled_trip_id="trip-1",
            route_id="route-1",
            status=BookingSessionStatus.PENDING_PAYMENT,
            payment_hold_expires_at=hold_expires_at,
            total_fare_amount=Decimal("500.00"),
            bookings=[booking],
            payments=[payment],
        )
        current_user = SimpleNamespace(
            id="user-1",
            role=UserRole.PASSENGER,
        )

        service._get_booking_session_for_update_or_404 = AsyncMock(
            return_value=session
        )
        service._list_booking_session_payments_for_update = AsyncMock(
            return_value=[payment]
        )
        service._list_booking_session_bookings_for_update = AsyncMock(
            return_value=[booking]
        )
        service.reconcile_pending_booking_session_payment = AsyncMock(
            return_value="pending_with_failed_payment"
        )
        service._get_booking_session_obj = AsyncMock(return_value=session)
        service._serialize_booking_session_with_refunds = AsyncMock(
            return_value={
                "id": session.id,
                "status": session.status.value,
                "payment_hold_expires_at": hold_expires_at,
            }
        )
        service._create_booking_session_razorpay_order = AsyncMock()

        result = await service.retry_booking_session_payment(
            current_user,
            session.id,
        )

        self.assertEqual(
            result["payment_order"]["razorpay_order_id"],
            "order_existing",
        )
        self.assertEqual(session.payment_hold_expires_at, hold_expires_at)
        self.assertEqual(payment.status, BookingPaymentStatus.CREATED)
        self.assertIsNone(payment.razorpay_payment_id)
        self.assertIsNone(payment.razorpay_signature)
        service._create_booking_session_razorpay_order.assert_not_awaited()


class ClosedBookingSessionPaymentReconciliationTests(
    unittest.IsolatedAsyncioTestCase
):
    def setUp(self) -> None:
        self.now = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)
        self.service = PassengerService(MagicMock())
        self.booking = SimpleNamespace(id="booking-1")
        self.payment = SimpleNamespace(
            razorpay_order_id="order_late",
            razorpay_payment_id="pay_failed",
            refund_requested_at=None,
            amount=Decimal("500.00"),
            status=BookingPaymentStatus.FAILED,
            created_at=self.now,
        )
        self.session = SimpleNamespace(
            id="session-closed",
            status=BookingSessionStatus.EXPIRED,
            bookings=[self.booking],
            payments=[self.payment],
        )

    async def test_late_capture_queues_existing_refund_pipeline(self) -> None:
        self.service._fetch_razorpay_order_payments = AsyncMock(
            return_value=[
                {
                    "id": "pay_captured_late",
                    "order_id": "order_late",
                    "amount": 50000,
                    "status": "captured",
                    "captured": True,
                }
            ]
        )
        self.service._mark_booking_session_paid_but_expired = AsyncMock()

        outcome = (
            await self.service.reconcile_closed_booking_session_payment(
                self.session
            )
        )

        self.assertEqual(outcome, "closed_captured_payment_refund_queued")
        self.service._mark_booking_session_paid_but_expired.assert_awaited_once_with(
            booking_session=self.session,
            payment=self.payment,
            bookings=[self.booking],
            payments=[self.payment],
            razorpay_payment_id="pay_captured_late",
        )

    async def test_failed_closed_order_remains_under_observation(self) -> None:
        self.service._fetch_razorpay_order_payments = AsyncMock(
            return_value=[
                {
                    "id": "pay_failed",
                    "order_id": "order_late",
                    "amount": 50000,
                    "status": "failed",
                }
            ]
        )
        self.service._mark_booking_session_paid_but_expired = AsyncMock()

        outcome = (
            await self.service.reconcile_closed_booking_session_payment(
                self.session
            )
        )

        self.assertEqual(outcome, "closed_with_failed")
        self.service._mark_booking_session_paid_but_expired.assert_not_awaited()


class RazorpayWebhookSignatureTests(unittest.TestCase):
    def test_valid_signature_is_accepted(self) -> None:
        body = b'{"event":"payment.captured"}'
        secret = "webhook-secret"
        signature = hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()

        with patch.dict(
            os.environ,
            {"RAZORPAY_WEBHOOK_SECRET": secret},
            clear=False,
        ):
            PassengerService._verify_razorpay_webhook_signature(
                body,
                signature,
            )

    def test_invalid_signature_is_rejected(self) -> None:
        with patch.dict(
            os.environ,
            {"RAZORPAY_WEBHOOK_SECRET": "webhook-secret"},
            clear=False,
        ):
            with self.assertRaises(HTTPException) as raised:
                PassengerService._verify_razorpay_webhook_signature(
                    b"{}",
                    "invalid",
                )

        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
