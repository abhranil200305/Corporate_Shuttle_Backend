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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class WSConnection:
    websocket: WebSocket
    last_pong_at: datetime = field(default_factory=utcnow)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class WSHub:
    def __init__(self) -> None:
        self._connections: dict[str, dict[str, WSConnection]] = defaultdict(dict)
        self._lock = asyncio.Lock()

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
            raise RuntimeError(
                f"WS connection not found for user_id={user_id} connection_id={connection_id}"
            )

        safe_payload = jsonable_encoder(payload)

        try:
            async with connection.send_lock:
                await connection.websocket.send_json(safe_payload)
            return True

        except Exception as exc:
            raise RuntimeError(
                f"WS send failed for user_id={user_id} connection_id={connection_id}"
            ) from exc

    async def send_ping(self, user_id: str, connection_id: str) -> bool:
        return await self.send_to_connection(
            user_id,
            connection_id,
            {"type": "ping"},
        )

    async def notify_user(self, user_id: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            connection_ids = list(self._connections.get(user_id, {}).keys())

        if not connection_ids:
            return

        for connection_id in connection_ids:
            await self.send_to_connection(user_id, connection_id, payload)

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