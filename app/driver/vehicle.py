#app/driver/vehicle.py
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pathlib import Path
import shutil
from datetime import datetime, timezone
from app.notifications.service import NotificationService
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, status, Request
from app.db.schema import PlatformSettings

from app.db.database import get_async_session
from app.db.schema import (
    Vehicle,
    VehicleVerificationStatus,
    User,
    UserRole,
    DriverProfile,
    VehicleOwnershipType,
    VehicleInspectionStatus,
    DriverVerificationStatus
)
from app.driver.schemas import VehicleOut
from app.auth.dependencies import get_current_active_user
from app.driver.validators import validate_registration_number
from app.realtime.events import get_api_refresh_hub, publish_admin_event

router = APIRouter(prefix="/driver/vehicle", tags=["Driver Vehicle"])

UPLOAD_DIR = Path("uploads/upload_vehicledetails_photo")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


async def emit_admin_vehicle_refresh(
    request: Request,
    *,
    driver_user_id: str,
    vehicle_id: str,
    reason: str,
) -> None:
    await publish_admin_event(
        get_api_refresh_hub(request.app),
        event="admin.vehicles_changed",
        data={
            "driver_user_id": driver_user_id,
            "vehicle_id": vehicle_id,
            "reason": reason,
        },
    )


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
@router.patch(
    "/register",
    response_model=VehicleOut,
    status_code=status.HTTP_201_CREATED
)
async def register_vehicle(
    request: Request,
    registration_number: str = Form(...),
    registration_valid_till: str = Form(...),

    vehicle_name: str = Form(...),
    vehicle_model: str = Form(...),
    color: str = Form(...),

    seat_count: int = Form(...),

    # ✅ RFID RESERVED FIELD
    default_rfid_reserved_seat_count: int = Form(0),

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
        raise HTTPException(
            status_code=403,
            detail="Only drivers allowed"
        )

    registration_number = validate_registration_number(
        registration_number
    )

    # ---------------------------
    # PLATFORM SETTINGS
    # ---------------------------
    settings_result = await session.execute(
        select(PlatformSettings).where(
            PlatformSettings.settings_key == "default"
        )
    )

    platform_settings = (
        settings_result.scalar_one_or_none()
    )

    allow_driver_rfid_seat_reservation = True

    if platform_settings:
        allow_driver_rfid_seat_reservation = (
            platform_settings.allow_driver_rfid_seat_reservation
        )

    # ---------------------------
    # BASIC VALIDATION
    # ---------------------------
    if seat_count <= 0:
        raise HTTPException(
            status_code=400,
            detail="Seat count must be greater than 0"
        )

    # ---------------------------
    # RFID FEATURE CONTROL
    # ---------------------------
    if not allow_driver_rfid_seat_reservation:
        default_rfid_reserved_seat_count = 0

    # ---------------------------
    # RFID RESERVED VALIDATION
    # ---------------------------
    if allow_driver_rfid_seat_reservation:

        if default_rfid_reserved_seat_count < 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Reserved RFID seat count "
                    "cannot be negative"
                )
            )

        if (
            default_rfid_reserved_seat_count
            > seat_count
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Reserved RFID seat count "
                    "cannot exceed total seat count"
                )
            )

    else:
        default_rfid_reserved_seat_count = 0

    # ---------------------------
    # OWNERSHIP VALIDATION
    # ---------------------------
    try:
        ownership_enum = VehicleOwnershipType(
            ownership_type
        )

    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid ownership_type"
        )

    # ---------------------------
    # RENTED RULE
    # ---------------------------
    if ownership_enum == VehicleOwnershipType.RENTED:

        if not authentication_file:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Authentication file required "
                    "for rented vehicle"
                )
            )

        if not owner_name:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Owner name required "
                    "for rented vehicle"
                )
            )

        if not owner_aadhaar_card:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Owner Aadhaar required "
                    "for rented vehicle"
                )
            )

    # ---------------------------
    # SELF RULE
    # ---------------------------
    if ownership_enum == VehicleOwnershipType.SELF:

        if authentication_file:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Authentication file not allowed "
                    "for self vehicle"
                )
            )

    # ---------------------------
    # DATE PARSE
    # ---------------------------
    registration_valid_till_dt = (
        parse_registration_date(
            registration_valid_till
        )
    )

    # ---------------------------
    # KYC CHECK
    # ---------------------------
    result = await session.execute(
        select(DriverProfile).where(
            DriverProfile.user_id
            == current_driver.id
        )
    )

    driver_profile = (
        result.scalar_one_or_none()
    )

    if not driver_profile:
        raise HTTPException(
            status_code=400,
            detail="Complete KYC first"
        )

    if (
        driver_profile.verification_status
        != DriverVerificationStatus.VERIFIED
    ):
        raise HTTPException(
            status_code=403,
            detail="KYC not verified"
        )

    # ---------------------------
    # EXISTING VEHICLE CHECK
    # ---------------------------
    result = await session.execute(
        select(Vehicle).where(
            Vehicle.driver_user_id
            == current_driver.id
        )
    )

    existing_vehicle = (
        result.scalar_one_or_none()
    )

    # ---------------------------
    # BLOCK NON-DRAFT VEHICLES
    # ---------------------------
    if existing_vehicle:

        if existing_vehicle.verification_status in [
            VehicleVerificationStatus.PENDING,
            VehicleVerificationStatus.VERIFIED,
        ]:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Vehicle already submitted "
                    "or verified"
                )
            )

    # ---------------------------
    # FILE SAVE HELPER
    # ---------------------------
    def save_file(
        file: UploadFile | None,
        prefix: str
    ):
        if not file:
            return None

        allowed_extensions = {
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".avif"
#           ".pdf"
        }

        extension = (
            Path(file.filename)
            .suffix
            .lower()
        )

        if extension not in allowed_extensions:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported file format: "
                    f"{extension}"
                )
            )

        filename = (
            f"{registration_number}_"
            f"{prefix}_"
            f"{file.filename}"
        )

        path = UPLOAD_DIR / filename

        with open(path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        return str(path)

    # ---------------------------
    # SAVE FILES
    # ---------------------------
    rc_path = save_file(
        rc_file,
        "rc"
    )

    rear_path = save_file(
        rear_photo,
        "rear"
    )

    auth_path = save_file(
        authentication_file,
        "auth"
    )

    front_path = save_file(
        front_photo,
        "front"
    )

    interior_path = save_file(
        interior_photo,
        "interior"
    )

    left_path = save_file(
        left_side_photo,
        "left"
    )

    right_path = save_file(
        right_side_photo,
        "right"
    )

    insurance_path = save_file(
        insurance_document,
        "insurance"
    )

    pollution_path = save_file(
        pollution_document,
        "pollution"
    )

    aadhaar_path = save_file(
        owner_aadhaar_card,
        "aadhaar"
    )

    # ============================================================
    # UPDATE EXISTING DRAFT / REJECTED VEHICLE
    # ============================================================
    if existing_vehicle:

        existing_vehicle.registration_number = (
            registration_number
        )

        existing_vehicle.registration_valid_till = (
            registration_valid_till_dt
        )

        existing_vehicle.vehicle_name = (
            vehicle_name
        )

        existing_vehicle.vehicle_model = (
            vehicle_model
        )

        existing_vehicle.color = color

        existing_vehicle.seat_count = seat_count

        existing_vehicle.default_rfid_reserved_seat_count = (
            default_rfid_reserved_seat_count
        )

        existing_vehicle.has_ac = has_ac

        existing_vehicle.ownership_type = (
            ownership_enum
        )

        existing_vehicle.owner_name = owner_name

        # ---------------------------
        # FILES
        # ---------------------------
        existing_vehicle.rc_file_path = rc_path

        existing_vehicle.rear_photo_file_path = (
            rear_path
        )

        existing_vehicle.authentication_file_path = (
            auth_path
        )

        existing_vehicle.front_photo_file_path = (
            front_path
        )

        existing_vehicle.interior_photo_file_path = (
            interior_path
        )

        existing_vehicle.left_side_file_path = (
            left_path
        )

        existing_vehicle.right_side_file_path = (
            right_path
        )

        existing_vehicle.insurance_document = (
            insurance_path
        )

        existing_vehicle.pollution_document = (
            pollution_path
        )

        existing_vehicle.owner_aadhaar_card = (
            aadhaar_path
        )

        # ---------------------------
        # RESET REJECTION
        # ---------------------------
        existing_vehicle.rejection_reason = None

        existing_vehicle.verification_status = (
            VehicleVerificationStatus.DRAFT
        )

        session.add(existing_vehicle)

        await session.commit()

        await session.refresh(
            existing_vehicle
        )

        await emit_admin_vehicle_refresh(
            request,
            driver_user_id=current_driver.id,
            vehicle_id=existing_vehicle.id,
            reason="driver_vehicle_registration_updated",
        )
        return existing_vehicle

    # ---------------------------
    # CREATE VEHICLE
    # ---------------------------
    vehicle = Vehicle(
        driver_user_id=current_driver.id,

        registration_number=registration_number,

        registration_valid_till=(
            registration_valid_till_dt
        ),

        vehicle_name=vehicle_name,
        vehicle_model=vehicle_model,
        color=color,

        seat_count=seat_count,

        # ✅ RFID RESERVED SEATS
        default_rfid_reserved_seat_count=(
            default_rfid_reserved_seat_count
        ),

        has_ac=has_ac,

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

        verification_status=(
            VehicleVerificationStatus.DRAFT
        ),
    )

    session.add(vehicle)

    await session.commit()

    await session.refresh(vehicle)

    await emit_admin_vehicle_refresh(
        request,
        driver_user_id=current_driver.id,
        vehicle_id=vehicle.id,
        reason="driver_vehicle_registered",
    )
    return vehicle
# ---------------------------
# View Vehicle
# ---------------------------
@router.get("/my-vehicle", response_model=VehicleOut)
async def get_my_vehicle(
    current_driver: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_async_session)
):
    # ---------------------------
    # ROLE CHECK
    # ---------------------------
    if current_driver.role != UserRole.DRIVER:
        raise HTTPException(
            status_code=403,
            detail="Only drivers allowed"
        )

    # ---------------------------
    # FETCH VEHICLE
    # ---------------------------
    result = await session.execute(
        select(Vehicle).where(
            Vehicle.driver_user_id == current_driver.id
        )
    )

    vehicle = result.scalar_one_or_none()

    if not vehicle:
        raise HTTPException(
            status_code=404,
            detail="No vehicle found"
        )

    # ---------------------------
    # PLATFORM SETTINGS
    # ---------------------------
    settings_result = await session.execute(
        select(PlatformSettings).where(
            PlatformSettings.settings_key == "default"
        )
    )

    platform_settings = (
        settings_result.scalar_one_or_none()
    )

    allow_driver_rfid_seat_reservation = True

    if platform_settings:
        allow_driver_rfid_seat_reservation = (
            platform_settings.allow_driver_rfid_seat_reservation
        )

    # ---------------------------
    # RFID RESERVED SEAT SAFETY
    # ---------------------------
    if not allow_driver_rfid_seat_reservation:

        # 🔥 Force disable RFID seats
        vehicle.default_rfid_reserved_seat_count = 0

    else:

        if (
            vehicle.default_rfid_reserved_seat_count
            < 0
        ):
            vehicle.default_rfid_reserved_seat_count = 0

        if (
            vehicle.default_rfid_reserved_seat_count
            > vehicle.seat_count
        ):
            vehicle.default_rfid_reserved_seat_count = (
                vehicle.seat_count
            )

    return vehicle
