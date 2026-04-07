# app/notifications/schemas.py
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class NotificationDataPayload(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    message: str = Field(..., min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)


class NotificationResponse(BaseModel):
    id: str
    title: str
    message: str
    data: dict[str, Any]
    read_at: datetime | None
    created_at: datetime


class NotificationListResponse(BaseModel):
    items: list[NotificationResponse]
    count: int


class NotificationUnreadCountResponse(BaseModel):
    unread_count: int


class NotificationMessageResponse(BaseModel):
    message: str


class WebSocketAuthMessage(BaseModel):
    type: str
    token: str


class WebSocketPingMessage(BaseModel):
    type: str


class WebSocketMarkReadMessage(BaseModel):
    type: str
    notification_id: str


class WebSocketEnvelope(BaseModel):
    id: str
    title: str
    message: str
    data: dict[str, Any]
    created_at: datetime