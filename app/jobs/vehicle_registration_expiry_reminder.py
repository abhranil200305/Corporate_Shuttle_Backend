from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import AsyncSessionLocal, engine
from app.db.schema import UserNotification, Vehicle
from app.notifications.hub import WSHub
from app.notifications.service import NotificationService

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_timezone() -> ZoneInfo:
    raw = os.getenv("VEHICLE_REGISTRATION_REMINDER_TIMEZONE", "Asia/Kolkata").strip()
    try:
        return ZoneInfo(raw or "Asia/Kolkata")
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Kolkata")


def _get_warning_days() -> int:
    raw = os.getenv("VEHICLE_REGISTRATION_REMINDER_WARNING_DAYS", "15").strip()
    try:
        value = int(raw)
    except ValueError:
        return 15
    return max(1, value)


def _get_batch_size() -> int:
    raw = os.getenv("VEHICLE_REGISTRATION_REMINDER_BATCH_SIZE", "500").strip()
    try:
        value = int(raw)
    except ValueError:
        return 500
    return max(1, value)


def _get_lock_key() -> int:
    raw = os.getenv("VEHICLE_REGISTRATION_REMINDER_LOCK_KEY", "82024005").strip()
    try:
        return int(raw)
    except ValueError:
        return 82024005


def _serialize_data(data: dict[str, Any]) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def _get_schedule_slots() -> list[tuple[int, int, str]]:
    return [
        (9, 30, "0930"),
        (18, 30, "1830"),
    ]


def _get_next_run_slot(
    *,
    now_local: datetime,
) -> tuple[datetime, str]:
    tz = now_local.tzinfo
    assert tz is not None

    candidates: list[tuple[datetime, str]] = []

    for hour, minute, slot_key in _get_schedule_slots():
        candidate = now_local.replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )
        if candidate > now_local:
            candidates.append((candidate, slot_key))

    if candidates:
        candidates.sort(key=lambda item: item[0])
        return candidates[0]

    next_day = now_local + timedelta(days=1)
    first_hour, first_minute, first_slot_key = _get_schedule_slots()[0]
    next_candidate = next_day.replace(
        hour=first_hour,
        minute=first_minute,
        second=0,
        microsecond=0,
    )
    return next_candidate, first_slot_key


