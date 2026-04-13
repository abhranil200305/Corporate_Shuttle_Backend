# app/driver/scan_events/scan.py

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime, timezone, timedelta
from math import radians, cos, sin, asin, sqrt
from decimal import Decimal
import base64
import json
import hmac
import hashlib
import os
import random
import asyncio
import smtplib
from email.mime.text import MIMEText

from pydantic import BaseModel

from app.db.database import get_async_session
from app.auth.dependencies import get_current_user
from app.db.schema import (
    TripBooking,
    ScheduledTrip,
    TripScanEvent,
    ScanType,
    BookingStatus,
    Stop,
    User,
)

router = APIRouter(prefix="/driver/scan", tags=["Driver Scan"])


# ============================================================
# REQUEST BODY
# ============================================================
class ScanRequest(BaseModel):
    qr_token: str | None = None
    otp_code: str | None = None
    lat: float
    lng: float


# ============================================================
# ENV
# ============================================================
QR_SECRET = os.getenv("PASSENGER_QR_SECRET")
OTP_HASH_SECRET = os.getenv("OTP_HASH_SECRET")

if not QR_SECRET:
    raise RuntimeError("PASSENGER_QR_SECRET is not set")

if not OTP_HASH_SECRET:
    raise RuntimeError("OTP_HASH_SECRET is not set")


# ============================================================
# HAVERSINE
# ============================================================
def haversine(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1

    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(a))

    return 6371 * c * 1000


# ============================================================
# OTP UTILS
# ============================================================
def generate_otp():
    return str(random.randint(100000, 999999))


def hash_otp(otp: str):
    return hmac.new(
        OTP_HASH_SECRET.encode(),
        otp.encode(),
        hashlib.sha256
    ).hexdigest()


def verify_otp(plain_otp: str, hashed_otp: str):
    return hmac.compare_digest(hash_otp(plain_otp), hashed_otp)


# ============================================================
# EMAIL OTP
# ============================================================
def send_email_sync(to_email: str, otp: str):
    msg = MIMEText(f"""
Your OTP is: {otp}

Valid for 5 minutes.
""")
    msg["Subject"] = "Trip OTP"
    msg["From"] = os.getenv("MAIL_FROM_EMAIL")
    msg["To"] = to_email

    server = smtplib.SMTP(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT")))
    server.starttls()
    server.login(os.getenv("SMTP_USERNAME"), os.getenv("SMTP_PASSWORD"))
    server.send_message(msg)
    server.quit()


async def send_email_otp(to_email: str, otp: str):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, send_email_sync, to_email, otp)


