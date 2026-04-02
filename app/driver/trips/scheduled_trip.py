# app/driver/trips/scheduled_trip.py

from fastapi import APIRouter, Depends, HTTPException, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime, timezone, timedelta

from app.db.database import get_async_session
from app.db.schema import (
    ScheduledTrip,
    ScheduledTripStatus,
    Vehicle,
    Route,
    RouteStop,
    User,
    UserRole,
)
from app.auth.dependencies import get_current_user

router = APIRouter(
    prefix="/driver/scheduled-trips",
    tags=["Driver Scheduled Trips"]
)


# ============================================================
# CREATE SCHEDULED TRIP
# ============================================================
@router.post("/create")
async def create_scheduled_trip(
    route_name: str = Form(...),
    planned_start_at: datetime = Form(...),
    planned_end_at: datetime = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    # -------------------------
    # Role check
    # -------------------------
    if current_user.role != UserRole.DRIVER:
        raise HTTPException(status_code=403, detail="Only drivers can create trips")

    driver_user_id = current_user.id
    now = datetime.now(timezone.utc)

    # -------------------------
    # 🚫 24-hour rule
    # -------------------------
    if planned_start_at > now + timedelta(hours=24):
        raise HTTPException(
            status_code=400,
            detail="Trip can only be scheduled within next 24 hours"
        )

    # -------------------------
    # 🚫 No past / early scheduling
    # -------------------------
    if planned_start_at < now:
        raise HTTPException(
            status_code=400,
            detail="Cannot schedule trip in the past"
        )

    if planned_end_at <= planned_start_at:
        raise HTTPException(status_code=400, detail="Invalid timing")

    # -------------------------
    # Get vehicle
    # -------------------------
    result = await session.execute(
        select(Vehicle).where(
            Vehicle.driver_user_id == driver_user_id,
            Vehicle.is_active == True
        )
    )
    vehicle = result.scalar_one_or_none()

    if not vehicle:
        raise HTTPException(status_code=400, detail="No active vehicle found")

    # -------------------------
    # Get route
    # -------------------------
    result = await session.execute(
        select(Route).where(
            Route.name.ilike(route_name),
            Route.is_active == True
        )
    )
    route = result.scalar_one_or_none()

    if not route:
        raise HTTPException(status_code=404, detail="Route not found")

    # -------------------------
    # Validate stops
    # -------------------------
    result = await session.execute(
        select(RouteStop)
        .where(RouteStop.route_id == route.id)
        .order_by(RouteStop.sequence_no)
    )
    stops = result.scalars().all()

    if len(stops) < 2:
        raise HTTPException(
            status_code=400,
            detail="Route must have at least 2 stops"
        )

    # -------------------------
    # Previous trip check
    # (ALLOW if CANCELLED)
    # -------------------------
    result = await session.execute(
        select(ScheduledTrip)
        .where(ScheduledTrip.driver_user_id == driver_user_id)
        .order_by(ScheduledTrip.created_at.desc())
    )
    last_trip = result.scalars().first()

    if last_trip:
        if last_trip.status not in [
            ScheduledTripStatus.COMPLETED,
            ScheduledTripStatus.CANCELLED
        ]:
            raise HTTPException(
                status_code=400,
                detail="Finish or cancel previous trip first"
            )

    # -------------------------
    # Create trip
    # -------------------------
    trip = ScheduledTrip(
        route_id=route.id,
        driver_user_id=driver_user_id,
        vehicle_id=vehicle.id,
        planned_start_at=planned_start_at,
        planned_end_at=planned_end_at,
        status=ScheduledTripStatus.SCHEDULED,
    )

    session.add(trip)
    await session.commit()
    await session.refresh(trip)

    # -------------------------
    # ✅ FINAL RESPONSE (UPDATED)
    # -------------------------
    return {
        "message": "Trip created successfully",
        "trip_id": trip.id,
        "route_id": route.id,
        "route_name": route.name,
        "vehicle_id": vehicle.id,
        "status": trip.status,
        "planned_start": trip.planned_start_at,
        "planned_end": trip.planned_end_at,
    }


# ============================================================
# START TRIP
# ============================================================
@router.post("/{trip_id}/start")
async def start_trip(
    trip_id: str,
    actual_start_at: datetime = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.DRIVER:
        raise HTTPException(status_code=403, detail="Only drivers allowed")

    trip = await session.get(ScheduledTrip, trip_id)

    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    if trip.driver_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your trip")

    if trip.status != ScheduledTripStatus.SCHEDULED:
        raise HTTPException(status_code=400, detail="Trip cannot be started")

    # 🚫 No early start
    if actual_start_at < trip.planned_start_at:
        raise HTTPException(
            status_code=400,
            detail="Cannot start before planned start time"
        )

    trip.actual_start_at = actual_start_at
    trip.status = ScheduledTripStatus.IN_PROGRESS

    await session.commit()

    return {
        "message": "Trip started",
        "trip_id": trip.id,
        "actual_start_at": trip.actual_start_at,
        "status": trip.status,
    }


# ============================================================
# END TRIP
# ============================================================
@router.post("/{trip_id}/end")
async def end_trip(
    trip_id: str,
    actual_end_at: datetime = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.DRIVER:
        raise HTTPException(status_code=403, detail="Only drivers allowed")

    trip = await session.get(ScheduledTrip, trip_id)

    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    if trip.driver_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your trip")

    if trip.status != ScheduledTripStatus.IN_PROGRESS:
        raise HTTPException(status_code=400, detail="Trip not in progress")

    if not trip.actual_start_at or actual_end_at < trip.actual_start_at:
        raise HTTPException(
            status_code=400,
            detail="Invalid end time"
        )

    trip.actual_end_at = actual_end_at
    trip.status = ScheduledTripStatus.COMPLETED

    await session.commit()

    return {
        "message": "Trip completed",
        "trip_id": trip.id,
        "actual_end_at": trip.actual_end_at,
        "status": trip.status,
    }


# ============================================================
# LIST DRIVER TRIPS
# ============================================================
@router.get("/my-trips")
async def list_driver_trips(
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.DRIVER:
        raise HTTPException(status_code=403, detail="Only drivers allowed")

    result = await session.execute(
        select(ScheduledTrip)
        .where(ScheduledTrip.driver_user_id == current_user.id)
        .order_by(ScheduledTrip.planned_start_at.desc())
    )

    trips = result.scalars().all()

    return [
        {
            "trip_id": t.id,
            "route_id": t.route_id,
            "vehicle_id": t.vehicle_id,
            "status": t.status,
            "planned_start": t.planned_start_at,
            "planned_end": t.planned_end_at,
        }
        for t in trips
    ]