# app/driver/rfid/rfid_allow.py

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_db
from app.db.schema import PlatformSettings
from app.driver.rfid.schemas import DriverRFIDReservationSettingResponse

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
    # ✅ Correct: always fetch the canonical settings row
    stmt = select(PlatformSettings).where(
        PlatformSettings.settings_key == "default"
    )

    result = await db.execute(stmt)
    settings = result.scalar_one_or_none()

    # ✅ Correct fallback: system default is TRUE
    if not settings:
        return DriverRFIDReservationSettingResponse(
            allow_driver_rfid_seat_reservation=True
        )

    return DriverRFIDReservationSettingResponse(
        allow_driver_rfid_seat_reservation=settings.allow_driver_rfid_seat_reservation
    )