# app/controllers/driverprofile/driverprofile.py

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.exc import IntegrityError
from pathlib import Path
import shutil
import uuid
from typing import Optional

from app.db.schema import DriverProfile, User, DriverVerificationStatus
from app.db.database import get_async_session
from app.auth.dependencies import get_current_user

router = APIRouter(prefix="/driver-profile", tags=["DriverProfile"])

# ------------------------------
# Upload folder
# ------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent.parent
UPLOAD_DIR = BASE_DIR / "upload_pic"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ------------------------------
# Pydantic Schemas
# ------------------------------
from pydantic import BaseModel

class DriverProfileResponse(BaseModel):
    id: str
    user_id: str
    full_name: str
    phone: str
    profile_picture_path: Optional[str]
    verification_status: DriverVerificationStatus

    class Config:
        from_attributes = True  # ✅ Pydantic v2


# ------------------------------
# Helper function
# ------------------------------
def save_upload(upload: UploadFile, prefix: str) -> Optional[str]:
    if not upload or not upload.filename:
        return None

    ext = Path(upload.filename).suffix or ".jpg"
    filename = f"{prefix}_{uuid.uuid4().hex}{ext}"
    file_path = UPLOAD_DIR / filename

    upload.file.seek(0)
    with file_path.open("wb") as buffer:
        shutil.copyfileobj(upload.file, buffer)

    return str(file_path.relative_to(BASE_DIR))


# ------------------------------
# Create Driver Profile
# ------------------------------
@router.post("/", response_model=DriverProfileResponse)
async def create_driver_profile(
    full_name: str = Form(...),
    phone: str = Form(...),
    profile_pic: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """Create driver profile"""

    result = await db.execute(
        select(DriverProfile).where(DriverProfile.user_id == current_user.id)
    )
    existing = result.scalar_one_or_none()

    if existing:
        raise HTTPException(status_code=400, detail="Driver profile already exists")

    profile_pic_path = save_upload(profile_pic, "profile")

    driver_profile = DriverProfile(
        user_id=current_user.id,
        full_name=full_name,
        phone=phone,
        profile_picture_path=profile_pic_path,

        # TEMP FIX (should be nullable in DB ideally)
        aadhaar_file_path="",
        pan_file_path="",

        verification_status=DriverVerificationStatus.DRAFT,
    )

    db.add(driver_profile)

    try:
        await db.commit()
        await db.refresh(driver_profile)
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"Profile creation failed: {str(e.orig)}"
        )

    return driver_profile


# ------------------------------
# Get My Profile
# ------------------------------
@router.get("/me", response_model=DriverProfileResponse)
async def get_my_driver_profile(
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """Get logged-in driver's profile"""

    result = await db.execute(
        select(DriverProfile).where(DriverProfile.user_id == current_user.id)
    )
    profile = result.scalar_one_or_none()

    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    return profile


# ------------------------------
# Update My Profile (Partial)
# ------------------------------
@router.patch("/update", response_model=DriverProfileResponse)
async def update_my_profile(
    full_name: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    profile_pic: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """Update logged-in driver's profile (partial update)"""

    # Fetch profile
    result = await db.execute(
        select(DriverProfile).where(DriverProfile.user_id == current_user.id)
    )
    profile = result.scalar_one_or_none()

    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    # ------------------------------
    # Update fields if provided
    # ------------------------------
    if full_name is not None:
        profile.full_name = full_name

    if phone is not None:
        profile.phone = phone

    if profile_pic is not None:
        profile.profile_picture_path = save_upload(profile_pic, "profile")

    db.add(profile)

    try:
        await db.commit()
        await db.refresh(profile)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Update failed")

    return profile