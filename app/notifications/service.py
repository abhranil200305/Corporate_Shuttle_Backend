# app/notifications/service.py
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.schema import UserNotification
from app.notifications.hub import WSHub

import logging


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


logger = logging.getLogger(__name__)


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
    def _is_dev_trigger_enabled() -> bool:
        raw = os.getenv("ENABLE_DEV_NOTIFICATION_TRIGGER", "").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    @classmethod
    def _assert_dev_trigger_enabled(cls) -> None:
        if cls._is_dev_trigger_enabled():
            return

        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "Notification dev trigger is not enabled.",
            },
        )

    @staticmethod
    def _normalize_target_user_ids(
        user_id: str,
        user_ids: list[str] | None = None,
    ) -> list[str]:
        primary_user_id = str(user_id or "").strip()
        if not primary_user_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_notification_user_id",
                    "message": "Primary user_id cannot be empty.",
                },
            )

        target_user_ids: list[str] = []
        seen_user_ids: set[str] = set()

        for raw_user_id in [primary_user_id, *(user_ids or [])]:
            cleaned_user_id = str(raw_user_id or "").strip()
            if not cleaned_user_id:
                continue
            if cleaned_user_id in seen_user_ids:
                continue

            seen_user_ids.add(cleaned_user_id)
            target_user_ids.append(cleaned_user_id)

        return target_user_ids

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

    async def create_notifications(
        self,
        *,
        user_id: str,
        title: str,
        message: str,
        data: dict[str, Any] | None = None,
        user_ids: list[str] | None = None,
        flush_only: bool = False,
    ) -> list[dict[str, Any]]:
        target_user_ids = self._normalize_target_user_ids(user_id, user_ids)
        cleaned_title = self._clean_title(title)
        cleaned_message = self._clean_message(message)
        safe_data = data or {}

        notifications: list[UserNotification] = []

        for target_user_id in target_user_ids:
            notification = UserNotification(
                user_id=target_user_id,
                title=cleaned_title,
                message=cleaned_message,
                data_json=json.dumps(
                    safe_data,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
            )
            self.db.add(notification)
            notifications.append(notification)

        await self.db.flush()

        payloads = [
            self._serialize_notification_row(notification)
            for notification in notifications
        ]

        if not flush_only:
            await self.db.commit()
            for notification in notifications:
                await self.db.refresh(notification)

            payloads = [
                self._serialize_notification_row(notification)
                for notification in notifications
            ]

        return payloads

    async def create_notification(
        self,
        *,
        user_id: str,
        title: str,
        message: str,
        data: dict[str, Any] | None = None,
        user_ids: list[str] | None = None,
        flush_only: bool = False,
    ) -> dict[str, Any]:
        payloads = await self.create_notifications(
            user_id=user_id,
            user_ids=user_ids,
            title=title,
            message=message,
            data=data,
            flush_only=flush_only,
        )
        return payloads[0]

    async def notify_user(
        self,
        *,
        user_id: str,
        title: str,
        message: str,
        data: dict[str, Any] | None = None,
        user_ids: list[str] | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        target_user_ids = self._normalize_target_user_ids(user_id, user_ids)

        payloads = await self.create_notifications(
            user_id=user_id,
            user_ids=user_ids,
            title=title,
            message=message,
            data=data,
            flush_only=not commit,
        )

        payloads_by_user_id = {
            target_user_id: payload
            for target_user_id, payload in zip(target_user_ids, payloads)
        }

        primary_user_id = target_user_ids[0]
        primary_payload = payloads_by_user_id[primary_user_id]

        if commit:
            await self.push_payload_to_user(
                user_id=primary_user_id,
                user_ids=target_user_ids[1:] or None,
                payload=primary_payload,
                payloads_by_user_id=payloads_by_user_id,
            )

        return primary_payload

    async def push_payload_to_user(
        self,
        *,
        user_id: str,
        payload: dict[str, Any],
        user_ids: list[str] | None = None,
        payloads_by_user_id: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        target_user_ids = self._normalize_target_user_ids(user_id, user_ids)

        logger.info(
            "notification_emit_requested primary_user_id=%s target_user_count=%s notification_id=%s title=%r message=%r payload=%s",
            user_id,
            len(target_user_ids),
            payload.get("id"),
            payload.get("title"),
            payload.get("message"),
            payload,
        )

        if self.ws_hub is None:
            logger.warning(
                "notification_emit_skipped_no_hub primary_user_id=%s notification_id=%s",
                user_id,
                payload.get("id"),
            )
            return

        if payloads_by_user_id is None:
            await self.ws_hub.notify_user(
                user_id,
                payload,
                user_ids=user_ids,
            )
            return

        for target_user_id in target_user_ids:
            target_payload = payloads_by_user_id.get(target_user_id)
            if target_payload is None:
                continue

            await self.ws_hub.notify_user(
                target_user_id,
                target_payload,
            )

    async def trigger_dev_notification(
        self,
        *,
        user_id: str,
        title: str,
        message: str,
        data: dict[str, Any] | None = None,
        user_ids: list[str] | None = None,
        persist: bool = False,
    ) -> dict[str, Any]:
        self._assert_dev_trigger_enabled()

        cleaned_title = self._clean_title(title)
        cleaned_message = self._clean_message(message)
        safe_data = data or {}

        if persist:
            payload = await self.notify_user(
                user_id=user_id,
                user_ids=user_ids,
                title=cleaned_title,
                message=cleaned_message,
                data=safe_data,
                commit=True,
            )
            return {
                "message": "Persisted and pushed notification successfully.",
                "mode": "persisted_and_live",
                "payload": payload,
            }

        payload = {
            "id": f"dev-{uuid4().hex}",
            "title": cleaned_title,
            "message": cleaned_message,
            "data": safe_data,
            "read_at": None,
            "created_at": utcnow(),
        }

        await self.push_payload_to_user(
            user_id=user_id,
            user_ids=user_ids,
            payload=payload,
        )

        return {
            "message": "Live-only notification pushed successfully.",
            "mode": "live_only",
            "payload": payload,
        }

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