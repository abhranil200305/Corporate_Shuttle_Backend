# app/driver/driverprofile.py
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.exc import IntegrityError
from pathlib import Path
import shutil
import uuid
from typing import Optional
import aiofiles
from sqlalchemy import select
from app.db.schema import BookingRating
from app.db.schema import DriverProfile, User, DriverVerificationStatus
from app.db.database import get_async_session
from app.auth.dependencies import get_current_user
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError
from datetime import datetime




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
    email: Optional[str]  # ✅ ADD THIS
    created_at: datetime   # ✅ ADD THIS
    updated_at: datetime   # ✅ ADD THIS


    class Config:
        from_attributes = True  # ✅ Pydantic v2

# ------------------------------
# Helper functions
# ------------------------------
def save_upload(upload: UploadFile, prefix: str) -> Optional[str]:
    if not upload or not upload.filename:
        return None

    ext = Path(upload.filename).suffix or ".jpg"
    filename = f"{prefix}_{uuid.uuid4().hex}{ext}"
    file_path = UPLOAD_DIR / filename
    file_path.parent.mkdir(parents=True, exist_ok=True)

    upload.file.seek(0)
    with file_path.open("wb") as buffer:
        shutil.copyfileobj(upload.file, buffer)

    # Return relative path
    return str(file_path.relative_to(BASE_DIR))

async def save_upload_async(file: UploadFile, folder: Path = UPLOAD_DIR) -> str:
    ext = Path(file.filename).suffix or ".jpg"
    filename = f"{uuid.uuid4().hex}{ext}"
    dest = folder / filename

    async with aiofiles.open(dest, "wb") as out_file:
        content = await file.read()
        await out_file.write(content)

    # Return relative path
    return str(dest.relative_to(BASE_DIR))

# ------------------------------
# Create Driver Profile
# ------------------------------
@router.post("/")
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

    profile_pic_path = await save_upload_async(profile_pic) if profile_pic else None

    driver_profile = DriverProfile(
        user_id=current_user.id,
        full_name=full_name.strip(),
        phone=phone.strip(),
        profile_picture_path=profile_pic_path,
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

@router.get("/me", response_model=DriverProfileResponse)
async def get_my_driver_profile(
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """Get logged-in driver's profile WITH rating + email"""

    # 👇 Load user relation
    result = await db.execute(
        select(DriverProfile)
        .where(DriverProfile.user_id == current_user.id)
        .options(selectinload(DriverProfile.user))
    )
    profile = result.scalar_one_or_none()

    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    # 👇 Ratings
    rating_result = await db.execute(
        select(BookingRating.driver_rating).where(
            BookingRating.driver_user_id == current_user.id
        )
    )
    ratings = rating_result.scalars().all()

    avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else None
    total_reviews = len(ratings)

    # 👇 Clean response (NO __dict__)
    return DriverProfileResponse(
        id=profile.id,
        user_id=profile.user_id,
        full_name=profile.full_name,
        phone=profile.phone,
        profile_picture_path=profile.profile_picture_path,
        verification_status=profile.verification_status,
        email=profile.user.email if profile.user else None,  # ✅ EMAIL
        average_rating=avg_rating,
        total_reviews=total_reviews,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )
# ------------------------------
# Update Driver Profile
# ------------------------------
@router.patch("/update", response_model=DriverProfileResponse)
async def update_driver_profile(
    full_name: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    profile_pic: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """Update logged-in driver's profile"""

    # 🔍 Get existing profile
    result = await db.execute(
        select(DriverProfile)
        .where(DriverProfile.user_id == current_user.id)
        .options(selectinload(DriverProfile.user))
    )
    profile = result.scalar_one_or_none()

    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    # ✏️ Update fields (only if provided)
    if full_name is not None:
        profile.full_name = full_name.strip()

    if phone is not None:
        profile.phone = phone.strip()

    # 🖼️ Update profile picture
    if profile_pic:
        new_path = await save_upload_async(profile_pic)
        profile.profile_picture_path = new_path

    # ⏱️ Update timestamp
    profile.updated_at = datetime.utcnow()

    try:
        await db.commit()
        await db.refresh(profile)
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"Update failed: {str(e.orig)}"
        )

    # ⭐ Ratings
    rating_result = await db.execute(
        select(BookingRating.driver_rating).where(
            BookingRating.driver_user_id == current_user.id
        )
    )
    ratings = rating_result.scalars().all()

    avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else None
    total_reviews = len(ratings)

    # ✅ Return response
    return DriverProfileResponse(
        id=profile.id,
        user_id=profile.user_id,
        full_name=profile.full_name,
        phone=profile.phone,
        profile_picture_path=profile.profile_picture_path,
        verification_status=profile.verification_status,
        email=profile.user.email if profile.user else None,
        average_rating=avg_rating,
        total_reviews=total_reviews,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )

