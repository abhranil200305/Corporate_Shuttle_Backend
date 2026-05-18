from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.exceptions import AuthError
from app.auth.service import AuthService
from app.db.database import AsyncSessionLocal
from app.db.schema import User, UserRole
from app.passenger.schemas import LegAvailableSeatsRequest
from app.passenger.service import PassengerService

router = APIRouter(prefix="/passenger/seatmap", tags=["passenger-seatmap"])

logger = logging.getLogger(__name__)

WS_PING_INTERVAL_SECONDS = 15
WS_PONG_TIMEOUT_SECONDS = 30


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SeatMapTopic:
    scheduled_trip_id: str
    route_id: str
    pickup_stop_id: str
    dropoff_stop_id: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SeatMapTopic:
        scheduled_trip_id = str(payload.get("scheduled_trip_id") or "").strip()
        route_id = str(payload.get("route_id") or "").strip()
        pickup_stop_id = str(payload.get("pickup_stop_id") or "").strip()
        dropoff_stop_id = str(payload.get("dropoff_stop_id") or "").strip()

        missing_fields = [
            field_name
            for field_name, field_value in (
                ("scheduled_trip_id", scheduled_trip_id),
                ("route_id", route_id),
                ("pickup_stop_id", pickup_stop_id),
                ("dropoff_stop_id", dropoff_stop_id),
            )
            if not field_value
        ]

        if missing_fields:
            raise ValueError(
                "Missing required seatmap topic fields: "
                + ", ".join(missing_fields)
            )

        return cls(
            scheduled_trip_id=scheduled_trip_id,
            route_id=route_id,
            pickup_stop_id=pickup_stop_id,
            dropoff_stop_id=dropoff_stop_id,
        )

    def key(self) -> str:
        return (
            f"{self.scheduled_trip_id}:"
            f"{self.route_id}:"
            f"{self.pickup_stop_id}:"
            f"{self.dropoff_stop_id}"
        )


@dataclass
class SeatMapConnection:
    websocket: WebSocket
    user_id: str
    last_pong_at: datetime = field(default_factory=utcnow)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    topics: set[str] = field(default_factory=set)


class SeatMapHub:
    def __init__(self) -> None:
        self._connections: dict[str, SeatMapConnection] = {}
        self._topic_connections: dict[str, set[str]] = defaultdict(set)
        self._topic_objects: dict[str, SeatMapTopic] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        *,
        connection_id: str,
        user_id: str,
        websocket: WebSocket,
    ) -> None:
        async with self._lock:
            self._connections[connection_id] = SeatMapConnection(
                websocket=websocket,
                user_id=user_id,
            )

    async def unregister(self, connection_id: str) -> None:
        async with self._lock:
            connection = self._connections.pop(connection_id, None)
            if connection is None:
                return

            for topic_key in list(connection.topics):
                connection_ids = self._topic_connections.get(topic_key)
                if connection_ids is None:
                    continue

                connection_ids.discard(connection_id)

                if not connection_ids:
                    self._topic_connections.pop(topic_key, None)
                    self._topic_objects.pop(topic_key, None)

    async def subscribe(
        self,
        *,
        connection_id: str,
        topic: SeatMapTopic,
    ) -> None:
        topic_key = topic.key()

        async with self._lock:
            connection = self._connections.get(connection_id)
            if connection is None:
                return

            connection.topics.add(topic_key)
            self._topic_connections[topic_key].add(connection_id)
            self._topic_objects[topic_key] = topic

    async def unsubscribe(
        self,
        *,
        connection_id: str,
        topic: SeatMapTopic,
    ) -> None:
        topic_key = topic.key()

        async with self._lock:
            connection = self._connections.get(connection_id)
            if connection is not None:
                connection.topics.discard(topic_key)

            connection_ids = self._topic_connections.get(topic_key)
            if connection_ids is None:
                return

            connection_ids.discard(connection_id)

            if not connection_ids:
                self._topic_connections.pop(topic_key, None)
                self._topic_objects.pop(topic_key, None)

    async def mark_pong(self, connection_id: str) -> bool:
        async with self._lock:
            connection = self._connections.get(connection_id)
            if connection is None:
                return False

            connection.last_pong_at = utcnow()
            return True

    async def is_stale(
        self,
        connection_id: str,
        *,
        timeout_seconds: int,
    ) -> bool:
        async with self._lock:
            connection = self._connections.get(connection_id)
            if connection is None:
                return True

            age_seconds = (utcnow() - connection.last_pong_at).total_seconds()
            return age_seconds >= timeout_seconds

    async def _get_connection(
        self,
        connection_id: str,
    ) -> SeatMapConnection | None:
        async with self._lock:
            return self._connections.get(connection_id)

    async def send_to_connection(
        self,
        connection_id: str,
        payload: dict[str, Any],
    ) -> bool:
        connection = await self._get_connection(connection_id)
        if connection is None:
            return False

        safe_payload = jsonable_encoder(payload)

        async with connection.send_lock:
            await connection.websocket.send_json(safe_payload)

        return True

    async def send_ping(self, connection_id: str) -> bool:
        return await self.send_to_connection(connection_id, {"type": "ping"})

    async def close_and_unregister(
        self,
        connection_id: str,
        *,
        code: int = 1001,
    ) -> None:
        connection = await self._get_connection(connection_id)

        if connection is not None:
            try:
                async with connection.send_lock:
                    await connection.websocket.close(code=code)
            except Exception:
                pass

        await self.unregister(connection_id)

    async def get_topic_connection_ids(self, topic: SeatMapTopic) -> list[str]:
        topic_key = topic.key()

        async with self._lock:
            return list(self._topic_connections.get(topic_key, set()))

    async def get_all_topics(self) -> list[SeatMapTopic]:
        async with self._lock:
            return list(self._topic_objects.values())

    async def shutdown(self, *, code: int = 1001) -> None:
        async with self._lock:
            connection_ids = list(self._connections.keys())

        for connection_id in connection_ids:
            await self.close_and_unregister(connection_id, code=code)

        async with self._lock:
            self._connections.clear()
            self._topic_connections.clear()
            self._topic_objects.clear()


