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
    VehicleOwnershipType,
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

    ownership_type: str = Form(...),

    owner_name: str | None = Form(None),

    rc_file: UploadFile = File(...),
    rear_photo: UploadFile = File(...),
    authentication_file: UploadFile | None = File(None),

    front_photo: UploadFile | None = File(None),
    interior_photo: UploadFile | None = File(None),
    left_side_photo: UploadFile | None = File(None),
    right_side_photo: UploadFile | None = File(None),

    insurance_document: UploadFile | None = File(None),
    pollution_document: UploadFile | None = File(None),
    owner_aadhaar_card: UploadFile | None = File(None),

    current_driver: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_async_session)
):
    # ---------------------------
    # ROLE CHECK
    # ---------------------------
    if current_driver.role != UserRole.DRIVER:
        raise HTTPException(status_code=403, detail="Only drivers allowed")

    # ---------------------------
    # OWNERSHIP VALIDATION
    # ---------------------------
    try:
        ownership_enum = VehicleOwnershipType(ownership_type)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ownership_type")

    # 🔥 RENTED RULE
    if ownership_enum == VehicleOwnershipType.RENTED:
        if not authentication_file:
            raise HTTPException(400, "Authentication file required for rented vehicle")
        if not owner_name:
            raise HTTPException(400, "Owner name required for rented vehicle")
        if not owner_aadhaar_card:
            raise HTTPException(400, "Owner Aadhaar required for rented vehicle")

    # 🔥 SELF RULE
    if ownership_enum == VehicleOwnershipType.SELF:
        if authentication_file:
            raise HTTPException(400, "Authentication file not allowed for self vehicle")

    # ---------------------------
    # DATE PARSE
    # ---------------------------
    registration_valid_till_dt = parse_registration_date(registration_valid_till)

    # ---------------------------
    # KYC CHECK
    # ---------------------------
    result = await session.execute(
        select(DriverProfile).where(DriverProfile.user_id == current_driver.id)
    )
    driver_profile = result.scalar_one_or_none()

    if not driver_profile:
        raise HTTPException(400, "Complete KYC first")

    if driver_profile.verification_status != DriverVerificationStatus.VERIFIED:
        raise HTTPException(403, "KYC not verified")

    # ---------------------------
    # DUPLICATE CHECK
    # ---------------------------
    result = await session.execute(
        select(Vehicle).where(Vehicle.registration_number == registration_number)
    )
    if result.scalar_one_or_none():
        raise HTTPException(400, "Vehicle already exists")

    # ---------------------------
    # FILE SAVE HELPER
    # ---------------------------
    def save_file(file: UploadFile | None, prefix: str):
        if not file:
            return None
        filename = f"{registration_number}_{prefix}_{file.filename}"
        path = UPLOAD_DIR / filename
        with open(path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        return str(path)

    # ---------------------------
    # SAVE FILES
    # ---------------------------
    rc_path = save_file(rc_file, "rc")
    rear_path = save_file(rear_photo, "rear")

    auth_path = save_file(authentication_file, "auth")
    front_path = save_file(front_photo, "front")
    interior_path = save_file(interior_photo, "interior")
    left_path = save_file(left_side_photo, "left")
    right_path = save_file(right_side_photo, "right")

    insurance_path = save_file(insurance_document, "insurance")
    pollution_path = save_file(pollution_document, "pollution")
    aadhaar_path = save_file(owner_aadhaar_card, "aadhaar")

    # ---------------------------
    # CREATE VEHICLE
    # ---------------------------
    vehicle = Vehicle(
        driver_user_id=current_driver.id,
        registration_number=registration_number,
        registration_valid_till=registration_valid_till_dt,
        vehicle_name=vehicle_name,
        vehicle_model=vehicle_model,
        color=color,
        seat_count=seat_count,
        has_ac=has_ac,

        # existing
        rc_file_path=rc_path,
        rear_photo_file_path=rear_path,

        ownership_type=ownership_enum,
        authentication_file_path=auth_path,

        front_photo_file_path=front_path,
        interior_photo_file_path=interior_path,
        left_side_file_path=left_path,
        right_side_file_path=right_path,

        insurance_document=insurance_path,
        pollution_document=pollution_path,
        owner_aadhaar_card=aadhaar_path,

        owner_name=owner_name,

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
    # Only drivers allowed
    if current_driver.role != UserRole.DRIVER:
        raise HTTPException(status_code=403, detail="Only drivers allowed")

    # Fetch vehicle
    result = await session.execute(
        select(Vehicle).where(Vehicle.driver_user_id == current_driver.id)
    )
    vehicle = result.scalar_one_or_none()

    if not vehicle:
        raise HTTPException(status_code=404, detail="No vehicle found")

    return vehicle
# ---------------------------
# Update Vehicle
# ---------------------------
@router.patch("/update", response_model=VehicleOut)
async def update_vehicle(
    # ---------------------------
    # BASIC FIELDS
    # ---------------------------
    vehicle_name: str | None = Form(None),
    vehicle_model: str | None = Form(None),
    color: str | None = Form(None),
    seat_count: int | None = Form(None),
    has_ac: bool | None = Form(None),
    registration_valid_till: str | None = Form(None),

    # ---------------------------
    # OWNERSHIP
    # ---------------------------
    ownership_type: str | None = Form(None),
    owner_name: str | None = Form(None),

    # ---------------------------
    # FILES (existing)
    # ---------------------------
    rc_file: UploadFile | None = File(None),
    rear_photo: UploadFile | None = File(None),
    authentication_file: UploadFile | None = File(None),

    # ---------------------------
    # NEW VEHICLE IMAGES
    # ---------------------------
    front_photo: UploadFile | None = File(None),
    interior_photo: UploadFile | None = File(None),
    left_side_photo: UploadFile | None = File(None),
    right_side_photo: UploadFile | None = File(None),

    # ---------------------------
    # DOCUMENTS
    # ---------------------------
    insurance_document: UploadFile | None = File(None),
    pollution_document: UploadFile | None = File(None),
    owner_aadhaar_card: UploadFile | None = File(None),

    current_driver: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_async_session)
):
    # ---------------------------
    # FETCH VEHICLE
    # ---------------------------
    result = await session.execute(
        select(Vehicle).where(Vehicle.driver_user_id == current_driver.id)
    )
    vehicle = result.scalar_one_or_none()

    if not vehicle:
        raise HTTPException(404, "No vehicle found")

    # ---------------------------
    # STATUS CHECK
    # ---------------------------
    if vehicle.verification_status in [
        VehicleVerificationStatus.PENDING,
        VehicleVerificationStatus.VERIFIED
    ]:
        raise HTTPException(
            403,
            "Cannot update after submission. Wait for admin or rejection."
        )

    if vehicle.verification_status == VehicleVerificationStatus.REJECTED:
        vehicle.verification_status = VehicleVerificationStatus.DRAFT

    # ---------------------------
    # FILE SAVE HELPER
    # ---------------------------
    def save_file(file: UploadFile | None, prefix: str):
        if not file:
            return None
        filename = f"{vehicle.registration_number}_{prefix}_{file.filename}"
        path = UPLOAD_DIR / filename
        with open(path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        return str(path)

    # ---------------------------
    # UPDATE BASIC FIELDS
    # ---------------------------
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

    if registration_valid_till is not None:
        vehicle.registration_valid_till = parse_registration_date(registration_valid_till)

    # ---------------------------
    # OWNERSHIP UPDATE
    # ---------------------------
    if ownership_type is not None:
        try:
            vehicle.ownership_type = VehicleOwnershipType(ownership_type)
        except ValueError:
            raise HTTPException(400, "Invalid ownership_type")

    if owner_name is not None:
        vehicle.owner_name = owner_name

    # ---------------------------
    # FILE UPDATES
    # ---------------------------
    if rc_file:
        vehicle.rc_file_path = save_file(rc_file, "rc")

    if rear_photo:
        vehicle.rear_photo_file_path = save_file(rear_photo, "rear")

    if authentication_file:
        vehicle.authentication_file_path = save_file(authentication_file, "auth")

    # ✅ vehicle images
    if front_photo:
        vehicle.front_photo_file_path = save_file(front_photo, "front")

    if interior_photo:
        vehicle.interior_photo_file_path = save_file(interior_photo, "interior")

    if left_side_photo:
        vehicle.left_side_file_path = save_file(left_side_photo, "left")

    if right_side_photo:
        vehicle.right_side_file_path = save_file(right_side_photo, "right")

    # ✅ documents
    if insurance_document:
        vehicle.insurance_document = save_file(insurance_document, "insurance")

    if pollution_document:
        vehicle.pollution_document = save_file(pollution_document, "pollution")

    if owner_aadhaar_card:
        vehicle.owner_aadhaar_card = save_file(owner_aadhaar_card, "aadhaar")

    # ---------------------------
    # 🔥 FINAL BUSINESS VALIDATION
    # ---------------------------
    if vehicle.ownership_type == VehicleOwnershipType.RENTED:
        if not vehicle.owner_name:
            raise HTTPException(400, "Owner name required for rented vehicle")
        if not vehicle.owner_aadhaar_card:
            raise HTTPException(400, "Owner Aadhaar required for rented vehicle")
        if not vehicle.authentication_file_path:
            raise HTTPException(400, "Authentication file required for rented vehicle")

    # ---------------------------
    # CLEANUP
    # ---------------------------
    vehicle.rejection_reason = None

    await session.commit()
    await session.refresh(vehicle)

    return vehicle

# ---------------------------
# SUBMIT VEHICLE
# ---------------------------
@router.post("/submit", response_model=VehicleOut)
async def submit_vehicle(
    request: Request,
    current_driver: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_async_session)
):
    # ---------------------------
    # FETCH VEHICLE
    # ---------------------------
    result = await session.execute(
        select(Vehicle).where(Vehicle.driver_user_id == current_driver.id)
    )
    vehicle = result.scalar_one_or_none()

    if not vehicle:
        raise HTTPException(status_code=404, detail="No vehicle found")

    # ---------------------------
    # STATUS CHECK
    # ---------------------------
    if vehicle.verification_status not in [
        VehicleVerificationStatus.DRAFT,
        VehicleVerificationStatus.REJECTED
    ]:
        raise HTTPException(
            status_code=400,
            detail="Vehicle already submitted or verified"
        )

    # ---------------------------
    # 🔥 VALIDATION BEFORE SUBMIT
    # ---------------------------

    # Required base files
    if not vehicle.rc_file_path:
        raise HTTPException(400, "RC file is required before submission")

    if not vehicle.rear_photo_file_path:
        raise HTTPException(400, "Rear photo is required before submission")

    # Recommended vehicle images
    if not vehicle.front_photo_file_path:
        raise HTTPException(400, "Front photo is required")

    if not vehicle.left_side_file_path:
        raise HTTPException(400, "Left side photo is required")

    # Ownership-based validation
    if vehicle.ownership_type == VehicleOwnershipType.RENTED:
        if not vehicle.owner_name:
            raise HTTPException(400, "Owner name required for rented vehicle")

        if not vehicle.owner_aadhaar_card:
            raise HTTPException(400, "Owner Aadhaar required for rented vehicle")

        if not vehicle.authentication_file_path:
            raise HTTPException(400, "Authentication file required for rented vehicle")

    # Documents validation
    if not vehicle.insurance_document:
        raise HTTPException(400, "Insurance document required")

    if not vehicle.pollution_document:
        raise HTTPException(400, "Pollution document required")

    # ---------------------------
    # UPDATE STATUS
    # ---------------------------
    vehicle.verification_status = VehicleVerificationStatus.PENDING
    vehicle.verification_requested_at = datetime.now(timezone.utc)

    await session.commit()
    await session.refresh(vehicle)

    # ---------------------------
    # 🔔 SEND NOTIFICATIONS
    # ---------------------------
    ws_hub = getattr(request.app.state, "ws_hub", None)
    if ws_hub is None:
        print("⚠️ WS Hub not initialized - real-time notifications disabled")

    notification_service = NotificationService(
        db=session,
        ws_hub=ws_hub
    )

    # Get admins
    result = await session.execute(
        select(User).where(User.role == UserRole.ADMIN)
    )
    admins = result.scalars().all()

    # Send notifications in parallel
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