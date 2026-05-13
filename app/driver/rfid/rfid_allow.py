# app/driver/rfid/rfid_allow.py

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_db
from app.db.schema import PlatformSettings
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
):
    stmt = select(PlatformSettings).limit(1)

    result = await db.execute(stmt)

    settings = result.scalar_one_or_none()

    # fallback if settings row not created
    if not settings:
        return DriverRFIDReservationSettingResponse(
            allow_driver_rfid_seat_reservation=False
        )

    return DriverRFIDReservationSettingResponse(
        allow_driver_rfid_seat_reservation=settings.allow_driver_rfid_seat_reservation
    )