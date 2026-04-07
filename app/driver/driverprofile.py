# app/controllers/driverprofile/driverprofile.py
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.exc import IntegrityError
from pathlib import Path
import shutil
import uuid
from typing import Optional
import aiofiles  # ✅ FIXED: import aiofiles
from sqlalchemy import select
from app.db.schema import BookingRating

from app.db.schema import DriverProfile, User, DriverVerificationStatus
from app.db.database import get_async_session
from app.auth.dependencies import get_current_user

router = APIRouter(prefix="/driver-profile", tags=["DriverProfile"])

# ------------------------------
# Upload folder
# ------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent.parent
UPLOAD_DIR = BASE_DIR / "uploads" / "upload_pic"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ------------------------------
# Pydantic Schemas
# ------------------------------

class DriverProfileResponse(BaseModel):
    id: str
    user_id: str
    full_name: str
    phone: str
    profile_picture_path: Optional[str]
    verification_status: DriverVerificationStatus
    average_rating: Optional[float]
    total_reviews: int

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

    # Ensure directory exists (safe)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    upload.file.seek(0)
    with file_path.open("wb") as buffer:
        shutil.copyfileobj(upload.file, buffer)

    # ✅ RETURN ABSOLUTE PATH
    return str(file_path.resolve())

async def save_upload_async(file: UploadFile, folder: Path = UPLOAD_DIR) -> str:
    ext = Path(file.filename).suffix or ".jpg"
    filename = f"{uuid.uuid4().hex}{ext}"
    dest = folder / filename
    async with aiofiles.open(dest, "wb") as out_file:
        content = await file.read()
        await out_file.write(content)
    return str(dest)

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
    """Get logged-in driver's profile WITH rating"""

    # -------------------------
    # Fetch profile
    # -------------------------
    result = await db.execute(
        select(DriverProfile).where(DriverProfile.user_id == current_user.id)
    )
    profile = result.scalar_one_or_none()

    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    # -------------------------
    # Fetch ratings
    # -------------------------
    rating_result = await db.execute(
        select(BookingRating.driver_rating)
        .where(BookingRating.driver_user_id == current_user.id)
    )

    ratings = rating_result.scalars().all()

    if ratings:
        avg_rating = round(sum(ratings) / len(ratings), 2)
        total_reviews = len(ratings)
    else:
        avg_rating = None
        total_reviews = 0

    # -------------------------
    # Return merged response
    # -------------------------
    return {
        **profile.__dict__,
        "average_rating": avg_rating,
        "total_reviews": total_reviews,
    }


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
        full_name_cleaned = full_name.strip()
        if not full_name_cleaned:
            raise HTTPException(status_code=400, detail="Full name cannot be empty")
        profile.full_name = full_name_cleaned

    if phone is not None:
        phone_cleaned = phone.strip()
        if not phone_cleaned:
            raise HTTPException(status_code=400, detail="Phone cannot be empty")
        profile.phone = phone_cleaned

    if profile_pic is not None:
        # Delete old file if exists
        if profile.profile_picture_path:
            try:
                old_path = Path(profile.profile_picture_path)
                if old_path.exists():
                    old_path.unlink()
            except Exception:
                pass

        profile.profile_picture_path = await save_upload_async(profile_pic)

    db.add(profile)

    try:
        await db.commit()
        await db.refresh(profile)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Update failed")

    # ------------------------------
    # Calculate ratings
    # ------------------------------
    rating_result = await db.execute(
        select(BookingRating.driver_rating)
        .where(BookingRating.driver_user_id == current_user.id)
    )
    ratings = rating_result.scalars().all()
    avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else None
    total_reviews = len(ratings)

    # ------------------------------
    # Return response compatible with DriverProfileResponse
    # ------------------------------
    return DriverProfileResponse(
        id=profile.id,
        user_id=profile.user_id,
        full_name=profile.full_name,
        phone=profile.phone,
        profile_picture_path=profile.profile_picture_path,
        average_rating=avg_rating,
        verification_status=profile.verification_status,  # <--- ADD THIS
        total_reviews=total_reviews,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )