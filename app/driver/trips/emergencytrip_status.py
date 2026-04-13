#app/driver/trips/emergencytrip_status.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_async_session
from app.auth.dependencies import get_current_user

from app.db.schema import (
    User,
    ScheduledTrip,
    EmergencyStopRequestStatus,
)

router = APIRouter()


@router.get("/{trip_id}/emergency-status")
async def get_emergency_status(
    trip_id: str,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    # -----------------------------
    # 1. Validate trip
    # -----------------------------
    trip = await session.get(ScheduledTrip, trip_id)

    if not trip:
        raise HTTPException(
            status_code=404,
            detail="Trip not found"
        )

    if trip.driver_user_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="You are not authorized for this trip"
        )

    # -----------------------------
    # 2. Extract status
    # -----------------------------
    emergency_status = trip.emergency_stop_request_status
    trip_status = trip.status

    # -----------------------------
    # 3. Response
    # -----------------------------
    return {
        "trip_id": trip.id,
        "emergency_status": emergency_status.value if emergency_status else None,
        "trip_status": trip_status.value if trip_status else None,
    }