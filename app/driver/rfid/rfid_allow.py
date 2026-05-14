# app/driver/rfid/rfid_allow.py

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_active_user, get_db
from app.db.schema import (
    PlatformSettings,
    RFIDDevice,
    User,
    Vehicle,
)
from app.driver.rfid.schemas import (
    DriverRFIDReservationSettingResponse,
)

router = APIRouter(
    prefix="/driver/rfid",
    tags=["Driver RFID"],
)


@router.get(
    "/allow-seat-reservation",
    response_model=DriverRFIDReservationSettingResponse,
)
async def get_driver_rfid_seat_reservation_setting(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    # ============================================================
    # FETCH PLATFORM SETTINGS
    # ============================================================
    stmt = select(PlatformSettings).where(
        PlatformSettings.settings_key == "default"
    )

    result = await db.execute(stmt)
    settings = result.scalar_one_or_none()

    # ============================================================
    # DEFAULT FALLBACK
    # ============================================================
    allow_driver_rfid_seat_reservation = True

    if settings is not None:
        allow_driver_rfid_seat_reservation = (
            settings.allow_driver_rfid_seat_reservation
        )

    # ============================================================
    # IF FEATURE DISABLED
    # ============================================================
    if not allow_driver_rfid_seat_reservation:
        return DriverRFIDReservationSettingResponse(
            allow_driver_rfid_seat_reservation=False,
            rfid_device_serial_number=None,
        )

    # ============================================================
    # FETCH DRIVER VEHICLE
    # ============================================================
    vehicle_stmt = select(Vehicle).where(
        Vehicle.driver_user_id == current_user.id
    )

    vehicle_result = await db.execute(vehicle_stmt)
    vehicle = vehicle_result.scalar_one_or_none()

    # ============================================================
    # IF DRIVER HAS NO VEHICLE
    # ============================================================
    if vehicle is None:
        return DriverRFIDReservationSettingResponse(
            allow_driver_rfid_seat_reservation=True,
            rfid_device_serial_number=None,
        )

    # ============================================================
    # FETCH RFID DEVICE
    # ============================================================
    device_stmt = (
        select(RFIDDevice)
        .where(
            RFIDDevice.vehicle_id == vehicle.id,
            RFIDDevice.is_active.is_(True),
        )
        .limit(1)
    )

    device_result = await db.execute(device_stmt)
    device = device_result.scalar_one_or_none()

    # ============================================================
    # RESPONSE
    # ============================================================
    return DriverRFIDReservationSettingResponse(
        allow_driver_rfid_seat_reservation=True,
        rfid_device_serial_number=(
            device.serial_number if device else None
        ),
    )