# app/controllers/driverauth/signup.py

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from pydantic import BaseModel, EmailStr
import hashlib
import secrets
import os
import smtplib
from email.message import EmailMessage

from app.db.database import get_db
from app.db.schema import User, OTPRequest, UserRole, OTPPurpose, new_id, utcnow

router = APIRouter(prefix="/driverauth", tags=["Driver Auth"])

# ---------------------------
# Request Schema
# ---------------------------
class DriverSignupRequest(BaseModel):
    email: EmailStr
    otp: str | None = None  # OTP is optional initially


# ---------------------------
# Helpers
# ---------------------------
def generate_otp() -> str:
    """Generate a 6-digit OTP"""
    return str(secrets.randbelow(1000000)).zfill(6)


def hash_value(value: str) -> str:
    """SHA256 hash"""
    return hashlib.sha256(value.encode()).hexdigest()


def send_email_otp(to_email: str, otp: str):
    """Send OTP via SMTP using environment variables"""
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", 465))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASS")

    if not all([smtp_host, smtp_user, smtp_password]):
        print("[WARN] SMTP config missing; skipping email send")
        return

    msg = EmailMessage()
    msg.set_content(f"Your OTP for driver signup is: {otp}\nIt is valid for 5 minutes.")
    msg["Subject"] = "Driver Signup OTP"
    msg["From"] = smtp_user
    msg["To"] = to_email

    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        print(f"[INFO] OTP sent to {to_email}")
    except Exception as e:
        print(f"[ERROR] Failed to send OTP to {to_email}: {e}")


# ---------------------------
# DRIVER SIGNUP ENDPOINT
# ---------------------------
@router.post("/signup", status_code=status.HTTP_200_OK)
def driver_signup(data: DriverSignupRequest, db: Session = Depends(get_db)):
    now = utcnow()

    # ---------------------------
    # STEP 1: ISSUE OTP
    # ---------------------------
    if not data.otp:
        existing_user = db.query(User).filter(User.email == data.email).first()
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Driver with this email already exists"
            )

        otp = generate_otp()
        otp_entry = OTPRequest(
            id=new_id(),
            email=data.email,
            otp_code_hash=hash_value(otp),
            purpose=OTPPurpose.LOGIN,
            expires_at=now + timedelta(minutes=5)
        )
        db.add(otp_entry)
        db.commit()

        print(f"[DEBUG] OTP for {data.email}: {otp}")
        send_email_otp(data.email, otp)

        return {"message": "OTP issued successfully"}

    # ---------------------------
    # STEP 2: VERIFY OTP & CREATE DRIVER
    # ---------------------------
    otp_entry = (
        db.query(OTPRequest)
        .filter(
            OTPRequest.email == data.email,
            OTPRequest.purpose == OTPPurpose.LOGIN,
        )
        .order_by(OTPRequest.created_at.desc())
        .first()
    )

    if not otp_entry:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No OTP request found")
    if otp_entry.expires_at < now:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OTP expired")
    if otp_entry.otp_code_hash != hash_value(data.otp):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OTP")

    # Mark OTP as used
    otp_entry.used_at = now
    db.commit()

    # Ensure driver does not already exist
    existing_user = db.query(User).filter(User.email == data.email).first()
    if existing_user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Driver already exists")

    # Create driver user
    driver = User(
        id=new_id(),
        email=data.email,
        role=UserRole.DRIVER,
        is_active=True
    )
    db.add(driver)
    db.commit()
    db.refresh(driver)  # optional, ensures driver.id is populated

    return {"message": "Driver signup successful", "driver_id": driver.id}