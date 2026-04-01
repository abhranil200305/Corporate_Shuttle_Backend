# app/controllers/driverprofile/driverprofileshow.py

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from pathlib import Path

from app.db.database import get_async_session
from app.db.schema import DriverProfile, User
from app.auth.dependencies import get_current_user

router = APIRouter(prefix="/driver-profile", tags=["DriverProfile"])


# ------------------------------
# GET: Logged-in user's profile picture
# ------------------------------
@router.get("/me/profile-pic")
async def get_my_profile_pic(
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """Return logged-in driver's profile picture"""

    # Fetch profile
    result = await db.execute(
        select(DriverProfile).where(DriverProfile.user_id == current_user.id)
    )
    profile = result.scalar_one_or_none()

    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    if not profile.profile_picture_path:
        raise HTTPException(status_code=404, detail="Profile picture not set")

    # Build absolute path
    BASE_DIR = Path(__file__).resolve().parent.parent.parent
    file_path = BASE_DIR / profile.profile_picture_path

    # Check file exists
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on server")

    # Return file
    return FileResponse(path=file_path)