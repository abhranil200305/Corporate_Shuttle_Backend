# app/notifications/hub.py
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import WebSocket
from fastapi.encoders import jsonable_encoder

import logging


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


logger = logging.getLogger(__name__)


@dataclass
class WSConnection:
    websocket: WebSocket
    last_pong_at: datetime = field(default_factory=utcnow)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class WSHub:
    def __init__(self) -> None:
        self._connections: dict[str, dict[str, WSConnection]] = defaultdict(dict)
        self._lock = asyncio.Lock()

    @staticmethod
    def _is_heartbeat_payload(payload: dict[str, Any]) -> bool:
        payload_type = str(payload.get("type") or "").strip().lower()
        return payload_type in {"ping", "pong"}

    @staticmethod
    def _normalize_target_user_ids(
        user_id: str,
        user_ids: list[str] | None = None,
    ) -> list[str]:
        primary_user_id = str(user_id or "").strip()
        if not primary_user_id:
            raise RuntimeError("Primary user_id cannot be empty.")

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

    async def register(self, user_id: str, websocket: WebSocket) -> str:
        connection_id = uuid4().hex
        async with self._lock:
            self._connections[user_id][connection_id] = WSConnection(
                websocket=websocket,
            )
        return connection_id

    async def unregister(self, user_id: str, connection_id: str) -> None:
        async with self._lock:
            user_connections = self._connections.get(user_id)
            if not user_connections:
                return

            user_connections.pop(connection_id, None)

            if not user_connections:
                self._connections.pop(user_id, None)

    async def mark_pong(self, user_id: str, connection_id: str) -> bool:
        async with self._lock:
            connection = self._connections.get(user_id, {}).get(connection_id)
            if connection is None:
                return False

            connection.last_pong_at = utcnow()
            return True

    async def is_stale(
        self,
        user_id: str,
        connection_id: str,
        *,
        timeout_seconds: int,
    ) -> bool:
        async with self._lock:
            connection = self._connections.get(user_id, {}).get(connection_id)
            if connection is None:
                return True

            age_seconds = (utcnow() - connection.last_pong_at).total_seconds()
            return age_seconds >= timeout_seconds

    async def _get_connection(
        self,
        user_id: str,
        connection_id: str,
    ) -> WSConnection | None:
        async with self._lock:
            return self._connections.get(user_id, {}).get(connection_id)

    async def send_to_connection(
        self,
        user_id: str,
        connection_id: str,
        payload: dict[str, Any],
    ) -> bool:
        connection = await self._get_connection(user_id, connection_id)
        if connection is None:
            return False

        safe_payload = jsonable_encoder(payload)
        is_heartbeat = self._is_heartbeat_payload(safe_payload)

        if not is_heartbeat:
            logger.info(
                "ws_send_attempt user_id=%s connection_id=%s notification_id=%s title=%r message=%r payload=%s",
                user_id,
                connection_id,
                safe_payload.get("id"),
                safe_payload.get("title"),
                safe_payload.get("message"),
                safe_payload,
            )

        async with connection.send_lock:
            await connection.websocket.send_json(safe_payload)

        if not is_heartbeat:
            logger.info(
                "ws_send_success user_id=%s connection_id=%s notification_id=%s",
                user_id,
                connection_id,
                safe_payload.get("id"),
            )

        return True

    async def send_ping(self, user_id: str, connection_id: str) -> bool:
        return await self.send_to_connection(
            user_id,
            connection_id,
            {"type": "ping"},
        )

    async def notify_user(
        self,
        user_id: str,
        payload: dict[str, Any],
        user_ids: list[str] | None = None,
    ) -> None:
        target_user_ids = self._normalize_target_user_ids(user_id, user_ids)

        logger.info(
            "ws_notification_emit_multi target_user_count=%s notification_id=%s title=%r message=%r payload=%s",
            len(target_user_ids),
            payload.get("id"),
            payload.get("title"),
            payload.get("message"),
            payload,
        )

        for target_user_id in target_user_ids:
            async with self._lock:
                connection_ids = list(self._connections.get(target_user_id, {}).keys())

            logger.info(
                "ws_notification_emit user_id=%s connection_count=%s notification_id=%s title=%r message=%r payload=%s",
                target_user_id,
                len(connection_ids),
                payload.get("id"),
                payload.get("title"),
                payload.get("message"),
                payload,
            )

            if not connection_ids:
                logger.warning(
                    "ws_notification_emit_no_connections user_id=%s notification_id=%s",
                    target_user_id,
                    payload.get("id"),
                )
                continue

            failed_connection_ids: list[str] = []

            for connection_id in connection_ids:
                try:
                    sent = await self.send_to_connection(
                        target_user_id,
                        connection_id,
                        payload,
                    )
                    if not sent:
                        failed_connection_ids.append(connection_id)
                except Exception as exc:
                    logger.warning(
                        "ws_notification_send_failed user_id=%s connection_id=%s notification_id=%s error=%r",
                        target_user_id,
                        connection_id,
                        payload.get("id"),
                        exc,
                    )
                    failed_connection_ids.append(connection_id)

            for connection_id in failed_connection_ids:
                await self.close_and_unregister(target_user_id, connection_id)

    async def close_and_unregister(
        self,
        user_id: str,
        connection_id: str,
        *,
        code: int = 1001,
    ) -> None:
        connection = await self._get_connection(user_id, connection_id)

        if connection is not None:
            try:
                async with connection.send_lock:
                    await connection.websocket.close(code=code)
            except Exception:
                pass

        await self.unregister(user_id, connection_id)

    async def connection_count_for_user(self, user_id: str) -> int:
        async with self._lock:
            return len(self._connections.get(user_id, {}))

    async def shutdown(self, *, code: int = 1001) -> None:
        async with self._lock:
            snapshot = {
                user_id: dict(connections)
                for user_id, connections in self._connections.items()
            }

        for user_id, user_connections in snapshot.items():
            for connection_id, connection in user_connections.items():
                try:
                    async with connection.send_lock:
                        await connection.websocket.close(code=code)
                except Exception:
                    pass

                await self.unregister(user_id, connection_id)

        async with self._lock:
            self._connections.clear()