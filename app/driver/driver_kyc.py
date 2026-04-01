# app/driver/driver_kyc.py

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pathlib import Path
import shutil
import uuid
from datetime import datetime, timezone

from app.db.database import get_async_session
from app.db.schema import (
    DriverProfile,
    DriverVerificationStatus,
    DriverLifecycleStatus
)
from app.auth.dependencies import get_current_user

router = APIRouter(prefix="/driver/kyc", tags=["Driver KYC"])

# -----------------------------
# Base path for uploads
# -----------------------------
BASE_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"
UPLOAD_DIR = BASE_DIR / "upload_document"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_TYPES = ["image/jpeg", "image/png", "application/pdf"]

# -----------------------------
# Helper Functions
# -----------------------------
def delete_old_file(relative_path: str | None):
    if relative_path:
        full_path = Path(__file__).resolve().parent.parent.parent / relative_path
        if full_path.exists():
            full_path.unlink()

def save_file(file: UploadFile) -> str:
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type: {file.filename}"
        )
    file_ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    file_name = f"{uuid.uuid4()}.{file_ext}"
    file_path = UPLOAD_DIR / file_name

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # store relative path for portability
    relative_path = Path("uploads") / "upload_document" / file_name
    return str(relative_path)

async def get_driver(db: AsyncSession, user_id: str):
    result = await db.execute(
        select(DriverProfile).where(DriverProfile.user_id == user_id)
    )
    return result.scalar_one_or_none()

# -----------------------------
# PATCH → Upload / Update Documents + Bank / Passbook
# -----------------------------
@router.patch("/upload-document")
async def upload_documents(
    aadhaar_card: UploadFile = File(None),
    pan: UploadFile = File(None),
    driving_license: UploadFile = File(None),
    passbook_file: UploadFile = File(None),
    aadhaar_number: str = Form(None),
    pan_number: str = Form(None),
    driving_license_number: str = Form(None),
    bank_account_number: str = Form(None),
    ifsc_code: str = Form(None),
    db: AsyncSession = Depends(get_async_session),
    current_user=Depends(get_current_user),
):
    driver = await get_driver(db, current_user.id)
    if not driver:
        raise HTTPException(status_code=404, detail="Driver profile not found")

    if driver.verification_status not in [
        DriverVerificationStatus.DRAFT,
        DriverVerificationStatus.REJECTED
    ]:
        raise HTTPException(
            status_code=400,
            detail="Cannot update documents after submission"
        )

    updated_fields = {}

    # ---------- Driver Documents ----------
    if aadhaar_card:
        delete_old_file(driver.aadhaar_file_path)
        driver.aadhaar_file_path = save_file(aadhaar_card)
        updated_fields["aadhaar_card_file"] = driver.aadhaar_file_path
    if aadhaar_number:
        driver.aadhaar_number = aadhaar_number
        updated_fields["aadhaar_number"] = driver.aadhaar_number

    if pan:
        delete_old_file(driver.pan_file_path)
        driver.pan_file_path = save_file(pan)
        updated_fields["pan_file"] = driver.pan_file_path
    if pan_number:
        driver.pan_number = pan_number
        updated_fields["pan_number"] = driver.pan_number

    if driving_license:
        delete_old_file(driver.driving_license_file_path)
        driver.driving_license_file_path = save_file(driving_license)
        updated_fields["driving_license_file"] = driver.driving_license_file_path
    if driving_license_number:
        driver.driving_license_number = driving_license_number
        updated_fields["driving_license_number"] = driver.driving_license_number

    # ---------- Bank / Passbook ----------
    if bank_account_number:
        driver.bank_account_number = bank_account_number
        updated_fields["bank_account_number"] = bank_account_number
    if ifsc_code:
        driver.ifsc_code = ifsc_code
        updated_fields["ifsc_code"] = ifsc_code
    if passbook_file:
        delete_old_file(driver.passbook_file_path)
        driver.passbook_file_path = save_file(passbook_file)
        updated_fields["passbook_file_path"] = driver.passbook_file_path

    if not updated_fields:
        raise HTTPException(status_code=400, detail="No files or numbers provided")

    await db.commit()

    return {
        "message": "Documents, IDs, and bank details updated successfully",
        "updated": updated_fields
    }

# -----------------------------
# POST → Submit KYC
# -----------------------------
@router.post("/submit")
async def submit_kyc(
    db: AsyncSession = Depends(get_async_session),
    current_user=Depends(get_current_user),
):
    driver = await get_driver(db, current_user.id)
    if not driver:
        raise HTTPException(status_code=404, detail="Driver profile not found")

    if driver.verification_status not in [
        DriverVerificationStatus.DRAFT,
        DriverVerificationStatus.REJECTED
    ]:
        raise HTTPException(status_code=400, detail="KYC already submitted")

    # Validate all required fields are present in DriverProfile
    required_fields = [
        driver.aadhaar_file_path,
        driver.pan_file_path,
        driver.driving_license_file_path,
        driver.aadhaar_number,
        driver.pan_number,
        driver.driving_license_number,
        driver.bank_account_number,
        driver.ifsc_code,
        driver.passbook_file_path
    ]

    if not all(required_fields):
        raise HTTPException(
            status_code=400,
            detail="Upload all documents (Aadhaar, PAN, DL, Passbook) and provide Aadhaar, PAN, DL numbers, bank account & IFSC"
        )

    # Mark KYC as pending
    driver.verification_status = DriverVerificationStatus.PENDING
    driver.verification_requested_at = datetime.now(timezone.utc)

    await db.commit()

    return {"message": "KYC submitted successfully", "status": driver.verification_status}

# -----------------------------
# GET → KYC Status
# -----------------------------
@router.get("/status")
async def get_kyc_status(
    db: AsyncSession = Depends(get_async_session),
    current_user=Depends(get_current_user),
):
    driver = await get_driver(db, current_user.id)
    if not driver:
        raise HTTPException(status_code=404, detail="Driver profile not found")

    return {
        "status": driver.verification_status,
        "rejection_reason": driver.rejection_reason,
        "documents": {
            "aadhaar_card_file": driver.aadhaar_file_path,
            "aadhaar_number": driver.aadhaar_number,
            "pan_file": driver.pan_file_path,
            "pan_number": driver.pan_number,
            "driving_license_file": driver.driving_license_file_path,
            "driving_license_number": driver.driving_license_number,
            "bank_account_number": driver.bank_account_number,
            "ifsc_code": driver.ifsc_code,
            "passbook_file_path": driver.passbook_file_path
        }
    }