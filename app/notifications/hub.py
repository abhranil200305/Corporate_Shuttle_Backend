# app/notifications/hub.py
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any
from uuid import uuid4

from fastapi import WebSocket


class WSHub:
    def __init__(self) -> None:
        self._connections: dict[str, dict[str, WebSocket]] = defaultdict(dict)
        self._lock = asyncio.Lock()

    async def register(self, user_id: str, websocket: WebSocket) -> str:
        connection_id = uuid4().hex
        async with self._lock:
            self._connections[user_id][connection_id] = websocket
        return connection_id

    async def unregister(self, user_id: str, connection_id: str) -> None:
        async with self._lock:
            user_connections = self._connections.get(user_id)
            if not user_connections:
                return

            user_connections.pop(connection_id, None)

            if not user_connections:
                self._connections.pop(user_id, None)

    async def notify_user(self, user_id: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            user_connections = list(self._connections.get(user_id, {}).items())

        if not user_connections:
            return

        dead_connection_ids: list[str] = []

        for connection_id, websocket in user_connections:
            try:
                await websocket.send_json(payload)
            except Exception:
                dead_connection_ids.append(connection_id)

        for connection_id in dead_connection_ids:
            await self.unregister(user_id, connection_id)

    async def connection_count_for_user(self, user_id: str) -> int:
        async with self._lock:
            return len(self._connections.get(user_id, {}))