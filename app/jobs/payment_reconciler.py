from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app.db.database import AsyncSessionLocal, engine
from app.db.schema import BookingStatus, TripBooking
from app.passenger.service import PassengerService

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_batch_size() -> int:
    raw = os.getenv("PAYMENT_RECONCILE_BATCH_SIZE", "100").strip()
    try:
        value = int(raw)
    except ValueError:
        return 100
    return max(1, value)


def _get_interval_seconds() -> int:
    raw = os.getenv("PAYMENT_RECONCILE_INTERVAL_SECONDS", "60").strip()
    try:
        value = int(raw)
    except ValueError:
        return 60
    return max(5, value)


def _get_lock_key() -> int:
    raw = os.getenv("PAYMENT_RECONCILE_LOCK_KEY", "82024001").strip()
    try:
        return int(raw)
    except ValueError:
        return 82024001


def _seconds_until_next_minute() -> float:
    now = utcnow()
    elapsed = now.second + (now.microsecond / 1_000_000)
    remaining = 60 - elapsed
    return remaining if remaining > 0 else 60.0


async def _fetch_pending_booking_ids(limit: int) -> list[str]:
    async with AsyncSessionLocal() as db:
        stmt = (
            select(TripBooking.id)
            .where(TripBooking.booking_status == BookingStatus.PENDING_PAYMENT)
            .order_by(TripBooking.created_at.asc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())


async def _process_booking_id(booking_id: str) -> str:
    async with AsyncSessionLocal() as db:
        service = PassengerService(db)

        stmt = (
            select(TripBooking)
            .where(
                TripBooking.id == booking_id,
                TripBooking.booking_status == BookingStatus.PENDING_PAYMENT,
            )
            .options(selectinload(TripBooking.payments))
            .with_for_update(skip_locked=True)
        )
        result = await db.execute(stmt)
        booking = result.scalars().unique().first()

        if booking is None:
            await db.rollback()
            return "skip_missing_or_locked"

        try:
            outcome = await service.reconcile_pending_booking_payment(booking)
            await db.commit()
            return outcome
        except Exception:
            await db.rollback()
            raise


async def reconcile_pending_booking_payments_once() -> None:
    lock_key = _get_lock_key()
    batch_size = _get_batch_size()

    async with engine.begin() as conn:
        acquired = bool(
            (
                await conn.execute(
                    text("SELECT pg_try_advisory_xact_lock(:key)"),
                    {"key": lock_key},
                )
            ).scalar()
        )

        if not acquired:
            logger.info("payment_reconcile skipped: advisory lock not acquired")
            return

        total_processed = 0

        while True:
            booking_ids = await _fetch_pending_booking_ids(batch_size)
            if not booking_ids:
                break

            for booking_id in booking_ids:
                try:
                    outcome = await _process_booking_id(booking_id)
                    total_processed += 1
                    logger.info(
                        "payment_reconcile booking_id=%s outcome=%s",
                        booking_id,
                        outcome,
                    )
                except Exception:
                    logger.exception(
                        "payment_reconcile booking_id=%s outcome=error",
                        booking_id,
                    )

            if len(booking_ids) < batch_size:
                break

        logger.info("payment_reconcile done processed=%s", total_processed)


async def payment_reconcile_loop() -> None:
    logger.info("payment_reconcile loop started")
    try:
        await reconcile_pending_booking_payments_once()

        while True:
            await asyncio.sleep(_seconds_until_next_minute())
            try:
                await reconcile_pending_booking_payments_once()
            except Exception:
                logger.exception("payment_reconcile tick failed")
    except asyncio.CancelledError:
        logger.info("payment_reconcile loop cancelled")
        raise