# ---------------------------
# Update Vehicle
# ---------------------------
@router.patch("/update", response_model=VehicleOut)
async def update_vehicle(
    request: Request,
    # ---------------------------
    # BASIC FIELDS
    # ---------------------------
    vehicle_name: str | None = Form(None),
    vehicle_model: str | None = Form(None),
    color: str | None = Form(None),
    seat_count: int | None = Form(None),

    # ✅ RFID RESERVED FIELD
    default_rfid_reserved_seat_count: int | None = Form(None),

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
        select(Vehicle).where(
            Vehicle.driver_user_id == current_driver.id
        )
    )

    vehicle = result.scalar_one_or_none()

    if not vehicle:
        raise HTTPException(
            status_code=404,
            detail="No vehicle found"
        )

    # ---------------------------
    # PLATFORM SETTINGS
    # ---------------------------
    settings_result = await session.execute(
        select(PlatformSettings).where(
            PlatformSettings.settings_key == "default"
        )
    )

    platform_settings = (
        settings_result.scalar_one_or_none()
    )

    allow_driver_rfid_seat_reservation = True

    if platform_settings:
        allow_driver_rfid_seat_reservation = (
            platform_settings.allow_driver_rfid_seat_reservation
        )

    # ---------------------------
    # STATUS CHECK
    # ---------------------------

    # ❌ Block while verification pending
    if (
        vehicle.verification_status
        == VehicleVerificationStatus.PENDING
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "Vehicle verification is pending."
            )
        )

    # ❌ Fully locked after final approval
    if (
        vehicle.verification_status
        == VehicleVerificationStatus.VERIFIED
        and vehicle.inspection_status
        == VehicleInspectionStatus.APPROVED
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "Vehicle already fully approved. "
                "Updates are locked."
            )
        )

    # ✅ Allow update after rejection
    if (
        vehicle.verification_status
        == VehicleVerificationStatus.REJECTED
        or vehicle.inspection_status
        == VehicleInspectionStatus.REJECTED
    ):

        # reset verification cycle
        vehicle.verification_status = (
            VehicleVerificationStatus.DRAFT
        )

        vehicle.rejection_reason = None

        # reset inspection cycle
        vehicle.inspection_status = None
        vehicle.inspection_reason = None
        vehicle.inspection_created_at = None
        vehicle.inspection_reviewed_at = None

    # ---------------------------
    # FILE SAVE HELPER
    # ---------------------------
    def save_file(
        file: UploadFile | None,
        prefix: str
    ):
        if not file:
            return None

        allowed_extensions = {
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".avif",
#           ".pdf"
        }

        extension = (
            Path(file.filename)
            .suffix
            .lower()
        )

        if extension not in allowed_extensions:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported file format: "
                    f"{extension}"
                )
            )

        filename = (
            f"{vehicle.registration_number}_"
            f"{prefix}_"
            f"{file.filename}"
        )

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

    # ---------------------------
    # SEAT COUNT VALIDATION
    # ---------------------------
    if seat_count is not None:

        if seat_count <= 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Seat count must be "
                    "greater than 0"
                )
            )

        if allow_driver_rfid_seat_reservation:

            if (
                vehicle.default_rfid_reserved_seat_count
                > seat_count
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Seat count cannot be less "
                        "than reserved RFID seats"
                    )
                )

        vehicle.seat_count = seat_count

    # ---------------------------
    # RFID FEATURE CONTROL
    # ---------------------------
    if not allow_driver_rfid_seat_reservation:

        vehicle.default_rfid_reserved_seat_count = 0

    # ---------------------------
    # RFID RESERVED VALIDATION
    # ---------------------------
    if allow_driver_rfid_seat_reservation:

        if (
            default_rfid_reserved_seat_count
            is not None
        ):

            if (
                default_rfid_reserved_seat_count
                < 0
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Reserved RFID seat count "
                        "cannot be negative"
                    )
                )

            effective_seat_count = (
                seat_count
                if seat_count is not None
                else vehicle.seat_count
            )

            if (
                default_rfid_reserved_seat_count
                > effective_seat_count
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Reserved RFID seat count "
                        "cannot exceed total seat count"
                    )
                )

            vehicle.default_rfid_reserved_seat_count = (
                default_rfid_reserved_seat_count
            )

    else:

        vehicle.default_rfid_reserved_seat_count = 0

    # ---------------------------
    # OTHER FIELDS
    # ---------------------------
    if has_ac is not None:
        vehicle.has_ac = has_ac

    if registration_valid_till is not None:
        vehicle.registration_valid_till = (
            parse_registration_date(
                registration_valid_till
            )
        )

    # ---------------------------
    # OWNERSHIP UPDATE
    # ---------------------------
    if ownership_type is not None:

        try:
            vehicle.ownership_type = (
                VehicleOwnershipType(
                    ownership_type
                )
            )

        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid ownership_type"
            )

    if owner_name is not None:
        vehicle.owner_name = owner_name

    # ---------------------------
    # FILE UPDATES
    # ---------------------------
    if rc_file:
        vehicle.rc_file_path = save_file(
            rc_file,
            "rc"
        )

    if rear_photo:
        vehicle.rear_photo_file_path = save_file(
            rear_photo,
            "rear"
        )

    if authentication_file:
        vehicle.authentication_file_path = (
            save_file(
                authentication_file,
                "auth"
            )
        )

    # ---------------------------
    # VEHICLE IMAGES
    # ---------------------------
    if front_photo:
        vehicle.front_photo_file_path = (
            save_file(
                front_photo,
                "front"
            )
        )

    if interior_photo:
        vehicle.interior_photo_file_path = (
            save_file(
                interior_photo,
                "interior"
            )
        )

    if left_side_photo:
        vehicle.left_side_file_path = (
            save_file(
                left_side_photo,
                "left"
            )
        )

    if right_side_photo:
        vehicle.right_side_file_path = (
            save_file(
                right_side_photo,
                "right"
            )
        )

    # ---------------------------
    # DOCUMENTS
    # ---------------------------
    if insurance_document:
        vehicle.insurance_document = (
            save_file(
                insurance_document,
                "insurance"
            )
        )

    if pollution_document:
        vehicle.pollution_document = (
            save_file(
                pollution_document,
                "pollution"
            )
        )

    if owner_aadhaar_card:
        vehicle.owner_aadhaar_card = (
            save_file(
                owner_aadhaar_card,
                "aadhaar"
            )
        )

    # ---------------------------
    # FINAL BUSINESS VALIDATION
    # ---------------------------
    if (
        vehicle.ownership_type
        == VehicleOwnershipType.RENTED
    ):

        if not vehicle.owner_name:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Owner name required "
                    "for rented vehicle"
                )
            )

        if not vehicle.owner_aadhaar_card:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Owner Aadhaar required "
                    "for rented vehicle"
                )
            )

        if not vehicle.authentication_file_path:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Authentication file required "
                    "for rented vehicle"
                )
            )

    # ---------------------------
    # CLEANUP
    # ---------------------------
    vehicle.rejection_reason = None

    await session.commit()
    await session.refresh(vehicle)

    await emit_admin_vehicle_refresh(
        request,
        driver_user_id=current_driver.id,
        vehicle_id=vehicle.id,
        reason="driver_vehicle_updated",
    )
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
    # ROLE CHECK
    # ---------------------------
    if current_driver.role != UserRole.DRIVER:
        raise HTTPException(
            status_code=403,
            detail="Only drivers allowed"
        )

    # ---------------------------
    # FETCH VEHICLE
    # ---------------------------
    result = await session.execute(
        select(Vehicle).where(
            Vehicle.driver_user_id == current_driver.id
        )
    )

    vehicle = result.scalar_one_or_none()

    if not vehicle:
        raise HTTPException(
            status_code=404,
            detail="No vehicle found"
        )

    # ---------------------------
    # PLATFORM SETTINGS
    # ---------------------------
    settings_result = await session.execute(
        select(PlatformSettings).where(
            PlatformSettings.settings_key == "default"
        )
    )

    platform_settings = (
        settings_result.scalar_one_or_none()
    )

    allow_driver_rfid_seat_reservation = True

    if platform_settings:
        allow_driver_rfid_seat_reservation = (
            platform_settings.allow_driver_rfid_seat_reservation
        )

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
    # BASIC VALIDATION
    # ---------------------------
    if vehicle.seat_count <= 0:
        raise HTTPException(
            status_code=400,
            detail="Seat count must be greater than 0"
        )

    # ---------------------------
    # RFID FEATURE CONTROL
    # ---------------------------
    if not allow_driver_rfid_seat_reservation:

        # 🔥 Force disable RFID seats
        vehicle.default_rfid_reserved_seat_count = 0

    # ---------------------------
    # RFID RESERVED VALIDATION
    # ---------------------------
    if (
        vehicle.default_rfid_reserved_seat_count
        < 0
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Reserved RFID seat count "
                "cannot be negative"
            )
        )

    if (
        vehicle.default_rfid_reserved_seat_count
        > vehicle.seat_count
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Reserved RFID seat count "
                "cannot exceed total seat count"
            )
        )

    # ---------------------------
    # VALIDATION BEFORE SUBMIT
    # ---------------------------
    if not vehicle.rc_file_path:
        raise HTTPException(
            status_code=400,
            detail="RC file is required before submission"
        )

    if not vehicle.rear_photo_file_path:
        raise HTTPException(
            status_code=400,
            detail="Rear photo is required before submission"
        )

    if not vehicle.front_photo_file_path:
        raise HTTPException(
            status_code=400,
            detail="Front photo is required"
        )

    if not vehicle.left_side_file_path:
        raise HTTPException(
            status_code=400,
            detail="Left side photo is required"
        )

    # ---------------------------
    # RENTED VEHICLE VALIDATION
    # ---------------------------
    if (
        vehicle.ownership_type
        == VehicleOwnershipType.RENTED
    ):

        if not vehicle.owner_name:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Owner name required "
                    "for rented vehicle"
                )
            )

        if not vehicle.owner_aadhaar_card:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Owner Aadhaar required "
                    "for rented vehicle"
                )
            )

        if not vehicle.authentication_file_path:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Authentication file required "
                    "for rented vehicle"
                )
            )

    # ---------------------------
    # REQUIRED DOCUMENTS
    # ---------------------------
    if not vehicle.insurance_document:
        raise HTTPException(
            status_code=400,
            detail="Insurance document required"
        )

    if not vehicle.pollution_document:
        raise HTTPException(
            status_code=400,
            detail="Pollution document required"
        )

    # ---------------------------
    # UPDATE STATUS
    # ---------------------------
    vehicle.verification_status = (
        VehicleVerificationStatus.PENDING
    )

    vehicle.verification_requested_at = (
        datetime.now(timezone.utc)
    )

    await session.commit()
    await session.refresh(vehicle)

    # ---------------------------
    # SEND NOTIFICATION
    # ---------------------------
    ws_hub = getattr(
        request.app.state,
        "ws_hub",
        None
    )

    if ws_hub is None:
        print(
            "⚠️ WS Hub not initialized - "
            "real-time notifications disabled"
        )

    notification_service = NotificationService(
        db=session,
        ws_hub=ws_hub
    )

    # ---------------------------
    # GET ACTIVE ADMINS
    # ---------------------------
    result = await session.execute(
        select(User.id).where(
            User.role == UserRole.ADMIN,
            User.is_active.is_(True),
        )
    )

    admin_user_ids = list(
        result.scalars().all()
    )

    # ---------------------------
    # SEND ADMIN NOTIFICATION
    # ---------------------------
    if admin_user_ids:

        await notification_service.notify_user(
            user_id=admin_user_ids[0],
            user_ids=admin_user_ids[1:],

            title="New Vehicle Submitted 🚗",

            message=(
                f"Driver {current_driver.email} "
                f"submitted vehicle "
                f"{vehicle.registration_number}"
            ),

            data={
                "vehicle_id": vehicle.id,
                "driver_id": current_driver.id,
                "type": "VEHICLE_SUBMITTED",
            },
        )

    await emit_admin_vehicle_refresh(
        request,
        driver_user_id=current_driver.id,
        vehicle_id=vehicle.id,
        reason="driver_vehicle_submitted",
    )
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
