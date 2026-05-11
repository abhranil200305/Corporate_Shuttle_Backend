from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None

    cleaned = value.strip()
    return cleaned or None


def _clean_required_text(value: str) -> str:
    cleaned = value.strip()

    if not cleaned:
        raise ValueError("Value cannot be empty.")

    return cleaned


class RFIDScanRequest(BaseModel):
    device_serial_number: str = Field(..., min_length=1, max_length=120)
    card_uid: str = Field(..., min_length=1, max_length=255)

    scan_lat: Decimal | None = Field(default=None, ge=Decimal("-90"), le=Decimal("90"))
    scan_lng: Decimal | None = Field(default=None, ge=Decimal("-180"), le=Decimal("180"))

    raw_payload: dict[str, Any] | None = None

    @field_validator("device_serial_number", "card_uid")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        return _clean_required_text(value)


class RFIDScanResponse(BaseModel):
    accepted: bool
    scan_event_id: str | None = None

    scan_type: str | None = None
    rejection_reason: str | None = None
    message: str

    device_id: str | None = None
    card_id: str | None = None
    passenger_user_id: str | None = None

    scheduled_trip_id: str | None = None
    route_id: str | None = None
    vehicle_id: str | None = None
    driver_user_id: str | None = None

    matched_stop_id: str | None = None
    matched_sequence_no: int | None = None