def _ensure_tzaware(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _build_notification_payload(
    vehicle: Vehicle,
    *,
    scheduled_local_now: datetime,
    slot_key: str,
) -> tuple[str, str, dict[str, Any], str] | None:
    registration_valid_till = vehicle.registration_valid_till
    if registration_valid_till is None:
        return None

    warning_days = _get_warning_days()
    registration_local = _ensure_tzaware(registration_valid_till).astimezone(
        scheduled_local_now.tzinfo
    )

    expired = registration_local <= scheduled_local_now
    days_until_expiry = (registration_local.date() - scheduled_local_now.date()).days

    if not expired and days_until_expiry > warning_days:
        return None

    registration_date_text = registration_local.strftime("%d %b %Y")

    if expired:
        title = "Vehicle registration expired"
        message = (
            f"Your vehicle registration expired on {registration_date_text}. "
            f"This vehicle can be terminated anytime unless the registration is renewed immediately."
        )
        reminder_kind = "expired"
    else:
        if days_until_expiry <= 0:
            title = "Vehicle registration expires today"
            message = (
                f"Your vehicle registration expires today ({registration_date_text}). "
                f"Renew it immediately to avoid service disruption."
            )
        elif days_until_expiry == 1:
            title = "Vehicle registration expires tomorrow"
            message = (
                f"Your vehicle registration expires tomorrow ({registration_date_text}). "
                f"Renew it before expiry to avoid service disruption."
            )
        else:
            title = f"Vehicle registration expires in {days_until_expiry} days"
            message = (
                f"Your vehicle registration will expire on {registration_date_text}, "
                f"which is in {days_until_expiry} days. Renew it before expiry to avoid service disruption."
            )

        reminder_kind = "warning"

    data = {
        "vehicle_id": vehicle.id,
        "driver_user_id": vehicle.driver_user_id,
        "registration_valid_till": registration_local.isoformat(),
        "reminder_kind": reminder_kind,
        "days_until_expiry": days_until_expiry,
        "reminder_date": scheduled_local_now.date().isoformat(),
        "reminder_slot": slot_key,
        "refresh": ["driver_trips"],
    }

    return title, message, data, _serialize_data(data)


async def _notification_already_sent(
    db: AsyncSession,
    *,
    user_id: str,
    title: str,
    message: str,
    data_json: str,
) -> bool:
    stmt = (
        select(UserNotification.id)
        .where(
            UserNotification.user_id == user_id,
            UserNotification.title == title,
            UserNotification.message == message,
            UserNotification.data_json == data_json,
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


async def _fetch_candidate_vehicle_ids(
    db: AsyncSession,
    *,
    upper_bound_utc: datetime,
    limit: int,
) -> list[str]:
    stmt = (
        select(Vehicle.id)
        .where(
            Vehicle.is_active.is_(True),
            Vehicle.registration_valid_till.is_not(None),
            Vehicle.registration_valid_till <= upper_bound_utc,
        )
        .order_by(
            Vehicle.registration_valid_till.asc(),
            Vehicle.created_at.asc(),
        )
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _process_vehicle_id(
    db: AsyncSession,
    *,
    vehicle_id: str,
    scheduled_local_now: datetime,
    slot_key: str,
    ws_hub: WSHub | None,
) -> str:
    stmt = (
        select(Vehicle)
        .where(
            Vehicle.id == vehicle_id,
            Vehicle.is_active.is_(True),
            Vehicle.registration_valid_till.is_not(None),
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    vehicle = result.scalar_one_or_none()

    if vehicle is None:
        await db.rollback()
        return "skip_missing_or_ineligible"

    built = _build_notification_payload(
        vehicle,
        scheduled_local_now=scheduled_local_now,
        slot_key=slot_key,
    )
    if built is None:
        await db.rollback()
        return "skip_not_due"

    title, message, data, data_json = built

    already_sent = await _notification_already_sent(
        db,
        user_id=vehicle.driver_user_id,
        title=title,
        message=message,
        data_json=data_json,
    )
    if already_sent:
        await db.rollback()
        return "skip_already_sent"

    notification_service = NotificationService(db=db, ws_hub=ws_hub)
    await notification_service.notify_user(
        user_id=vehicle.driver_user_id,
        title=title,
        message=message,
        data=data,
    )

    return f"sent_{data['reminder_kind']}"


async def send_vehicle_registration_expiry_reminders_once(
    *,
    ws_hub: WSHub | None = None,
    scheduled_local_now: datetime | None = None,
    slot_key: str | None = None,
) -> None:
    tz = _get_timezone()
    effective_local_now = scheduled_local_now or utcnow().astimezone(tz)

    if effective_local_now.tzinfo is None:
        effective_local_now = effective_local_now.replace(tzinfo=tz)

    effective_slot_key = slot_key
    if not effective_slot_key:
        effective_slot_key = effective_local_now.strftime("%H%M")

    lock_key = _get_lock_key()
    batch_size = _get_batch_size()
    warning_days = _get_warning_days()
    upper_bound_utc = effective_local_now.astimezone(timezone.utc) + timedelta(days=warning_days + 1)

    async with engine.connect() as conn:
        acquired = bool(
            (
                await conn.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": lock_key},
                )
            ).scalar()
        )

        if not acquired:
            logger.info(
                "vehicle_registration_expiry_reminder skipped: advisory lock not acquired"
            )
            return

        try:
            async with AsyncSessionLocal(bind=conn) as db:
                total_processed = 0

                while True:
                    vehicle_ids = await _fetch_candidate_vehicle_ids(
                        db,
                        upper_bound_utc=upper_bound_utc,
                        limit=batch_size,
                    )
                    await db.rollback()

                    if not vehicle_ids:
                        break

                    for vehicle_id in vehicle_ids:
                        try:
                            outcome = await _process_vehicle_id(
                                db,
                                vehicle_id=vehicle_id,
                                scheduled_local_now=effective_local_now,
                                slot_key=effective_slot_key,
                                ws_hub=ws_hub,
                            )
                            total_processed += 1
                            logger.info(
                                "vehicle_registration_expiry_reminder vehicle_id=%s slot=%s outcome=%s",
                                vehicle_id,
                                effective_slot_key,
                                outcome,
                            )
                        except Exception:
                            await db.rollback()
                            logger.exception(
                                "vehicle_registration_expiry_reminder vehicle_id=%s slot=%s outcome=error",
                                vehicle_id,
                                effective_slot_key,
                            )

                    if len(vehicle_ids) < batch_size:
                        break

                logger.info(
                    "vehicle_registration_expiry_reminder done slot=%s processed=%s",
                    effective_slot_key,
                    total_processed,
                )
        finally:
            try:
                await conn.rollback()
            except Exception:
                pass

            try:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": lock_key},
                )
                await conn.commit()
            except Exception:
                logger.exception(
                    "vehicle_registration_expiry_reminder advisory unlock failed"
                )


async def vehicle_registration_expiry_reminder_loop(
    ws_hub: WSHub | None = None,
) -> None:
    tz = _get_timezone()
    logger.info(
        "vehicle_registration_expiry_reminder loop started timezone=%s",
        str(tz),
    )

    try:
        while True:
            now_local = utcnow().astimezone(tz)
            next_run_local, slot_key = _get_next_run_slot(now_local=now_local)
            delay_seconds = max(
                0.0,
                (next_run_local.astimezone(timezone.utc) - utcnow()).total_seconds(),
            )

            logger.info(
                "vehicle_registration_expiry_reminder next_run_local=%s slot=%s sleep_seconds=%.3f",
                next_run_local.isoformat(),
                slot_key,
                delay_seconds,
            )

            await asyncio.sleep(delay_seconds)

            try:
                await send_vehicle_registration_expiry_reminders_once(
                    ws_hub=ws_hub,
                    scheduled_local_now=next_run_local,
                    slot_key=slot_key,
                )
            except Exception:
                logger.exception("vehicle_registration_expiry_reminder tick failed")

    except asyncio.CancelledError:
        logger.info("vehicle_registration_expiry_reminder loop cancelled")
        raise