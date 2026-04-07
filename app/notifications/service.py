# app/notifications/service.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.schema import UserNotification
from app.notifications.hub import WSHub


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NotificationService:
    def __init__(
        self,
        db: AsyncSession,
        ws_hub: WSHub | None = None,
    ) -> None:
        self.db = db
        self.ws_hub = ws_hub

    @staticmethod
    def _clean_title(value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_notification_title",
                    "message": "Notification title cannot be empty.",
                },
            )
        if len(cleaned) > 255:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_notification_title",
                    "message": "Notification title cannot exceed 255 characters.",
                },
            )
        return cleaned

    @staticmethod
    def _clean_message(value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_notification_message",
                    "message": "Notification message cannot be empty.",
                },
            )
        return cleaned

    @staticmethod
    def _serialize_notification_row(notification: UserNotification) -> dict[str, Any]:
        data: dict[str, Any] = {}
        raw = (notification.data_json or "").strip()

        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    data = parsed
            except Exception:
                data = {}

        return {
            "id": notification.id,
            "title": notification.title,
            "message": notification.message,
            "data": data,
            "read_at": notification.read_at,
            "created_at": notification.created_at,
        }

    async def create_notification(
        self,
        *,
        user_id: str,
        title: str,
        message: str,
        data: dict[str, Any] | None = None,
        flush_only: bool = False,
    ) -> dict[str, Any]:
        cleaned_title = self._clean_title(title)
        cleaned_message = self._clean_message(message)

        notification = UserNotification(
            user_id=user_id,
            title=cleaned_title,
            message=cleaned_message,
            data_json=json.dumps(data or {}, separators=(",", ":"), ensure_ascii=False),
        )
        self.db.add(notification)
        await self.db.flush()

        payload = self._serialize_notification_row(notification)

        if not flush_only:
            await self.db.commit()
            await self.db.refresh(notification)
            payload = self._serialize_notification_row(notification)

        return payload

    async def notify_user(
        self,
        *,
        user_id: str,
        title: str,
        message: str,
        data: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        payload = await self.create_notification(
            user_id=user_id,
            title=title,
            message=message,
            data=data,
            flush_only=not commit,
        )

        if commit:
            await self.push_payload_to_user(user_id=user_id, payload=payload)

        return payload

    async def push_payload_to_user(
        self,
        *,
        user_id: str,
        payload: dict[str, Any],
    ) -> None:
        if self.ws_hub is None:
            return
        await self.ws_hub.notify_user(user_id, payload)

    async def list_notifications(
        self,
        *,
        user_id: str,
        limit: int = 50,
        offset: int = 0,
        unread_only: bool = False,
    ) -> dict[str, Any]:
        safe_limit = max(1, min(limit, 100))
        safe_offset = max(0, offset)

        stmt = (
            select(UserNotification)
            .where(UserNotification.user_id == user_id)
            .order_by(UserNotification.created_at.desc())
            .limit(safe_limit)
            .offset(safe_offset)
        )

        count_stmt = select(func.count(UserNotification.id)).where(
            UserNotification.user_id == user_id
        )

        if unread_only:
            stmt = stmt.where(UserNotification.read_at.is_(None))
            count_stmt = count_stmt.where(UserNotification.read_at.is_(None))

        result = await self.db.execute(stmt)
        items = result.scalars().all()

        count_result = await self.db.execute(count_stmt)
        total_count = int(count_result.scalar_one() or 0)

        return {
            "items": [self._serialize_notification_row(item) for item in items],
            "count": total_count,
        }

    async def get_unread_count(
        self,
        *,
        user_id: str,
    ) -> int:
        stmt = select(func.count(UserNotification.id)).where(
            UserNotification.user_id == user_id,
            UserNotification.read_at.is_(None),
        )
        result = await self.db.execute(stmt)
        return int(result.scalar_one() or 0)

    async def mark_notification_read(
        self,
        *,
        user_id: str,
        notification_id: str,
    ) -> None:
        stmt = (
            select(UserNotification)
            .where(
                UserNotification.id == notification_id,
                UserNotification.user_id == user_id,
            )
            .limit(1)
        )
        result = await self.db.execute(stmt)
        notification = result.scalar_one_or_none()

        if notification is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "notification_not_found",
                    "message": "Notification not found.",
                },
            )

        if notification.read_at is None:
            notification.read_at = utcnow()
            self.db.add(notification)
            await self.db.commit()

    async def mark_all_read(
        self,
        *,
        user_id: str,
    ) -> int:
        now = utcnow()

        stmt = (
            update(UserNotification)
            .where(
                UserNotification.user_id == user_id,
                UserNotification.read_at.is_(None),
            )
            .values(read_at=now)
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return int(result.rowcount or 0)