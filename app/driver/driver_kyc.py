# app/driver/driver_kyc.py

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pathlib import Path
import shutil
import uuid
import os
from datetime import datetime, timezone

from app.db.database import get_async_session
from app.db.schema import DriverProfile, DriverVerificationStatus
from app.auth.dependencies import get_current_user

router = APIRouter(prefix="/driver/kyc", tags=["Driver KYC"])

# ✅ Base path
BASE_DIR = Path(__file__).resolve().parent.parent.parent
UPLOAD_DIR = BASE_DIR / "upload_document"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_TYPES = ["image/jpeg", "image/png", "application/pdf"]


# -----------------------------
# Helper Functions
# -----------------------------
def delete_old_file(path: str | None):
    if path and os.path.exists(path):
        os.remove(path)


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

    return str(file_path)


async def get_driver(db: AsyncSession, user_id: str):
    result = await db.execute(
        select(DriverProfile).where(DriverProfile.user_id == user_id)
    )
    return result.scalar_one_or_none()


# -----------------------------
# PATCH → Upload / Update Documents
# -----------------------------
@router.patch("/upload-document")
async def upload_documents(
    aadhaar_card: UploadFile = File(None),
    pan: UploadFile = File(None),
    driving_license: UploadFile = File(None),

    db: AsyncSession = Depends(get_async_session),
    current_user=Depends(get_current_user),
):
    driver = await get_driver(db, current_user.id)

    if not driver:
        raise HTTPException(status_code=404, detail="Driver profile not found")

    # ❗ Only allow upload in DRAFT or REJECTED
    if driver.verification_status not in [
        DriverVerificationStatus.DRAFT,
        DriverVerificationStatus.REJECTED
    ]:
        raise HTTPException(
            status_code=400,
            detail="Cannot update documents after submission"
        )

    updated_fields = {}

    # Aadhaar
    if aadhaar_card:
        delete_old_file(driver.aadhaar_file_path)
        driver.aadhaar_file_path = save_file(aadhaar_card)
        updated_fields["aadhaar_card"] = driver.aadhaar_file_path

    # PAN
    if pan:
        delete_old_file(driver.pan_file_path)
        driver.pan_file_path = save_file(pan)
        updated_fields["pan"] = driver.pan_file_path

    # Driving License
    if driving_license:
        delete_old_file(driver.driving_license_file_path)
        driver.driving_license_file_path = save_file(driving_license)
        updated_fields["driving_license"] = driver.driving_license_file_path

    if not updated_fields:
        raise HTTPException(status_code=400, detail="No files uploaded")

    await db.commit()

    return {
        "message": "Documents updated successfully",
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

    if not all([
        driver.aadhaar_file_path,
        driver.pan_file_path,
        driver.driving_license_file_path
    ]):
        raise HTTPException(status_code=400, detail="Upload all documents first")

    driver.verification_status = DriverVerificationStatus.PENDING
    driver.verification_requested_at = datetime.now(timezone.utc)

    await db.commit()

    return {"message": "KYC submitted successfully"}


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
            "aadhaar_card": driver.aadhaar_file_path,
            "pan": driver.pan_file_path,
            "driving_license": driver.driving_license_file_path
        }
    }