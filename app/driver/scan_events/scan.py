# app/driver/scan_events/scan.py

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.db.database import get_async_session
from app.db.schema import User
from app.driver.scan_events.booking_credential_scan import (
    ensure_driver_owns_trip,
    execute_credential_scan,
    resolve_qr_payload_bookings_for_update,
)

router = APIRouter(prefix="/driver/scan", tags=["Driver Scan"])


class ScanRequest(BaseModel):
    qr_token: str
    lat: float
    lng: float
    booking_id: str | None = None
    seat_number: int | None = None


QR_SECRET = os.getenv("PASSENGER_QR_SECRET")
if not QR_SECRET:
    raise RuntimeError("PASSENGER_QR_SECRET is not set")


def add_padding(s: str) -> str:
    return s + "=" * (-len(s) % 4)


def decode_qr_token(qr_token: str):
    if "." not in qr_token:
        raise HTTPException(400, "Invalid QR format")

    encoded_payload, signature = qr_token.rsplit(".", 1)

    try:
        payload_bytes = base64.urlsafe_b64decode(add_padding(encoded_payload))
        payload = json.loads(payload_bytes)
    except Exception:
        raise HTTPException(400, "Invalid QR payload") from None

    expected_signature = hmac.new(
        QR_SECRET.encode(),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(400, "Invalid QR signature")

    return payload


# =========================================================
# MAIN API
# =========================================================
@router.post("/{trip_id}/scan")
async def scan_passenger(
    trip_id: str,
    request: Request,
    data: ScanRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    # =====================================================
    # 1. DECODE QR
    # =====================================================
    payload = decode_qr_token(data.qr_token)

    if datetime.now(timezone.utc).timestamp() > payload["expires_at"]:
        raise HTTPException(400, "QR expired")

    await ensure_driver_owns_trip(
        db,
        trip_id=trip_id,
        current_user=current_user,
    )

    bookings = await resolve_qr_payload_bookings_for_update(
        db,
        trip_id=trip_id,
        payload=payload,
        target_booking_id=data.booking_id,
        target_seat_number=data.seat_number,
    )

    return await execute_credential_scan(
        trip_id=trip_id,
        request=request,
        lat=data.lat,
        lng=data.lng,
        db=db,
        current_user=current_user,
        bookings=bookings,
        success_message="Scan successful",
        driver_trip_validated=True,
    )
