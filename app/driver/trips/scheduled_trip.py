# app/driver/trips/scheduled_trip.py

from fastapi import APIRouter, Depends, HTTPException, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from datetime import datetime, timezone, timedelta

from geopy.distance import geodesic

from app.db.database import get_async_session
from app.db.schema import (
    ScheduledTrip,
    ScheduledTripStatus,
    Vehicle,
    Route,
    RouteStop,
    Stop,
    TripEvent,
    User,
    UserRole,
)
from app.auth.dependencies import get_current_user

router = APIRouter(
    prefix="/driver/scheduled-trips",
    tags=["Driver Scheduled Trips"]
)

# ============================================================
# HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_ist(dt: datetime | None):
    if not dt:
        return None
    return (dt + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M:%S")


def is_within_radius(stop: Stop, lat: float, lng: float) -> bool:
    distance = geodesic(
        (float(stop.lat), float(stop.lng)),
        (lat, lng)
    ).meters
    return distance <= stop.radius_meters


# ============================================================
# CREATE TRIP
# ============================================================

@router.post("/create")
async def create_trip(
    route_name: str = Form(...),
    planned_start_at: datetime = Form(...),
    planned_end_at: datetime = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.DRIVER:
        raise HTTPException(403, "Only drivers allowed")

    now = now_utc()

    planned_start_at = to_utc(planned_start_at)
    planned_end_at = to_utc(planned_end_at)

    if planned_start_at < now:
        raise HTTPException(400, "Cannot schedule in past")

    if planned_start_at > now + timedelta(hours=24):
        raise HTTPException(400, "Only allowed within 24 hours")

    if planned_end_at <= planned_start_at:
        raise HTTPException(400, "End must be after start")

    # VEHICLE
    result = await session.execute(
        select(Vehicle).where(
            Vehicle.driver_user_id == current_user.id,
            Vehicle.is_active == True
        )
    )
    vehicle = result.scalar_one_or_none()
    if not vehicle:
        raise HTTPException(400, "No active vehicle found")

    # ROUTE
    result = await session.execute(
        select(Route).where(
            func.lower(Route.name) == route_name.lower(),
            Route.is_active == True
        )
    )
    route = result.scalar_one_or_none()
    if not route:
        raise HTTPException(404, "Route not found")

    # STOPS
    result = await session.execute(
        select(RouteStop)
        .where(RouteStop.route_id == route.id)
        .order_by(RouteStop.sequence_no)
    )
    stops = result.scalars().all()

    if len(stops) < 2:
        raise HTTPException(400, "Route must have at least 2 stops")

    # PREVIOUS TRIP CHECK
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
        raise HTTPException(400, "Previous trip not finished")

    trip = ScheduledTrip(
        route_id=route.id,
        vehicle_id=vehicle.id,
        driver_user_id=current_user.id,
        planned_start_at=planned_start_at,
        planned_end_at=planned_end_at,
        status=ScheduledTripStatus.SCHEDULED,
    )

    session.add(trip)
    await session.commit()
    await session.refresh(trip)

    return {
        "trip_id": trip.id,
        "planned_start_at_ist": to_ist(trip.planned_start_at),
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
    try:
        trip = await session.get(ScheduledTrip, trip_id)

        # 🔐 VALIDATION
        if not trip:
            raise HTTPException(404, "Trip not found")

        if trip.driver_user_id != current_user.id:
            raise HTTPException(403, "Not your trip")

        if trip.status == ScheduledTripStatus.IN_PROGRESS:
            return {
                "message": "Trip already started",
                "actual_start_at": to_ist(trip.actual_start_at)
            }

        if trip.status != ScheduledTripStatus.SCHEDULED:
            raise HTTPException(400, f"Invalid status: {trip.status}")

        # ⏱️ TIME CHECK
        if now_utc() < trip.planned_start_at - timedelta(minutes=5):
            raise HTTPException(400, "Too early to start")

        # ---------------- STOPS ----------------
        result = await session.execute(
            select(RouteStop)
            .where(RouteStop.route_id == trip.route_id)
            .order_by(RouteStop.sequence_no)
        )
        stops = result.scalars().all()

        if not stops:
            raise HTTPException(400, "No stops found")

        # ---------------- DUPLICATE CHECK ----------------
        result = await session.execute(
            select(TripEvent.id)
            .where(TripEvent.scheduled_trip_id == trip.id)
            .limit(1)
        )
        already_exists = result.scalar_one_or_none()

        if already_exists:
            raise HTTPException(400, "Trip already initialized")

        # ---------------- CREATE EVENTS ----------------
        events = [
            TripEvent(
                scheduled_trip_id=trip.id,
                stop_id=rs.stop_id
            )
            for rs in stops
        ]

        session.add_all(events)

        # ---------------- UPDATE TRIP ----------------
        trip.actual_start_at = now_utc()
        trip.status = ScheduledTripStatus.IN_PROGRESS

        await session.commit()

        return {
            "message": "Trip started successfully",
            "total_stops": len(events),
            "actual_start_at": to_ist(trip.actual_start_at)
        }

    except HTTPException:
        await session.rollback()
        raise

    except Exception as e:
        await session.rollback()
        print("START TRIP ERROR:", str(e))
        raise HTTPException(500, "Internal Server Error")
# ============================================================
# STOP ACTION (FIXED)
# ============================================================

@router.post("/{trip_id}/stop-action")
async def stop_action(
    trip_id: str,
    stop_id: str = Form(...),
    mode: str = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    try:
        trip = await session.get(ScheduledTrip, trip_id)

        # 🔐 VALIDATION
        if not trip or trip.driver_user_id != current_user.id:
            raise HTTPException(403, "Invalid trip")

        if trip.status != ScheduledTripStatus.IN_PROGRESS:
            raise HTTPException(400, "Trip not active")

        # ✅ CHECK STOP BELONGS TO ROUTE
        result = await session.execute(
            select(RouteStop).where(
                RouteStop.route_id == trip.route_id,
                RouteStop.stop_id == stop_id
            )
        )
        route_stop = result.scalar_one_or_none()

        if not route_stop:
            raise HTTPException(400, "Stop not part of this route")

        # ✅ GET EVENT
        result = await session.execute(
            select(TripEvent).where(
                TripEvent.scheduled_trip_id == trip_id,
                TripEvent.stop_id == stop_id
            )
        )
        event = result.scalar_one_or_none()

        if not event:
            raise HTTPException(404, "Trip event not found")

        # ================= MODE =================
        if mode == "arrive":

            if event.arrival_time:
                raise HTTPException(400, "Already arrived")

            event.arrival_time = now_utc()

            await session.commit()

            return {
                "message": "Arrived successfully",
                "trip_id": trip_id,
                "stop_id": stop_id,
                "arrival_time": to_ist(event.arrival_time)
            }

        elif mode == "depart":

            if not event.arrival_time:
                raise HTTPException(400, "Arrive first")

            if event.departure_time:
                raise HTTPException(400, "Already departed")

            event.departure_time = now_utc()

            await session.commit()

            return {
                "message": "Departed successfully",
                "trip_id": trip_id,
                "stop_id": stop_id,
                "departure_time": to_ist(event.departure_time)
            }

        else:
            raise HTTPException(400, "Invalid mode")

    except HTTPException:
        await session.rollback()
        raise

    except Exception as e:
        await session.rollback()
        print("STOP ACTION ERROR:", str(e))
        raise HTTPException(500, "Internal Server Error")


# ============================================================
# END TRIP
# ============================================================

@router.post("/{trip_id}/end")
async def end_trip(
    trip_id: str,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    trip = await session.get(ScheduledTrip, trip_id)

    if not trip or trip.driver_user_id != current_user.id:
        raise HTTPException(403, "Invalid trip")

    if trip.status != ScheduledTripStatus.IN_PROGRESS:
        raise HTTPException(400, "Trip not active")

    trip.actual_end_at = now_utc()
    trip.status = ScheduledTripStatus.COMPLETED

    await session.commit()

    return {
        "message": "Trip completed",
        "time": to_ist(trip.actual_end_at)
    }