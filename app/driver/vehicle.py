from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pathlib import Path
import shutil
from datetime import datetime, timezone

from app.db.database import get_async_session
from app.db.schema import Vehicle, VehicleVerificationStatus, User, UserRole
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

    driver_user_id = current_driver.id

    # Check for duplicate registration number
    result = await session.execute(select(Vehicle).where(Vehicle.registration_number == registration_number))
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

    # Save vehicle in DB with relative paths
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

    result = await session.execute(select(Vehicle).where(Vehicle.driver_user_id == current_driver.id))
    vehicle = result.scalar_one_or_none()
    if not vehicle:
        raise HTTPException(status_code=404, detail="No vehicle registered")
    return vehicle


# ---------------------------
# Update Vehicle
# ---------------------------
@router.patch("/update/{vehicle_id}", response_model=VehicleOut)
async def update_vehicle(
    vehicle_id: str,
    vehicle_name: str = Form(...),
    vehicle_model: str = Form(...),
    color: str = Form(...),
    seat_count: int = Form(...),
    has_ac: bool = Form(...),
    current_driver: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_async_session)
):
    if current_driver.role != UserRole.DRIVER:
        raise HTTPException(status_code=403, detail="Only drivers can update vehicles")

    result = await session.execute(
        select(Vehicle).where(Vehicle.id == vehicle_id, Vehicle.driver_user_id == current_driver.id)
    )
    vehicle = result.scalar_one_or_none()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found or not owned by driver")

    vehicle.vehicle_name = vehicle_name
    vehicle.vehicle_model = vehicle_model
    vehicle.color = color
    vehicle.seat_count = seat_count
    vehicle.has_ac = has_ac
    vehicle.verification_status = VehicleVerificationStatus.DRAFT
    vehicle.rejection_reason = None

    await session.commit()
    await session.refresh(vehicle)
    return vehicle


# ---------------------------
# Request Verification
# ---------------------------
@router.post("/request-verification/{vehicle_id}", response_model=VehicleOut)
async def request_vehicle_verification(
    vehicle_id: str,
    current_driver: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_async_session)
):
    if current_driver.role != UserRole.DRIVER:
        raise HTTPException(status_code=403, detail="Only drivers can request verification")

    result = await session.execute(
        select(Vehicle).where(Vehicle.id == vehicle_id, Vehicle.driver_user_id == current_driver.id)
    )
    vehicle = result.scalar_one_or_none()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found or not owned by driver")

    vehicle.verification_status = VehicleVerificationStatus.PENDING
    vehicle.verification_requested_at = datetime.now(timezone.utc)

    await session.commit()
    await session.refresh(vehicle)
    return vehicle