from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import AsyncSessionLocal
from app.db.schema import (
    User,
    UserNotification,
    UserRole,
    Vehicle,
    VehicleInspectionStatus,
)
from app.jobs.lease import (
    get_job_owner_id,
    release_job_lease,
    try_acquire_or_renew_job_lease,
)
from app.notifications.hub import WSHub
from app.notifications.service import NotificationService

logger = logging.getLogger(__name__)

_JOB_NAME = "vehicle_inspection_status_reminder"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_timezone() -> ZoneInfo:
    raw = os.getenv("VEHICLE_INSPECTION_REMINDER_TIMEZONE", "Asia/Kolkata").strip()
    try:
        return ZoneInfo(raw or "Asia/Kolkata")
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Kolkata")


def _get_batch_size() -> int:
    raw = os.getenv("VEHICLE_INSPECTION_REMINDER_BATCH_SIZE", "500").strip()
    try:
        value = int(raw)
    except ValueError:
        return 500
    return max(1, value)


def _get_lease_seconds() -> int:
    raw = os.getenv("VEHICLE_INSPECTION_REMINDER_LEASE_SECONDS", "1800").strip()
    try:
        value = int(raw)
    except ValueError:
        return 1800
    return max(60, value)


def _get_auto_pending_after_days() -> int:
    raw = os.getenv("VEHICLE_INSPECTION_AUTO_PENDING_AFTER_DAYS", "15").strip()
    try:
        value = int(raw)
    except ValueError:
        return 15
    return max(1, value)


def _serialize_data(data: dict[str, Any]) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def _get_schedule_slots() -> list[tuple[int, int, str]]:
    return [
        (9, 30, "0930"),
        (18, 30, "1830"),
    ]


def _get_next_run_slot(*, now_local: datetime) -> tuple[datetime, str]:
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


def _status_requires_daily_reminder(status: VehicleInspectionStatus | None) -> bool:
    return status in {
        VehicleInspectionStatus.PENDING,
        VehicleInspectionStatus.REJECTED,
    }


def _approved_has_expired(
    *,
    vehicle: Vehicle,
    now_utc: datetime,
) -> bool:
    if vehicle.inspection_status != VehicleInspectionStatus.APPROVED:
        return False

    if vehicle.inspection_reviewed_at is None:
        return False

    approved_at = _ensure_tzaware(vehicle.inspection_reviewed_at)
    cutoff = approved_at + timedelta(days=_get_auto_pending_after_days())
    return now_utc >= cutoff


