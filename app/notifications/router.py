# app/notifications/router.py
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_active_user, get_db
from app.auth.exceptions import AuthError
from app.auth.service import AuthService
from app.db.database import AsyncSessionLocal
from app.db.schema import User
from app.notifications.schemas import (
    NotificationListResponse,
    NotificationMessageResponse,
    NotificationUnreadCountResponse,
)
from app.notifications.service import NotificationService


router = APIRouter(prefix="/notifications", tags=["notifications"])


def _get_ws_hub_from_app(app: Any):
    hub = getattr(app.state, "ws_hub", None)
    if hub is None:
        raise RuntimeError("WS hub is not initialized on app.state.")
    return hub


def _get_notification_service(
    request: Request,
    db: AsyncSession,
) -> NotificationService:
    return NotificationService(
        db=db,
        ws_hub=_get_ws_hub_from_app(request.app),
    )


@router.get("", response_model=NotificationListResponse)
async def list_notifications(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    unread_only: bool = Query(default=False),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    service = _get_notification_service(request, db)
    return await service.list_notifications(
        user_id=current_user.id,
        limit=limit,
        offset=offset,
        unread_only=unread_only,
    )


@router.get("/unread-count", response_model=NotificationUnreadCountResponse)
async def unread_count(
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    service = _get_notification_service(request, db)
    count = await service.get_unread_count(user_id=current_user.id)
    return {"unread_count": count}


@router.post("/{notification_id}/read", response_model=NotificationMessageResponse)
async def mark_read(
    request: Request,
    notification_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    service = _get_notification_service(request, db)
    await service.mark_notification_read(
        user_id=current_user.id,
        notification_id=notification_id,
    )
    return {"message": "Notification marked as read."}


@router.post("/read-all", response_model=NotificationMessageResponse)
async def mark_all_read(
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    service = _get_notification_service(request, db)
    await service.mark_all_read(user_id=current_user.id)
    return {"message": "All notifications marked as read."}


async def _authenticate_ws_user(token: str) -> User:
    async with AsyncSessionLocal() as db:
        auth_service = AuthService(db)
        try:
            return await auth_service.authenticate_token(token)
        except AuthError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={
                    "error": exc.error_code,
                    "message": exc.message,
                },
            ) from exc


@router.websocket("/ws")
async def notifications_ws(websocket: WebSocket) -> None:
    await websocket.accept()

    try:
        auth_payload = await websocket.receive_json()
    except Exception:
        await websocket.send_json(
            {
                "error": "invalid_ws_auth_payload",
                "message": "Expected initial auth JSON payload.",
            }
        )
        await websocket.close(code=1008)
        return

    if not isinstance(auth_payload, dict):
        await websocket.send_json(
            {
                "error": "invalid_ws_auth_payload",
                "message": "Expected initial auth JSON object.",
            }
        )
        await websocket.close(code=1008)
        return

    message_type = str(auth_payload.get("type") or "").strip().lower()
    token = str(auth_payload.get("token") or "").strip()

    if message_type != "auth" or not token:
        await websocket.send_json(
            {
                "error": "invalid_ws_auth_payload",
                "message": "First websocket message must be {\"type\":\"auth\",\"token\":\"...\"}.",
            }
        )
        await websocket.close(code=1008)
        return

    try:
        current_user = await _authenticate_ws_user(token)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        await websocket.send_json(
            {
                "error": detail.get("error", "ws_auth_failed"),
                "message": detail.get("message", "WebSocket authentication failed."),
            }
        )
        await websocket.close(code=1008)
        return

    hub = _get_ws_hub_from_app(websocket.app)
    connection_id = await hub.register(current_user.id, websocket)

    try:
        await websocket.send_json(
            {
                "message": "WebSocket authenticated successfully.",
                "user_id": current_user.id,
            }
        )

        while True:
            incoming = await websocket.receive_json()

            if not isinstance(incoming, dict):
                continue

            incoming_type = str(incoming.get("type") or "").strip().lower()

            if incoming_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            if incoming_type == "mark_read":
                notification_id = str(incoming.get("notification_id") or "").strip()
                if notification_id:
                    async with AsyncSessionLocal() as db:
                        service = NotificationService(db=db, ws_hub=hub)
                        await service.mark_notification_read(
                            user_id=current_user.id,
                            notification_id=notification_id,
                        )
                    await websocket.send_json(
                        {
                            "message": "Notification marked as read.",
                            "notification_id": notification_id,
                        }
                    )
                continue

    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        await hub.unregister(current_user.id, connection_id)