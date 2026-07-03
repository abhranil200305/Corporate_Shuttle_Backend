from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.auth.exceptions import AuthError
from app.auth.service import AuthService
from app.auth.session_utils import extract_bearer_token
from app.db.database import AsyncSessionLocal
from app.db.schema import User, UserRole
from app.realtime.events import (
    get_api_refresh_hub,
    send_current_driver_eligibility,
)

router = APIRouter(tags=["api-refresh-websocket"])

WS_PING_INTERVAL_SECONDS = 15
WS_PONG_TIMEOUT_SECONDS = 30

logger = logging.getLogger(__name__)


def _extract_token(websocket: WebSocket) -> str | None:
    query_token = (websocket.query_params.get("token") or "").strip()
    if query_token:
        return query_token
    return extract_bearer_token(websocket.headers.get("authorization"))


async def _authenticate(websocket: WebSocket, role: UserRole) -> User | None:
    token = _extract_token(websocket)
    if not token:
        return None
    async with AsyncSessionLocal() as db:
        try:
            user = await AuthService(db).authenticate_token(token)
        except AuthError:
            return None
    if user.role != role:
        return None
    return user


async def _heartbeat(
    *,
    hub,
    audience: UserRole,
    user_id: str,
    connection_id: str,
) -> None:
    while True:
        await asyncio.sleep(WS_PING_INTERVAL_SECONDS)
        if await hub.is_stale(
            audience,
            user_id,
            connection_id,
            timeout_seconds=WS_PONG_TIMEOUT_SECONDS,
        ):
            await hub.close_and_unregister(
                audience,
                user_id,
                connection_id,
            )
            return
        if not await hub.send_to_connection(
            audience,
            user_id,
            connection_id,
            {"type": "ping"},
        ):
            return


async def _serve_refresh_socket(
    websocket: WebSocket,
    audience: UserRole,
) -> None:
    user = await _authenticate(websocket, audience)
    if user is None:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    hub = get_api_refresh_hub(websocket.app)
    connection_id = await hub.register(audience, user.id, websocket)
    heartbeat_task = asyncio.create_task(
        _heartbeat(
            hub=hub,
            audience=audience,
            user_id=user.id,
            connection_id=connection_id,
        ),
        name=f"api-refresh-heartbeat-{connection_id}",
    )

    try:
        await hub.send_to_connection(
            audience,
            user.id,
            connection_id,
            {
                "type": "ws.ready",
                "channel": audience.value,
                "user_id": user.id,
                "message": "API refresh WebSocket authenticated.",
            },
        )
        await hub.send_event_to_connection(
            audience,
            user.id,
            connection_id,
            event="channel.connected",
            data={"reason": "initial_sync"},
        )
        if audience == UserRole.DRIVER:
            await send_current_driver_eligibility(
                hub,
                driver_user_id=user.id,
                connection_id=connection_id,
            )

        while True:
            incoming = await websocket.receive_json()
            if not isinstance(incoming, dict):
                continue
            incoming_type = str(incoming.get("type") or "").strip().lower()
            if incoming_type == "ping":
                await hub.send_to_connection(
                    audience,
                    user.id,
                    connection_id,
                    {"type": "pong"},
                )
            elif incoming_type == "pong":
                await hub.mark_pong(
                    audience,
                    user.id,
                    connection_id,
                )
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception(
            "api_refresh_connection_error audience=%s user_id=%s",
            audience.value,
            user.id,
        )
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        await hub.unregister(audience, user.id, connection_id)


@router.websocket("/passenger/ws/refresh")
async def passenger_refresh_ws(websocket: WebSocket) -> None:
    await _serve_refresh_socket(websocket, UserRole.PASSENGER)


@router.websocket("/driver/ws/refresh")
async def driver_refresh_ws(websocket: WebSocket) -> None:
    await _serve_refresh_socket(websocket, UserRole.DRIVER)