def _build_notification_payload(
    vehicle: Vehicle,
    *,
    scheduled_local_now: datetime,
    slot_key: str,
    auto_switched_from_approved: bool,
) -> tuple[str, str, dict[str, Any], str] | None:
    if not _status_requires_daily_reminder(vehicle.inspection_status):
        return None

    status_value = vehicle.inspection_status.value

    inspection_created_local = None
    if vehicle.inspection_created_at is not None:
        inspection_created_local = _ensure_tzaware(
            vehicle.inspection_created_at
        ).astimezone(scheduled_local_now.tzinfo)

    if auto_switched_from_approved:
        cycle_text = (
            inspection_created_local.strftime("%d %b %Y %I:%M %p")
            if inspection_created_local is not None
            else "just now"
        )
        title = "Vehicle inspection status reset to pending"
        message = (
            f"15 days have passed since the last approved inspection. "
            f"The inspection status has been set back to pending as of {cycle_text}. "
            f"Please review and act."
        )
        reminder_kind = "auto_pending_after_approval_expiry"

    elif vehicle.inspection_status == VehicleInspectionStatus.REJECTED:
        cycle_text = (
            inspection_created_local.strftime("%d %b %Y %I:%M %p")
            if inspection_created_local is not None
            else "an earlier inspection request"
        )
        title = "Vehicle inspection rejected"
        message = (
            f"The vehicle inspection is currently rejected. "
            f"The current inspection cycle started on {cycle_text}. Immediate action is required."
        )
        reminder_kind = "rejected_daily"

    else:
        cycle_text = (
            inspection_created_local.strftime("%d %b %Y %I:%M %p")
            if inspection_created_local is not None
            else "an earlier inspection request"
        )
        title = "Vehicle inspection pending"
        message = (
            f"The vehicle inspection is currently pending. "
            f"The current inspection cycle started on {cycle_text}. Please review and act."
        )
        reminder_kind = "pending_daily"

    data = {
        "vehicle_id": vehicle.id,
        "driver_user_id": vehicle.driver_user_id,
        "inspection_status": status_value,
        "inspection_created_at": None
        if inspection_created_local is None
        else inspection_created_local.isoformat(),
        "inspection_reviewed_at": None,
        "auto_switched_from_approved": auto_switched_from_approved,
        "reminder_kind": reminder_kind,
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


async def _fetch_candidate_vehicle_batch(
    db: AsyncSession,
    *,
    now_utc: datetime,
    limit: int,
    after_created_at: datetime | None,
    after_vehicle_id: str | None,
) -> list[tuple[str, datetime]]:
    auto_pending_cutoff = now_utc - timedelta(days=_get_auto_pending_after_days())

    stmt = (
        select(Vehicle.id, Vehicle.created_at)
        .where(
            Vehicle.is_active.is_(True),
            Vehicle.driver_user_id.is_not(None),
            or_(
                Vehicle.inspection_status.in_(
                    [
                        VehicleInspectionStatus.PENDING,
                        VehicleInspectionStatus.REJECTED,
                    ]
                ),
                and_(
                    Vehicle.inspection_status == VehicleInspectionStatus.APPROVED,
                    Vehicle.inspection_reviewed_at.is_not(None),
                    Vehicle.inspection_reviewed_at <= auto_pending_cutoff,
                ),
            ),
        )
        .order_by(
            Vehicle.created_at.asc(),
            Vehicle.id.asc(),
        )
        .limit(limit)
    )

    if after_created_at is not None and after_vehicle_id is not None:
        stmt = stmt.where(
            or_(
                Vehicle.created_at > after_created_at,
                and_(
                    Vehicle.created_at == after_created_at,
                    Vehicle.id > after_vehicle_id,
                ),
            )
        )

    result = await db.execute(stmt)
    rows = result.all()
    return [(row[0], row[1]) for row in rows]


async def _fetch_active_admin_user_ids(db: AsyncSession) -> list[str]:
    stmt = select(User.id).where(
        User.role == UserRole.ADMIN,
        User.is_active.is_(True),
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _send_to_recipient(
    *,
    db: AsyncSession,
    ws_hub: WSHub | None,
    user_id: str,
    title: str,
    message: str,
    data: dict[str, Any],
    data_json: str,
) -> str:
    already_sent = await _notification_already_sent(
        db,
        user_id=user_id,
        title=title,
        message=message,
        data_json=data_json,
    )
    if already_sent:
        return "skip_already_sent"

    notification_service = NotificationService(db=db, ws_hub=ws_hub)
    await notification_service.notify_user(
        user_id=user_id,
        title=title,
        message=message,
        data=data,
    )
    return "sent"


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
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    vehicle = result.scalar_one_or_none()

    if vehicle is None:
        await db.rollback()
        return "skip_missing_or_ineligible"

    now_utc = scheduled_local_now.astimezone(timezone.utc)
    auto_switched_from_approved = False

    if _approved_has_expired(vehicle=vehicle, now_utc=now_utc):
        vehicle.inspection_status = VehicleInspectionStatus.PENDING
        vehicle.inspection_created_at = now_utc
        vehicle.inspection_reviewed_at = None
        vehicle.rejection_reason = None
        db.add(vehicle)
        await db.flush()
        auto_switched_from_approved = True

    built = _build_notification_payload(
        vehicle,
        scheduled_local_now=scheduled_local_now,
        slot_key=slot_key,
        auto_switched_from_approved=auto_switched_from_approved,
    )
    if built is None:
        await db.rollback()
        return "skip_not_due"

    title, message, data, data_json = built
    admin_ids = await _fetch_active_admin_user_ids(db)

    driver_outcome = await _send_to_recipient(
        db=db,
        ws_hub=ws_hub,
        user_id=vehicle.driver_user_id,
        title=title,
        message=message,
        data=data,
        data_json=data_json,
    )

    admin_sent = 0
    admin_skipped = 0

    for admin_user_id in admin_ids:
        admin_outcome = await _send_to_recipient(
            db=db,
            ws_hub=ws_hub,
            user_id=admin_user_id,
            title=title,
            message=message,
            data=data,
            data_json=data_json,
        )
        if admin_outcome == "sent":
            admin_sent += 1
        else:
            admin_skipped += 1

    return (
        f"{driver_outcome}"
        f"_admins_sent={admin_sent}"
        f"_admins_skipped={admin_skipped}"
        f"_status={vehicle.inspection_status.value}"
        f"_auto_switched={auto_switched_from_approved}"
    )


async def send_vehicle_inspection_status_reminders_once(
    *,
    ws_hub: WSHub | None = None,
    scheduled_local_now: datetime | None = None,
    slot_key: str | None = None,
) -> None:
    tz = _get_timezone()
    effective_local_now = scheduled_local_now or utcnow().astimezone(tz)

    if effective_local_now.tzinfo is None:
        effective_local_now = effective_local_now.replace(tzinfo=tz)

    effective_slot_key = slot_key or effective_local_now.strftime("%H%M")
    batch_size = _get_batch_size()
    lease_seconds = _get_lease_seconds()
    owner_id = get_job_owner_id()
    now_utc = effective_local_now.astimezone(timezone.utc)

    async with AsyncSessionLocal() as lease_db:
        acquired = await try_acquire_or_renew_job_lease(
            db=lease_db,
            job_name=_JOB_NAME,
            owner_id=owner_id,
            lease_seconds=lease_seconds,
        )

    if not acquired:
        logger.info(
            "vehicle_inspection_status_reminder skipped: job lease not acquired owner_id=%s slot=%s",
            owner_id,
            effective_slot_key,
        )
        return

    try:
        async with AsyncSessionLocal() as db:
            total_processed = 0
            cursor_created_at: datetime | None = None
            cursor_vehicle_id: str | None = None

            while True:
                batch = await _fetch_candidate_vehicle_batch(
                    db,
                    now_utc=now_utc,
                    limit=batch_size,
                    after_created_at=cursor_created_at,
                    after_vehicle_id=cursor_vehicle_id,
                )
                await db.rollback()

                if not batch:
                    break

                for vehicle_id, _created_at in batch:
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
                            "vehicle_inspection_status_reminder vehicle_id=%s slot=%s outcome=%s",
                            vehicle_id,
                            effective_slot_key,
                            outcome,
                        )
                    except Exception:
                        await db.rollback()
                        logger.exception(
                            "vehicle_inspection_status_reminder vehicle_id=%s slot=%s outcome=error",
                            vehicle_id,
                            effective_slot_key,
                        )

                last_vehicle_id, last_created_at = batch[-1]
                cursor_vehicle_id = last_vehicle_id
                cursor_created_at = last_created_at

                if len(batch) < batch_size:
                    break

            logger.info(
                "vehicle_inspection_status_reminder done slot=%s processed=%s",
                effective_slot_key,
                total_processed,
            )
    finally:
        try:
            async with AsyncSessionLocal() as lease_db:
                await release_job_lease(
                    db=lease_db,
                    job_name=_JOB_NAME,
                    owner_id=owner_id,
                )
        except Exception:
            logger.exception(
                "vehicle_inspection_status_reminder lease release failed"
            )


async def vehicle_inspection_status_reminder_loop(
    ws_hub: WSHub | None = None,
) -> None:
    tz = _get_timezone()
    logger.info(
        "vehicle_inspection_status_reminder loop started timezone=%s",
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
                "vehicle_inspection_status_reminder next_run_local=%s slot=%s sleep_seconds=%.3f",
                next_run_local.isoformat(),
                slot_key,
                delay_seconds,
            )

            await asyncio.sleep(delay_seconds)

            try:
                await send_vehicle_inspection_status_reminders_once(
                    ws_hub=ws_hub,
                    scheduled_local_now=next_run_local,
                    slot_key=slot_key,
                )
            except Exception:
                logger.exception("vehicle_inspection_status_reminder tick failed")

    except asyncio.CancelledError:
        logger.info("vehicle_inspection_status_reminder loop cancelled")
        raise