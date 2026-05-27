# app/driver/rfid/rfid_allow.py

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_active_user, get_db
from app.db.schema import (
    PlatformSettings,
    RFIDDevice,
    Vehicle,
    User,
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

    allow_driver_rfid_seat_reservation = True
    if settings is not None:
        allow_driver_rfid_seat_reservation = (
            settings.allow_driver_rfid_seat_reservation
        )

    # ============================================================
    # FETCH DRIVER VEHICLE
    # ============================================================
    vehicle_stmt = select(Vehicle).where(
        Vehicle.driver_user_id == current_user.id
    )

    vehicle_result = await db.execute(vehicle_stmt)
    vehicle = vehicle_result.scalar_one_or_none()

    rfid_serial_number = None

    # ============================================================
    # FETCH RFID DEVICE (ONLY IF VEHICLE EXISTS)
    # ============================================================
    if vehicle is not None:
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

        if device:
            rfid_serial_number = device.serial_number

    # ============================================================
    # RESPONSE (IMPORTANT CHANGE HERE)
    # ============================================================
    return DriverRFIDReservationSettingResponse(
        allow_driver_rfid_seat_reservation=allow_driver_rfid_seat_reservation,
        rfid_device_serial_number=rfid_serial_number,
    )