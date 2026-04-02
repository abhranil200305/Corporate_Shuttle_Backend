from fastapi import APIRouter, Depends, HTTPException, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime, timezone

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
    vehicle_id: str = Form(...),
    planned_start_at: datetime = Form(...),
    planned_end_at: datetime = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    # ✅ Ensure driver role
    if current_user.role != UserRole.DRIVER:
        raise HTTPException(status_code=403, detail="Only drivers can create trips")

    driver_user_id = current_user.id

    # 1️⃣ Validate vehicle
    vehicle = await session.get(Vehicle, vehicle_id)
    if not vehicle or vehicle.driver_user_id != driver_user_id:
        raise HTTPException(status_code=400, detail="Invalid vehicle")

    # 2️⃣ Get route (case-insensitive)
    result = await session.execute(
        select(Route).where(
            Route.name.ilike(route_name),
            Route.is_active == True
        )
    )
    route = result.scalar_one_or_none()

    if not route:
        raise HTTPException(status_code=404, detail="Route not found")

    # 3️⃣ Get route stops
    result = await session.execute(
        select(RouteStop)
        .where(RouteStop.route_id == route.id)
        .order_by(RouteStop.sequence_no)
    )
    stops = result.scalars().all()

    if len(stops) < 2:
        raise HTTPException(status_code=400, detail="Route must have at least 2 stops")

    # 4️⃣ Ensure previous trip completed (IMPORTANT RULE)
    result = await session.execute(
        select(ScheduledTrip)
        .where(ScheduledTrip.driver_user_id == driver_user_id)
        .order_by(ScheduledTrip.created_at.desc())
    )
    last_trip = result.scalars().first()

    if last_trip and last_trip.actual_end_at is None:
        raise HTTPException(
            status_code=400,
            detail="Finish previous trip before creating a new one"
        )

    # 5️⃣ Validate time
    if planned_end_at <= planned_start_at:
        raise HTTPException(status_code=400, detail="Invalid trip timing")

    # 6️⃣ Create trip
    trip = ScheduledTrip(
        route_id=route.id,
        driver_user_id=driver_user_id,
        vehicle_id=vehicle_id,
        planned_start_at=planned_start_at,
        planned_end_at=planned_end_at,
        status=ScheduledTripStatus.SCHEDULED,
    )

    session.add(trip)
    await session.commit()
    await session.refresh(trip)

    return {
        "message": "Trip created successfully",
        "trip_id": trip.id,
        "route_id": route.id,
        "route_name": route.name,
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

    now = datetime.now(timezone.utc)

    # RULE: planned_start_at <= actual_start_at
    if now < trip.planned_start_at:
        raise HTTPException(
            status_code=400,
            detail="Cannot start before planned start time"
        )

    trip.actual_start_at = now
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

    now = datetime.now(timezone.utc)

    trip.actual_end_at = now
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

    return trips