seatmap_hub = SeatMapHub()


async def _authenticate_ws_passenger(token: str) -> User:
    async with AsyncSessionLocal() as db:
        auth_service = AuthService(db)

        try:
            user = await auth_service.authenticate_token(token)
        except AuthError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={
                    "error": exc.error_code,
                    "message": exc.message,
                },
            ) from exc

        if user.role != UserRole.PASSENGER:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "passenger_only",
                    "message": "Seatmap websocket is only available to passengers.",
                },
            )

        return user


async def _build_seatmap_snapshot(
    db: AsyncSession,
    *,
    user: User,
    topic: SeatMapTopic,
    reason: str,
) -> dict[str, Any]:
    service = PassengerService(db)

    payload = LegAvailableSeatsRequest(
        route_id=topic.route_id,
        pickup_stop_id=topic.pickup_stop_id,
        dropoff_stop_id=topic.dropoff_stop_id,
    )

    seat_state = await service.get_leg_available_seats(
        user,
        topic.scheduled_trip_id,
        payload,
    )

    return {
        "type": "seat_map.snapshot",
        "reason": reason,
        "version_at": utcnow(),
        **seat_state,
    }


async def broadcast_seatmap_snapshot(
    *,
    topic: SeatMapTopic,
    reason: str,
) -> None:
    connection_ids = await seatmap_hub.get_topic_connection_ids(topic)

    if not connection_ids:
        return

    failed_connection_ids: list[str] = []

    async with AsyncSessionLocal() as db:
        for connection_id in connection_ids:
            connection = await seatmap_hub._get_connection(connection_id)
            if connection is None:
                continue

            user = await db.get(User, connection.user_id)
            if user is None:
                failed_connection_ids.append(connection_id)
                continue

            try:
                snapshot = await _build_seatmap_snapshot(
                    db,
                    user=user,
                    topic=topic,
                    reason=reason,
                )
                sent = await seatmap_hub.send_to_connection(
                    connection_id,
                    snapshot,
                )
                if not sent:
                    failed_connection_ids.append(connection_id)
            except Exception:
                logger.exception(
                    "seatmap_broadcast_failed connection_id=%s topic=%s",
                    connection_id,
                    topic.key(),
                )
                failed_connection_ids.append(connection_id)

    for connection_id in failed_connection_ids:
        await seatmap_hub.close_and_unregister(connection_id)


