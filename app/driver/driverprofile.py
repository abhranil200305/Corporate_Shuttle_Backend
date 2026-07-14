# app/driver/driverprofile.py

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from pathlib import Path
import uuid
from typing import Optional
import aiofiles
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from datetime import datetime, timezone

from app.db.schema import BookingRating
from app.db.schema import DriverProfile, User, DriverVerificationStatus
from app.db.database import get_async_session
from app.auth.dependencies import get_current_user
from app.realtime.events import get_api_refresh_hub, publish_admin_event


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

    residential_street_line_1: Optional[str]
    residential_street_line_2: Optional[str]
    residential_city: Optional[str]
    residential_state: Optional[str]
    residential_postal_code: Optional[str]
    residential_country: Optional[str]

    verification_status: DriverVerificationStatus
    average_rating: Optional[float]
    total_reviews: int
    email: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ------------------------------
# Helper functions
# ------------------------------
async def save_upload_async(file: UploadFile, folder: Path = UPLOAD_DIR) -> str:
    ext = Path(file.filename).suffix or ".jpg"
    filename = f"{uuid.uuid4().hex}{ext}"
    dest = folder / filename

    async with aiofiles.open(dest, "wb") as out_file:
        content = await file.read()
        await out_file.write(content)

    return str(dest.relative_to(BASE_DIR))


