from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete, or_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.schema import JobLease


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_OWNER_ID = (os.getenv("JOB_RUNNER_OWNER_ID", "").strip() or uuid4().hex)


def get_job_owner_id() -> str:
    return _OWNER_ID


def _require_nonempty(value: str, *, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} cannot be empty.")
    return cleaned


def _require_positive_int(value: int, *, field_name: str) -> int:
    if value <= 0:
        raise ValueError(f"{field_name} must be greater than 0.")
    return value


async def try_acquire_or_renew_job_lease(
    *,
    db: AsyncSession,
    job_name: str,
    owner_id: str,
    lease_seconds: int,
) -> bool:
    cleaned_job_name = _require_nonempty(job_name, field_name="job_name")
    cleaned_owner_id = _require_nonempty(owner_id, field_name="owner_id")
    normalized_lease_seconds = _require_positive_int(
        int(lease_seconds),
        field_name="lease_seconds",
    )

    now = utcnow()
    expires_at = now + timedelta(seconds=normalized_lease_seconds)

    stmt = (
        insert(JobLease)
        .values(
            job_name=cleaned_job_name,
            owner_id=cleaned_owner_id,
            lease_expires_at=expires_at,
            heartbeat_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[JobLease.job_name],
            set_={
                "owner_id": cleaned_owner_id,
                "lease_expires_at": expires_at,
                "heartbeat_at": now,
                "updated_at": now,
            },
            where=or_(
                JobLease.lease_expires_at <= now,
                JobLease.owner_id == cleaned_owner_id,
            ),
        )
        .returning(JobLease.job_name)
    )

    result = await db.execute(stmt)
    acquired = result.scalar_one_or_none() is not None

    if acquired:
        await db.commit()
        return True

    await db.rollback()
    return False


async def heartbeat_job_lease(
    *,
    db: AsyncSession,
    job_name: str,
    owner_id: str,
    lease_seconds: int,
) -> bool:
    cleaned_job_name = _require_nonempty(job_name, field_name="job_name")
    cleaned_owner_id = _require_nonempty(owner_id, field_name="owner_id")
    normalized_lease_seconds = _require_positive_int(
        int(lease_seconds),
        field_name="lease_seconds",
    )

    now = utcnow()
    expires_at = now + timedelta(seconds=normalized_lease_seconds)

    stmt = (
        update(JobLease)
        .where(
            JobLease.job_name == cleaned_job_name,
            JobLease.owner_id == cleaned_owner_id,
        )
        .values(
            lease_expires_at=expires_at,
            heartbeat_at=now,
            updated_at=now,
        )
    )

    result = await db.execute(stmt)
    updated = int(result.rowcount or 0) > 0

    if updated:
        await db.commit()
        return True

    await db.rollback()
    return False


async def release_job_lease(
    *,
    db: AsyncSession,
    job_name: str,
    owner_id: str,
) -> bool:
    cleaned_job_name = _require_nonempty(job_name, field_name="job_name")
    cleaned_owner_id = _require_nonempty(owner_id, field_name="owner_id")

    stmt = delete(JobLease).where(
        JobLease.job_name == cleaned_job_name,
        JobLease.owner_id == cleaned_owner_id,
    )

    result = await db.execute(stmt)
    released = int(result.rowcount or 0) > 0
    await db.commit()
    return released