async def broadcast_all_seatmap_snapshots_for_trip(
    *,
    scheduled_trip_id: str,
    reason: str,
) -> None:
    topics = await seatmap_hub.get_all_topics()

    for topic in topics:
        if topic.scheduled_trip_id != scheduled_trip_id:
            continue

        await broadcast_seatmap_snapshot(
            topic=topic,
            reason=reason,
        )


async def _heartbeat_loop(connection_id: str) -> None:
    while True:
        await asyncio.sleep(WS_PING_INTERVAL_SECONDS)

        if await seatmap_hub.is_stale(
            connection_id,
            timeout_seconds=WS_PONG_TIMEOUT_SECONDS,
        ):
            await seatmap_hub.close_and_unregister(connection_id, code=1001)
            return

        ping_sent = await seatmap_hub.send_ping(connection_id)
        if not ping_sent:
            return


@router.websocket("/ws")
async def passenger_seatmap_ws(websocket: WebSocket) -> None:
    token = (websocket.query_params.get("token") or "").strip()

    if not token:
        await websocket.close(code=1008)
        return

    try:
        current_user = await _authenticate_ws_passenger(token)
    except HTTPException:
        await websocket.close(code=1008)
        return

    await websocket.accept()

    connection_id = f"{current_user.id}:{id(websocket)}"
    heartbeat_task: asyncio.Task | None = None

    await seatmap_hub.register(
        connection_id=connection_id,
        user_id=current_user.id,
        websocket=websocket,
    )

    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(connection_id),
        name=f"seatmap-heartbeat-{connection_id}",
    )

    try:
        await websocket.send_json(
            {
                "type": "seat_map.connected",
                "message": "Seatmap websocket authenticated successfully.",
                "user_id": current_user.id,
            }
        )

        while True:
            incoming = await websocket.receive_json()

            if not isinstance(incoming, dict):
                continue

            incoming_type = str(incoming.get("type") or "").strip().lower()

            if incoming_type == "ping":
                await seatmap_hub.send_to_connection(
                    connection_id,
                    {"type": "pong"},
                )
                continue

            if incoming_type == "pong":
                await seatmap_hub.mark_pong(connection_id)
                continue

            if incoming_type == "seat_map.subscribe":
                try:
                    topic = SeatMapTopic.from_payload(incoming)
                except ValueError as exc:
                    await seatmap_hub.send_to_connection(
                        connection_id,
                        {
                            "type": "seat_map.error",
                            "error": "invalid_seat_map_topic",
                            "message": str(exc),
                        },
                    )
                    continue

                await seatmap_hub.subscribe(
                    connection_id=connection_id,
                    topic=topic,
                )

                async with AsyncSessionLocal() as db:
                    snapshot = await _build_seatmap_snapshot(
                        db,
                        user=current_user,
                        topic=topic,
                        reason="subscribed",
                    )

                await seatmap_hub.send_to_connection(connection_id, snapshot)
                continue

            if incoming_type == "seat_map.unsubscribe":
                try:
                    topic = SeatMapTopic.from_payload(incoming)
                except ValueError:
                    continue

                await seatmap_hub.unsubscribe(
                    connection_id=connection_id,
                    topic=topic,
                )
                continue

            if incoming_type == "seat_map.refresh":
                try:
                    topic = SeatMapTopic.from_payload(incoming)
                except ValueError as exc:
                    await seatmap_hub.send_to_connection(
                        connection_id,
                        {
                            "type": "seat_map.error",
                            "error": "invalid_seat_map_topic",
                            "message": str(exc),
                        },
                    )
                    continue

                async with AsyncSessionLocal() as db:
                    snapshot = await _build_seatmap_snapshot(
                        db,
                        user=current_user,
                        topic=topic,
                        reason="manual_refresh",
                    )

                await seatmap_hub.send_to_connection(connection_id, snapshot)
                continue

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception(
            "seatmap_ws_connection_error user_id=%s connection_id=%s",
            current_user.id,
            connection_id,
        )
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

        await seatmap_hub.unregister(connection_id)