from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import WebSocket
from fastapi.encoders import jsonable_encoder

from app.db.schema import UserRole
from app.realtime.catalog import get_instruction

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass
class RefreshConnection:
    websocket: WebSocket
    last_pong_at: datetime = field(default_factory=utcnow)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class APIRefreshHub:
    """In-process fan-out hub for passenger and driver refresh sockets."""

    def __init__(self) -> None:
        self._connections: dict[
            UserRole,
            dict[str, dict[str, RefreshConnection]],
        ] = defaultdict(lambda: defaultdict(dict))
        self._scheduled_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        audience: UserRole,
        user_id: str,
        websocket: WebSocket,
    ) -> str:
        connection_id = uuid4().hex
        async with self._lock:
            self._connections[audience][user_id][connection_id] = (
                RefreshConnection(websocket=websocket)
            )
        return connection_id

    async def unregister(
        self,
        audience: UserRole,
        user_id: str,
        connection_id: str,
    ) -> None:
        async with self._lock:
            role_connections = self._connections.get(audience)
            if not role_connections:
                return
            user_connections = role_connections.get(user_id)
            if not user_connections:
                return
            user_connections.pop(connection_id, None)
            if not user_connections:
                role_connections.pop(user_id, None)
            if not role_connections:
                self._connections.pop(audience, None)

    async def _get_connection(
        self,
        audience: UserRole,
        user_id: str,
        connection_id: str,
    ) -> RefreshConnection | None:
        async with self._lock:
            return (
                self._connections.get(audience, {})
                .get(user_id, {})
                .get(connection_id)
            )

    async def send_to_connection(
        self,
        audience: UserRole,
        user_id: str,
        connection_id: str,
        payload: dict[str, Any],
    ) -> bool:
        connection = await self._get_connection(
            audience,
            user_id,
            connection_id,
        )
        if connection is None:
            return False
        async with connection.send_lock:
            await connection.websocket.send_json(jsonable_encoder(payload))
        return True

    async def mark_pong(
        self,
        audience: UserRole,
        user_id: str,
        connection_id: str,
    ) -> bool:
        connection = await self._get_connection(
            audience,
            user_id,
            connection_id,
        )
        if connection is None:
            return False
        connection.last_pong_at = utcnow()
        return True

    async def is_stale(
        self,
        audience: UserRole,
        user_id: str,
        connection_id: str,
        *,
        timeout_seconds: int,
    ) -> bool:
        connection = await self._get_connection(
            audience,
            user_id,
            connection_id,
        )
        if connection is None:
            return True
        return (
            utcnow() - connection.last_pong_at
        ).total_seconds() >= timeout_seconds

    def build_event_payload(
        self,
        audience: UserRole,
        event: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        instruction = get_instruction(event, audience)
        return {
            "type": "api.refresh",
            "event": event,
            "audience": audience.value,
            "resources": list(instruction.resources),
            "endpoints": list(instruction.endpoints),
            "data": data or {},
            "occurred_at": utcnow().isoformat(),
        }

    async def send_event_to_connection(
        self,
        audience: UserRole,
        user_id: str,
        connection_id: str,
        *,
        event: str,
        data: dict[str, Any] | None = None,
    ) -> bool:
        return await self.send_to_connection(
            audience,
            user_id,
            connection_id,
            self.build_event_payload(audience, event, data),
        )

    async def publish(
        self,
        audience: UserRole,
        *,
        event: str,
        data: dict[str, Any] | None = None,
        user_ids: list[str] | set[str] | tuple[str, ...] | None = None,
    ) -> int:
        payload = self.build_event_payload(audience, event, data)

        async with self._lock:
            role_connections = self._connections.get(audience, {})
            if user_ids is None:
                targets = {
                    user_id: list(connections)
                    for user_id, connections in role_connections.items()
                }
            else:
                cleaned_ids = {
                    str(user_id).strip()
                    for user_id in user_ids
                    if str(user_id).strip()
                }
                targets = {
                    user_id: list(role_connections.get(user_id, {}))
                    for user_id in cleaned_ids
                }

        sent_count = 0
        failed: list[tuple[str, str]] = []
        for user_id, connection_ids in targets.items():
            for connection_id in connection_ids:
                try:
                    if await self.send_to_connection(
                        audience,
                        user_id,
                        connection_id,
                        payload,
                    ):
                        sent_count += 1
                except Exception:
                    logger.exception(
                        "api_refresh_send_failed audience=%s user_id=%s "
                        "connection_id=%s event=%s",
                        audience.value,
                        user_id,
                        connection_id,
                        event,
                    )
                    failed.append((user_id, connection_id))

        for user_id, connection_id in failed:
            await self.close_and_unregister(
                audience,
                user_id,
                connection_id,
            )

        logger.info(
            "api_refresh_published audience=%s event=%s connections=%s",
            audience.value,
            event,
            sent_count,
        )
        return sent_count

    async def close_and_unregister(
        self,
        audience: UserRole,
        user_id: str,
        connection_id: str,
        *,
        code: int = 1001,
    ) -> None:
        connection = await self._get_connection(
            audience,
            user_id,
            connection_id,
        )
        if connection is not None:
            try:
                async with connection.send_lock:
                    await connection.websocket.close(code=code)
            except Exception:
                pass
        await self.unregister(audience, user_id, connection_id)

    async def schedule_callback(
        self,
        key: str,
        run_at: datetime,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        async def runner() -> None:
            try:
                delay = max(0.0, (as_utc(run_at) - utcnow()).total_seconds())
                if delay:
                    await asyncio.sleep(delay)
                await callback()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "api_refresh_scheduled_callback_failed key=%s",
                    key,
                )
            finally:
                async with self._lock:
                    current_task = asyncio.current_task()
                    if self._scheduled_tasks.get(key) is current_task:
                        self._scheduled_tasks.pop(key, None)

        task = asyncio.create_task(runner(), name=f"api-refresh-{key}")
        async with self._lock:
            previous = self._scheduled_tasks.get(key)
            self._scheduled_tasks[key] = task
        if previous is not None:
            previous.cancel()

    async def cancel_scheduled(self, key: str) -> None:
        async with self._lock:
            task = self._scheduled_tasks.pop(key, None)
        if task is not None:
            task.cancel()

    async def shutdown(self, *, code: int = 1001) -> None:
        async with self._lock:
            scheduled_tasks = list(self._scheduled_tasks.values())
            self._scheduled_tasks.clear()
            snapshot = {
                audience: {
                    user_id: dict(connections)
                    for user_id, connections in role_connections.items()
                }
                for audience, role_connections in self._connections.items()
            }

        for task in scheduled_tasks:
            task.cancel()
        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks, return_exceptions=True)

        for audience, role_connections in snapshot.items():
            for user_id, connections in role_connections.items():
                for connection_id in connections:
                    await self.close_and_unregister(
                        audience,
                        user_id,
                        connection_id,
                        code=code,
                    )

        async with self._lock:
            self._connections.clear()
