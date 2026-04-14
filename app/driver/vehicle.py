#app/driver/vehicle.py
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pathlib import Path
import shutil
from datetime import datetime, timezone
from app.notifications.service import NotificationService
from app.notifications.hub import WSHub
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, status, Request
import asyncio


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

UPLOAD_DIR = Path("uploads/upload_vehicledetails_photo")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# Helper: Parse + Validate Date
# ============================================================
def parse_registration_date(date_str: str) -> datetime:
    try:
        dt = datetime.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid date format. Use ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)"
        )

    # Ensure timezone aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    # Must be future date
    if dt <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=400,
            detail="Registration validity must be a future date"
        )

    return dt


# ---------------------------
# Register Vehicle
# ---------------------------
@router.patch("/register", response_model=VehicleOut, status_code=status.HTTP_201_CREATED)
async def register_vehicle(
    registration_number: str = Form(...),
    registration_valid_till: str = Form(...),
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
        raise HTTPException(status_code=403, detail="Only drivers allowed")

    # Parse Date
    registration_valid_till_dt = parse_registration_date(registration_valid_till)

    # KYC CHECK
    result = await session.execute(
        select(DriverProfile).where(DriverProfile.user_id == current_driver.id)
    )
    driver_profile = result.scalar_one_or_none()

    if not driver_profile:
        raise HTTPException(status_code=400, detail="Complete KYC first")

    if driver_profile.verification_status != DriverVerificationStatus.VERIFIED:
        raise HTTPException(status_code=403, detail="KYC not verified")

    # Check duplicate
    result = await session.execute(
        select(Vehicle).where(Vehicle.registration_number == registration_number)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Vehicle already exists")

    # Save files
    rc_filename = f"{registration_number}_rc_{rc_file.filename}"
    rear_filename = f"{registration_number}_rear_{rear_photo.filename}"

    rc_path = UPLOAD_DIR / rc_filename
    rear_path = UPLOAD_DIR / rear_filename

    with open(rc_path, "wb") as f:
        shutil.copyfileobj(rc_file.file, f)

    with open(rear_path, "wb") as f:
        shutil.copyfileobj(rear_photo.file, f)

    vehicle = Vehicle(
        driver_user_id=current_driver.id,
        registration_number=registration_number,
        registration_valid_till=registration_valid_till_dt,
        vehicle_name=vehicle_name,
        vehicle_model=vehicle_model,
        color=color,
        seat_count=seat_count,
        has_ac=has_ac,
        rc_file_path=str(rc_path),
        rear_photo_file_path=str(rear_path),
        verification_status=VehicleVerificationStatus.DRAFT,
    )

    session.add(vehicle)
    await session.commit()
    await session.refresh(vehicle)

    return vehicle


# ---------------------------
# View Vehicle
# ---------------------------
@router.get("/my-vehicle", response_model=VehicleOut)
async def get_my_vehicle(
    current_driver: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_async_session)
):
    result = await session.execute(
        select(Vehicle).where(Vehicle.driver_user_id == current_driver.id)
    )
    vehicle = result.scalar_one_or_none()

    if not vehicle:
        raise HTTPException(status_code=404, detail="No vehicle found")

    return vehicle   # ✅ registration_valid_till automatically returned


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
    registration_valid_till: str | None = Form(None),  # ✅ ADDED
    rc_file: UploadFile | None = File(None),
    rear_photo: UploadFile | None = File(None),
    current_driver: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_async_session)
):
    result = await session.execute(
        select(Vehicle).where(Vehicle.driver_user_id == current_driver.id)
    )
    vehicle = result.scalar_one_or_none()

    if not vehicle:
        raise HTTPException(status_code=404, detail="No vehicle found")

    # ❌ BLOCK UPDATE if submitted
    if vehicle.verification_status in [
        VehicleVerificationStatus.PENDING,
        VehicleVerificationStatus.VERIFIED
    ]:
        raise HTTPException(
            status_code=403,
            detail="Cannot update after submission. Wait for admin or rejection."
        )

    # If REJECTED → allow edit
    if vehicle.verification_status == VehicleVerificationStatus.REJECTED:
        vehicle.verification_status = VehicleVerificationStatus.DRAFT

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

    # Update registration validity
    if registration_valid_till is not None:
        vehicle.registration_valid_till = parse_registration_date(registration_valid_till)

    # Files
    if rc_file:
        rc_path = UPLOAD_DIR / f"{vehicle.registration_number}_rc_{rc_file.filename}"
        with open(rc_path, "wb") as f:
            shutil.copyfileobj(rc_file.file, f)
        vehicle.rc_file_path = str(rc_path)

    if rear_photo:
        rear_path = UPLOAD_DIR / f"{vehicle.registration_number}_rear_{rear_photo.filename}"
        with open(rear_path, "wb") as f:
            shutil.copyfileobj(rear_photo.file, f)
        vehicle.rear_photo_file_path = str(rear_path)

    vehicle.rejection_reason = None

    await session.commit()
    await session.refresh(vehicle)

    return vehicle


