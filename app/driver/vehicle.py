# app/driver/vehicle.py

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pathlib import Path
import shutil
from datetime import datetime, timezone

from app.db.database import get_async_session
from app.db.schema import (
    Vehicle,
    VehicleVerificationStatus,
    User,
    UserRole,
    DriverProfile,
    DriverVerificationStatus
)
from app.driver.schemas import VehicleOut
from app.auth.dependencies import get_current_active_user

router = APIRouter(prefix="/driver/vehicle", tags=["Driver Vehicle"])

# Save uploaded vehicle photos here
UPLOAD_DIR = Path("uploads/upload_vehicledetails_photo")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------
# Register Vehicle
# ---------------------------
@router.post("/register", response_model=VehicleOut, status_code=status.HTTP_201_CREATED)
async def register_vehicle(
    registration_number: str = Form(...),
    vehicle_name: str = Form(...),
    vehicle_model: str = Form(...),
    color: str = Form(...),
    seat_count: int = Form(...),
    has_ac: bool = Form(False),
    rc_file: UploadFile = File(...),
    rear_photo: UploadFile = File(...),
    current_driver: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_async_session)
):
    if current_driver.role != UserRole.DRIVER:
        raise HTTPException(status_code=403, detail="Only drivers can register vehicles")

    # ✅ KYC CHECK (NEW)
    result = await session.execute(
        select(DriverProfile).where(DriverProfile.user_id == current_driver.id)
    )
    driver_profile = result.scalar_one_or_none()

    if not driver_profile:
        raise HTTPException(status_code=400, detail="Driver profile not found. Complete KYC first.")

    if driver_profile.verification_status != DriverVerificationStatus.VERIFIED:
        raise HTTPException(
            status_code=403,
            detail="KYC not verified. Complete and get approval before registering vehicle."
        )

    driver_user_id = current_driver.id

    # Check for duplicate registration number
    result = await session.execute(
        select(Vehicle).where(Vehicle.registration_number == registration_number)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Vehicle registration already exists")

    # Save files
    rc_filename = f"{registration_number}_rc_{rc_file.filename}"
    rear_filename = f"{registration_number}_rear_{rear_photo.filename}"

    rc_path = UPLOAD_DIR / rc_filename
    rear_path = UPLOAD_DIR / rear_filename

    with open(rc_path, "wb") as f:
        shutil.copyfileobj(rc_file.file, f)

    with open(rear_path, "wb") as f:
        shutil.copyfileobj(rear_photo.file, f)

    # Save vehicle in DB
    vehicle = Vehicle(
        driver_user_id=driver_user_id,
        registration_number=registration_number,
        vehicle_name=vehicle_name,
        vehicle_model=vehicle_model,
        color=color,
        seat_count=seat_count,
        has_ac=has_ac,
        rc_file_path=str(Path("uploads/upload_vehicledetails_photo") / rc_filename),
        rear_photo_file_path=str(Path("uploads/upload_vehicledetails_photo") / rear_filename),
        verification_status=VehicleVerificationStatus.DRAFT,
    )

    session.add(vehicle)
    await session.commit()
    await session.refresh(vehicle)

    return vehicle


# ---------------------------
# View Own Vehicle
# ---------------------------
@router.get("/my-vehicle", response_model=VehicleOut)
async def get_my_vehicle(
    current_driver: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_async_session)
):
    if current_driver.role != UserRole.DRIVER:
        raise HTTPException(status_code=403, detail="Only drivers can view vehicles")

    result = await session.execute(
        select(Vehicle).where(Vehicle.driver_user_id == current_driver.id)
    )
    vehicle = result.scalar_one_or_none()

    if not vehicle:
        raise HTTPException(status_code=404, detail="No vehicle registered")

    return vehicle


# ---------------------------
# Update Vehicle
# ---------------------------
@router.patch("/update", response_model=VehicleOut)
async def update_vehicle(
    vehicle_name: str | None = Form(None),
    vehicle_model: str | None = Form(None),
    color: str | None = Form(None),
    seat_count: int | None = Form(None),
    has_ac: bool | None = Form(None),
    rc_file: UploadFile | None = File(None),
    rear_photo: UploadFile | None = File(None),
    current_driver: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_async_session)
):
    if current_driver.role != UserRole.DRIVER:
        raise HTTPException(status_code=403, detail="Only drivers can update vehicles")

    result = await session.execute(
        select(Vehicle).where(Vehicle.driver_user_id == current_driver.id)
    )
    vehicle = result.scalar_one_or_none()

    if not vehicle:
        raise HTTPException(status_code=404, detail="No vehicle registered")

    # Update fields
    if vehicle_name is not None:
        vehicle.vehicle_name = vehicle_name
    if vehicle_model is not None:
        vehicle.vehicle_model = vehicle_model
    if color is not None:
        vehicle.color = color
    if seat_count is not None:
        vehicle.seat_count = seat_count
    if has_ac is not None:
        vehicle.has_ac = has_ac

    # Update files
    if rc_file:
        rc_filename = f"{vehicle.registration_number}_rc_{rc_file.filename}"
        rc_path = UPLOAD_DIR / rc_filename
        with open(rc_path, "wb") as f:
            shutil.copyfileobj(rc_file.file, f)
        vehicle.rc_file_path = str(rc_path)

    if rear_photo:
        rear_filename = f"{vehicle.registration_number}_rear_{rear_photo.filename}"
        rear_path = UPLOAD_DIR / rear_filename
        with open(rear_path, "wb") as f:
            shutil.copyfileobj(rear_photo.file, f)
        vehicle.rear_photo_file_path = str(rear_path)

    # Reset verification
    vehicle.verification_status = VehicleVerificationStatus.DRAFT
    vehicle.rejection_reason = None

    await session.commit()
    await session.refresh(vehicle)

    return vehicle


# ---------------------------
# Request Verification
# ---------------------------
@router.post("/request-verification", response_model=VehicleOut)
async def request_vehicle_verification(
    current_driver: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_async_session)
):
    if current_driver.role != UserRole.DRIVER:
        raise HTTPException(status_code=403, detail="Only drivers can request verification")

    result = await session.execute(
        select(Vehicle).where(Vehicle.driver_user_id == current_driver.id)
    )
    vehicle = result.scalar_one_or_none()

    if not vehicle:
        raise HTTPException(status_code=404, detail="No vehicle registered")

    vehicle.verification_status = VehicleVerificationStatus.PENDING
    vehicle.verification_requested_at = datetime.now(timezone.utc)

    await session.commit()
    await session.refresh(vehicle)

    return vehicle