# app/driver/trips/current_trip.py

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.db.database import get_async_session
from app.auth.dependencies import get_current_user
from app.db.schema import (
    ScheduledTrip,
    ScheduledTripStatus,
    User,
    UserRole,
)

router = APIRouter(prefix="/driver/trips", tags=["Driver Trips"])


# ============================================================
# DRIVER - GET CURRENT TRIP
# ============================================================
@router.get("/current")
async def get_current_trip(
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """
    Returns the current trip for the driver.
    Only one trip should exist in SCHEDULED / IN_PROGRESS state.
    """

    # ---------------------------
    # Role check
    # ---------------------------
    if current_user.role != UserRole.DRIVER:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "driver_only",
                "message": "Only drivers can access this endpoint.",
            },
        )

    # ---------------------------
    # Fetch current trip
    # ---------------------------
    stmt = (
        select(ScheduledTrip)
        .where(
            ScheduledTrip.driver_user_id == current_user.id,
            ScheduledTrip.status.in_(
                [
                    ScheduledTripStatus.SCHEDULED,
                    ScheduledTripStatus.IN_PROGRESS,
                ]
            ),
        )
        .options(
            selectinload(ScheduledTrip.route),
            selectinload(ScheduledTrip.vehicle),
            selectinload(ScheduledTrip.driver),
        )
        .order_by(ScheduledTrip.planned_start_at.asc())
        .limit(1)
    )

    result = await session.execute(stmt)
    trip = result.scalar_one_or_none()

    if trip is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "no_active_trip",
                "message": "No current trip found for this driver.",
            },
        )

    # ---------------------------
    # Response
    # ---------------------------
    return {
        "trip": {
            "id": trip.id,
            "route_id": trip.route_id,
            "vehicle_id": trip.vehicle_id,
            "driver_user_id": trip.driver_user_id,
            "planned_start_at": trip.planned_start_at,
            "planned_end_at": trip.planned_end_at,
            "actual_start_at": trip.actual_start_at,
            "actual_end_at": trip.actual_end_at,
            "status": trip.status,
            "admin_note": trip.admin_note,
        }
    }