# ============================================================
# SEND OTP API
# ============================================================
@router.post("/{trip_id}/send-otp/{booking_id}")
async def send_otp(
    trip_id: str,
    booking_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    trip = await db.get(ScheduledTrip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.driver_user_id != current_user.id:
        raise HTTPException(403, "Not your trip")

    booking = await db.get(TripBooking, booking_id)
    if not booking:
        raise HTTPException(404, "Booking not found")

    if booking.scheduled_trip_id != trip_id:
        raise HTTPException(400, "Invalid booking")

    passenger = await db.get(User, booking.passenger_user_id)
    if not passenger or not passenger.email:
        raise HTTPException(400, "Passenger email not found")

    otp = generate_otp()
    otp_hash = hash_otp(otp)
    expiry = datetime.now(timezone.utc) + timedelta(minutes=5)

    if booking.booking_status == BookingStatus.BOOKED:
        booking.boarding_otp_hash = otp_hash
        booking.boarding_otp_expires_at = expiry

    elif booking.booking_status == BookingStatus.BOARDED:
        booking.drop_otp_hash = otp_hash
        booking.drop_otp_expires_at = expiry

    else:
        raise HTTPException(400, "Invalid state")

    await send_email_otp(passenger.email, otp)

    db.add(booking)
    await db.commit()

    return {"message": "OTP sent successfully"}


# ============================================================
# BASE64 FIX
# ============================================================
def add_padding(s: str) -> str:
    return s + "=" * (-len(s) % 4)


# ============================================================
# QR DECODE
# ============================================================
def decode_qr_token(qr_token: str):
    if "." not in qr_token:
        raise HTTPException(400, "Invalid QR format")

    encoded_payload, signature = qr_token.rsplit(".", 1)

    try:
        payload_bytes = base64.urlsafe_b64decode(add_padding(encoded_payload))
        payload = json.loads(payload_bytes)
    except Exception:
        raise HTTPException(400, "Invalid QR payload")

    expected_signature = hmac.new(
        QR_SECRET.encode(),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(400, "Invalid QR signature")

    return payload


# ============================================================
# MAIN SCAN / OTP VERIFY
# ============================================================
@router.post("/{trip_id}/scan")
async def scan_passenger(
    trip_id: str,
    data: ScanRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    booking = None

    # ================= QR =================
    if data.qr_token:
        payload = decode_qr_token(data.qr_token)
        booking_id = payload.get("booking_id")

        if not booking_id:
            raise HTTPException(400, "Invalid QR")

        if datetime.now(timezone.utc).timestamp() > payload["expires_at"]:
            raise HTTPException(400, "QR expired")

        result = await db.execute(
            select(TripBooking).where(
                TripBooking.id == booking_id,
                TripBooking.scheduled_trip_id == trip_id
            )
        )
        booking = result.scalar_one_or_none()

    # ================= OTP =================
    elif data.otp_code:
        result = await db.execute(
            select(TripBooking).where(
                TripBooking.scheduled_trip_id == trip_id
            )
        )
        bookings = result.scalars().all()

        now = datetime.now(timezone.utc)

        for b in bookings:
            if b.booking_status == BookingStatus.BOOKED:
                if b.boarding_otp_hash and verify_otp(data.otp_code, b.boarding_otp_hash):
                    if now > b.boarding_otp_expires_at:
                        raise HTTPException(400, "OTP expired")
                    booking = b
                    break

            elif b.booking_status == BookingStatus.BOARDED:
                if b.drop_otp_hash and verify_otp(data.otp_code, b.drop_otp_hash):
                    if now > b.drop_otp_expires_at:
                        raise HTTPException(400, "OTP expired")
                    booking = b
                    break

        if not booking:
            raise HTTPException(400, "Invalid OTP")

    else:
        raise HTTPException(400, "Provide qr_token or otp_code")

    # ================= VALIDATION =================
    trip = await db.get(ScheduledTrip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.driver_user_id != current_user.id:
        raise HTTPException(403, "Not your trip")

    if not booking:
        raise HTTPException(404, "Booking not found")

    # ================= TYPE =================
    if booking.booking_status == BookingStatus.BOOKED:
        scan_type = ScanType.BOARD
    elif booking.booking_status == BookingStatus.BOARDED:
        scan_type = ScanType.DROP
    else:
        raise HTTPException(400, "Invalid booking state")

    # ================= STOP =================
    stop_id = (
        booking.pickup_stop_id
        if scan_type == ScanType.BOARD
        else booking.dropoff_stop_id
    )

    stop = await db.get(Stop, stop_id)

    distance = haversine(
        data.lat,
        data.lng,
        float(stop.lat),
        float(stop.lng),
    )

    if distance > stop.radius_meters:
        raise HTTPException(400, "Not within stop radius")

    # ================= SAVE =================
    scan_event = TripScanEvent(
        scheduled_trip_id=trip_id,
        booking_id=booking.id,
        driver_user_id=current_user.id,
        scan_type=scan_type,
        scan_lat=Decimal(str(data.lat)),
        scan_lng=Decimal(str(data.lng)),
        matched_stop_id=stop.id,
        within_radius=True,
        qr_payload_user_id=booking.passenger_user_id,
    )

    db.add(scan_event)

    now = datetime.now(timezone.utc)

    if scan_type == ScanType.BOARD:
        booking.booking_status = BookingStatus.BOARDED
        booking.boarded_at = now
        booking.boarding_otp_hash = None

    else:
        booking.booking_status = BookingStatus.COMPLETED
        booking.completed_at = now
        booking.drop_otp_hash = None

    db.add(booking)
    await db.commit()

    return {
        "message": "Success",
        "method": "QR" if data.qr_token else "OTP",
        "scan_type": scan_type.value,
        "distance": round(distance, 2),
    }