# ------------------------------
# Create Driver Profile
# ------------------------------
@router.post("/")
async def create_driver_profile(
    request: Request,
    full_name: str = Form(...),
    phone: str = Form(...),

    # =========================
    # REQUIRED ADDRESS FIELDS
    # =========================
    residential_street_line_1: str = Form(...),
    residential_street_line_2: str = Form(...),

    # =========================
    # OPTIONAL ADDRESS FIELDS
    # =========================
    residential_city: Optional[str] = Form(None),
    residential_state: Optional[str] = Form(None),
    residential_postal_code: Optional[str] = Form(None),
    residential_country: Optional[str] = Form(None),

    # =========================
    # PROFILE PICTURE
    # =========================
    profile_pic: Optional[UploadFile] = File(None),

    # =========================
    # DEPENDENCIES
    # =========================
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    
    # =========================
    # CLEAN INPUTS
    # =========================
    full_name = full_name.strip()
    phone = phone.strip()
    residential_street_line_1 = residential_street_line_1.strip()
    residential_street_line_2 = residential_street_line_2.strip()

    # =========================
    # CHECK EXISTING PROFILE FOR THIS USER
    # =========================
    result = await db.execute(
        select(DriverProfile).where(
            DriverProfile.user_id == current_user.id
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        raise HTTPException(
            status_code=400,
            detail="Driver profile already exists for this user",
        )

    # =========================
    # NEW LOGIC: CHECK IF PHONE NUMBER IS ALREADY TAKEN
    # =========================
    phone_check = await db.execute(
        select(DriverProfile).where(DriverProfile.phone == phone)
    )
    if phone_check.scalar_one_or_none():
        raise HTTPException(
            status_code=400,
            detail="A driver profile with this phone number is already registered",
        )

    # =========================
    # VALIDATE REQUIRED FIELDS
    # =========================
    if not full_name:
        raise HTTPException(status_code=422, detail="full_name cannot be empty")
    if not phone:
        raise HTTPException(status_code=422, detail="phone cannot be empty")
    if not residential_street_line_1:
        raise HTTPException(status_code=422, detail="residential_street_line_1 cannot be empty")
    if not residential_street_line_2:
        raise HTTPException(status_code=422, detail="residential_street_line_2 cannot be empty")

    # =========================
    # CLEAN OPTIONAL FIELDS
    # =========================
    residential_city = residential_city.strip() if residential_city else None
    residential_state = residential_state.strip() if residential_state else None
    residential_postal_code = residential_postal_code.strip() if residential_postal_code else None
    residential_country = residential_country.strip() if residential_country else None

    # =========================
    # SAVE PROFILE PICTURE
    # =========================
    profile_pic_path = (
        await save_upload_async(profile_pic)
        if profile_pic
        else None
    )

    # =========================
    # CREATE DRIVER PROFILE
    # =========================
    driver_profile = DriverProfile(
        user_id=current_user.id,
        full_name=full_name,
        phone=phone,
        profile_picture_path=profile_pic_path,
        residential_street_line_1=residential_street_line_1,
        residential_street_line_2=residential_street_line_2,
        residential_city=residential_city,
        residential_state=residential_state,
        residential_postal_code=residential_postal_code,
        residential_country=residential_country,
        aadhaar_file_path="",
        pan_file_path="",
        verification_status=DriverVerificationStatus.DRAFT,
    )

    db.add(driver_profile)

    # =========================
    # SAVE TO DATABASE
    # =========================
    try:
        await db.commit()
        await db.refresh(driver_profile)
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"Database error: {str(e.orig)}",
        )

    await publish_admin_event(
        get_api_refresh_hub(request.app),
        event="admin.drivers_changed",
        data={
            "driver_user_id": current_user.id,
            "reason": "driver_profile_created",
        },
    )
    return driver_profile


# ------------------------------
# Get Driver Profile
# ------------------------------
@router.get("/me", response_model=DriverProfileResponse)
async def get_my_driver_profile(
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(DriverProfile)
        .where(DriverProfile.user_id == current_user.id)
        .options(selectinload(DriverProfile.user))
    )
    profile = result.scalar_one_or_none()

    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    rating_result = await db.execute(
        select(BookingRating.driver_rating).where(
            BookingRating.driver_user_id == current_user.id
        )
    )
    ratings = rating_result.scalars().all()

    avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else None
    total_reviews = len(ratings)

    return DriverProfileResponse(
        id=profile.id,
        user_id=profile.user_id,
        full_name=profile.full_name,
        phone=profile.phone,
        profile_picture_path=profile.profile_picture_path,

        residential_street_line_1=profile.residential_street_line_1,
        residential_street_line_2=profile.residential_street_line_2,
        residential_city=profile.residential_city,
        residential_state=profile.residential_state,
        residential_postal_code=profile.residential_postal_code,
        residential_country=profile.residential_country,

        verification_status=profile.verification_status,
        email=profile.user.email if profile.user else None,
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
    request: Request,
    full_name: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),

    residential_street_line_1: Optional[str] = Form(None),
    residential_street_line_2: Optional[str] = Form(None),
    residential_city: Optional[str] = Form(None),
    residential_state: Optional[str] = Form(None),
    residential_postal_code: Optional[str] = Form(None),
    residential_country: Optional[str] = Form(None),

    profile_pic: Optional[UploadFile] = File(None),

    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(DriverProfile)
        .where(DriverProfile.user_id == current_user.id)
        .options(selectinload(DriverProfile.user))
    )
    profile = result.scalar_one_or_none()

    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    if full_name is not None:
        profile.full_name = full_name.strip()

    if phone is not None:
        profile.phone = phone.strip()

    if residential_street_line_1 is not None:
        profile.residential_street_line_1 = residential_street_line_1.strip()

    if residential_street_line_2 is not None:
        profile.residential_street_line_2 = residential_street_line_2.strip()

    if residential_city is not None:
        profile.residential_city = residential_city.strip()

    if residential_state is not None:
        profile.residential_state = residential_state.strip()

    if residential_postal_code is not None:
        profile.residential_postal_code = residential_postal_code.strip()

    if residential_country is not None:
        profile.residential_country = residential_country.strip()

    if profile_pic:
        profile.profile_picture_path = await save_upload_async(profile_pic)

    # ✅ FIXED HERE (correct place)
    profile.updated_at = datetime.now(timezone.utc)

    try:
        await db.commit()
        await db.refresh(profile)
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e.orig))

    rating_result = await db.execute(
        select(BookingRating.driver_rating).where(
            BookingRating.driver_user_id == current_user.id
        )
    )
    ratings = rating_result.scalars().all()

    avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else None
    total_reviews = len(ratings)

    response = DriverProfileResponse(
        id=profile.id,
        user_id=profile.user_id,
        full_name=profile.full_name,
        phone=profile.phone,
        profile_picture_path=profile.profile_picture_path,

        residential_street_line_1=profile.residential_street_line_1,
        residential_street_line_2=profile.residential_street_line_2,
        residential_city=profile.residential_city,
        residential_state=profile.residential_state,
        residential_postal_code=profile.residential_postal_code,
        residential_country=profile.residential_country,

        verification_status=profile.verification_status,
        email=profile.user.email if profile.user else None,
        average_rating=avg_rating,
        total_reviews=total_reviews,

        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )
    await publish_admin_event(
        get_api_refresh_hub(request.app),
        event="admin.drivers_changed",
        data={
            "driver_user_id": current_user.id,
            "reason": "driver_profile_updated",
        },
    )
    return response