# ---------------------------
# SUBMIT VEHICLE
# ---------------------------
import asyncio

@router.post("/submit", response_model=VehicleOut)
async def submit_vehicle(
    request: Request,
    current_driver: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_async_session)
):
    # Get vehicle
    result = await session.execute(
        select(Vehicle).where(Vehicle.driver_user_id == current_driver.id)
    )
    vehicle = result.scalar_one_or_none()

    if not vehicle:
        raise HTTPException(status_code=404, detail="No vehicle found")

    # Only DRAFT or REJECTED allowed
    if vehicle.verification_status not in [
        VehicleVerificationStatus.DRAFT,
        VehicleVerificationStatus.REJECTED
    ]:
        raise HTTPException(
            status_code=400,
            detail="Vehicle already submitted or verified"
        )

    # Update status
    vehicle.verification_status = VehicleVerificationStatus.PENDING
    vehicle.verification_requested_at = datetime.now(timezone.utc)

    await session.commit()
    await session.refresh(vehicle)

    # ============================================================
    # 🔥 SEND NOTIFICATION TO ADMINS
    # ============================================================

    # Get WS Hub (safe)
    ws_hub = getattr(request.app.state, "ws_hub", None)
    if ws_hub is None:
        print("⚠️ WS Hub not initialized - real-time notifications disabled")

    notification_service = NotificationService(
        db=session,
        ws_hub=ws_hub
    )

    # Get all admins
    result = await session.execute(
        select(User).where(User.role == UserRole.ADMIN)
    )
    admins = result.scalars().all()

    # 🚀 Send notifications in parallel
    tasks = [
        notification_service.notify_user(
            user_id=admin.id,
            title="New Vehicle Submitted 🚗",
            message=f"Driver {current_driver.email} submitted vehicle {vehicle.registration_number}",
            data={
                "vehicle_id": vehicle.id,
                "driver_id": current_driver.id,
                "type": "VEHICLE_SUBMITTED"
            }
        )
        for admin in admins
    ]

    if tasks:
        await asyncio.gather(*tasks)

    return vehicle

# ---------------------------
# Get Inspection Status
# ---------------------------
@router.get("/inspection-status")
async def get_vehicle_inspection_status(
    current_driver: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_async_session)
):
    # Fetch vehicle
    result = await session.execute(
        select(Vehicle).where(Vehicle.driver_user_id == current_driver.id)
    )
    vehicle = result.scalar_one_or_none()

    if not vehicle:
        raise HTTPException(status_code=404, detail="No vehicle found")

    return {
        "vehicle_id": vehicle.id,
        "inspection_status": vehicle.inspection_status,
        "inspection_reason": vehicle.inspection_reason,
        "inspection_created_at": vehicle.inspection_created_at,
        "inspection_reviewed_at": vehicle.inspection_reviewed_at,
    }