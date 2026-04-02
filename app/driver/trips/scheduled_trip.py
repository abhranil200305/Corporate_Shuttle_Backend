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
# 🔧 HELPER (IMPORTANT FIX)
# ============================================================
def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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
    if current_user.role != UserRole.DRIVER:
        raise HTTPException(403, "Only drivers can create trips")

    now = datetime.now(timezone.utc)

    # ✅ FIX: Normalize time
    planned_start_at = to_utc(planned_start_at)
    planned_end_at = to_utc(planned_end_at)

    # 🚫 Past check
    if planned_start_at < now:
        raise HTTPException(400, "Cannot schedule trip in the past")

    # 🚫 24 hour rule
    if planned_start_at > now + timedelta(hours=24):
        raise HTTPException(400, "Trip allowed only within next 24 hours")

    # 🚫 End validation
    if planned_end_at <= planned_start_at:
        raise HTTPException(400, "End time must be after start time")

    # -------------------------
    # Vehicle
    # -------------------------
    result = await session.execute(
        select(Vehicle).where(
            Vehicle.driver_user_id == current_user.id,
            Vehicle.is_active == True
        )
    )
    vehicle = result.scalar_one_or_none()

    if not vehicle:
        raise HTTPException(400, "No active vehicle found")

    # -------------------------
    # Route
    # -------------------------
    result = await session.execute(
        select(Route).where(
            Route.name.ilike(f"%{route_name}%"),
            Route.is_active == True
        )
    )
    route = result.scalar_one_or_none()

    if not route:
        raise HTTPException(404, "Route not found")

    # -------------------------
    # Stops
    # -------------------------
    result = await session.execute(
        select(RouteStop)
        .where(RouteStop.route_id == route.id)
        .order_by(RouteStop.sequence_no)
    )
    stops = result.scalars().all()

    if len(stops) < 2:
        raise HTTPException(400, "Route must have at least 2 stops")

    # -------------------------
    # Previous trip check
    # -------------------------
    result = await session.execute(
        select(ScheduledTrip)
        .where(ScheduledTrip.driver_user_id == current_user.id)
        .order_by(ScheduledTrip.created_at.desc())
    )
    last_trip = result.scalars().first()

    if last_trip and last_trip.status not in [
        ScheduledTripStatus.COMPLETED,
        ScheduledTripStatus.CANCELLED
    ]:
        raise HTTPException(
            400,
            f"Previous trip not finished. Current: {last_trip.status}"
        )

    # -------------------------
    # Create
    # -------------------------
    trip = ScheduledTrip(
        route_id=route.id,
        driver_user_id=current_user.id,
        vehicle_id=vehicle.id,
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
        "status": trip.status,
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
        raise HTTPException(403, "Only drivers allowed")

    trip = await session.get(ScheduledTrip, trip_id)

    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.driver_user_id != current_user.id:
        raise HTTPException(403, "Not your trip")

    if trip.status != ScheduledTripStatus.SCHEDULED:
        raise HTTPException(400, f"Trip cannot be started. Current: {trip.status}")

    # ✅ FIX
    actual_start_at = to_utc(actual_start_at)

    if actual_start_at < trip.planned_start_at - timedelta(minutes=5):
        raise HTTPException(400, "Too early to start trip")

    trip.actual_start_at = actual_start_at
    trip.status = ScheduledTripStatus.IN_PROGRESS

    await session.commit()

    return {
        "message": "Trip started",
        "trip_id": trip.id,
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
        raise HTTPException(403, "Only drivers allowed")

    trip = await session.get(ScheduledTrip, trip_id)

    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.driver_user_id != current_user.id:
        raise HTTPException(403, "Not your trip")

    if trip.status != ScheduledTripStatus.IN_PROGRESS:
        raise HTTPException(400, f"Trip not in progress. Current: {trip.status}")

    # ✅ FIX
    actual_end_at = to_utc(actual_end_at)

    if actual_end_at < trip.actual_start_at:
        raise HTTPException(400, "End time before start time")

    trip.actual_end_at = actual_end_at
    trip.status = ScheduledTripStatus.COMPLETED

    await session.commit()

    return {
        "message": "Trip completed",
        "trip_id": trip.id,
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
        raise HTTPException(403, "Only drivers allowed")

    result = await session.execute(
        select(ScheduledTrip)
        .where(ScheduledTrip.driver_user_id == current_user.id)
        .order_by(ScheduledTrip.planned_start_at.desc())
    )

    trips = result.scalars().all()

    return [
        {
            "trip_id": t.id,
            "status": t.status,
            "planned_start": t.planned_start_at,
            "planned_end": t.planned_end_at,
        }
        for t in trips
    ]