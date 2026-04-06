# app/driver/trips/scheduled_trip.py

from fastapi import APIRouter, Depends, HTTPException, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, update
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
    TripBooking,
    BookingStatus,
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


def is_within_radius(stop: Stop, lat: float, lng: float, radius=150) -> bool:
    distance = geodesic(
        (float(stop.lat), float(stop.lng)),
        (lat, lng)
    ).meters
    return distance <= radius


# ============================================================
# CREATE TRIP (UNCHANGED)
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

    result = await session.execute(
        select(Vehicle).where(
            Vehicle.driver_user_id == current_user.id,
            Vehicle.is_active == True
        )
    )
    vehicle = result.scalar_one_or_none()
    if not vehicle:
        raise HTTPException(400, "No active vehicle found")

    result = await session.execute(
        select(Route).where(
            func.lower(Route.name) == route_name.lower(),
            Route.is_active == True
        )
    )
    route = result.scalar_one_or_none()
    if not route:
        raise HTTPException(404, "Route not found")

    result = await session.execute(
        select(RouteStop)
        .where(RouteStop.route_id == route.id)
        .order_by(RouteStop.sequence_no)
    )
    stops = result.scalars().all()
    if len(stops) < 2:
        raise HTTPException(400, "Route must have at least 2 stops")

    result = await session.execute(
        select(ScheduledTrip)
        .where(ScheduledTrip.driver_user_id == current_user.id)
        .order_by(ScheduledTrip.created_at.desc())
    )
    last_trip = result.scalars().first()
    if last_trip and last_trip.status not in [
        ScheduledTripStatus.COMPLETED,
        ScheduledTripStatus.CANCELLED,
        ScheduledTripStatus.PREMATURE_END
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
    lat: float = Form(...),
    lng: float = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    trip = await session.get(ScheduledTrip, trip_id)

    if not trip or trip.driver_user_id != current_user.id:
        raise HTTPException(403, "Invalid trip")

    if trip.status != ScheduledTripStatus.SCHEDULED:
        raise HTTPException(400, "Trip already started")

    if trip.status != ScheduledTripStatus.SCHEDULED:
        raise HTTPException(400, "Trip already started")

    current_time = now_utc()

    if current_time < trip.planned_start_at:
         raise HTTPException(
        400,
            f"Cannot start before planned time ({to_ist(trip.planned_start_at)})"
    )
    # ❌ NEW: Cannot start after trip already ended
    if current_time > trip.planned_end_at:
        raise HTTPException(
        400,
        f"Cannot start trip after planned end time ({to_ist(trip.planned_end_at)})"
    )
    # =========================
    # GET FIRST STOP
    # =========================
    result = await session.execute(
        select(RouteStop)
        .where(RouteStop.route_id == trip.route_id)
        .order_by(RouteStop.sequence_no)
    )
    route_stops = result.scalars().all()

    if not route_stops:
        raise HTTPException(400, "No stops found")

    first_stop_rs = route_stops[0]
    first_stop = await session.get(Stop, first_stop_rs.stop_id)

    if not first_stop:
        raise HTTPException(400, "First stop not found")

    # =========================
    # GEO VALIDATION (IMPROVED)
    # =========================
    stop_lat = float(first_stop.lat)
    stop_lng = float(first_stop.lng)

    driver_lat = float(lat)
    driver_lng = float(lng)

    distance = geodesic(
        (stop_lat, stop_lng),
        (driver_lat, driver_lng)
    ).meters

    # ✅ USE DB radius + GPS tolerance buffer
    base_radius = first_stop.radius_meters or 0
    gps_buffer = 50   # meters (real-world drift)
    allowed_radius = base_radius + gps_buffer

    # 🔥 DEBUG LOG
    print("===== START TRIP DEBUG =====")
    print(f"STOP: ({stop_lat}, {stop_lng})")
    print(f"DRIVER: ({driver_lat}, {driver_lng})")
    print(f"DISTANCE: {distance:.2f} meters")
    print(f"BASE RADIUS: {base_radius}")
    print(f"FINAL ALLOWED RADIUS: {allowed_radius}")

    if distance > allowed_radius:
        raise HTTPException(
            400,
            f"Driver not at starting stop (distance={round(distance,2)}m, allowed={allowed_radius}m)"
        )

    # =========================
    # CREATE EVENTS (NO DUPLICATE)
    # =========================
    existing_stop_ids = set(
        await session.scalars(
            select(TripEvent.stop_id).where(
                TripEvent.scheduled_trip_id == trip.id
            )
        )
    )

    events = [
        TripEvent(scheduled_trip_id=trip.id, stop_id=rs.stop_id)
        for rs in route_stops
        if rs.stop_id not in existing_stop_ids
    ]

    if events:
        session.add_all(events)

    # =========================
    # UPDATE TRIP
    # =========================
    trip.actual_start_at = now_utc()
    trip.started_near_stop_id = first_stop.id
    trip.started_at_lat = driver_lat
    trip.started_at_long = driver_lng
    trip.status = ScheduledTripStatus.IN_PROGRESS

    await session.commit()

    return {
        "message": "Trip started",
        "start_time": to_ist(trip.actual_start_at),
        "debug": {
            "distance_m": round(distance, 2),
            "allowed_radius_m": allowed_radius
        }
    }

# ============================================================
# STOP ACTION (STRICT)
# ============================================================

@router.post("/{trip_id}/stop-action")
async def stop_action(
    trip_id: str,
    stop_id: str = Form(...),
    mode: str = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    trip = await session.get(ScheduledTrip, trip_id)

    if not trip or trip.driver_user_id != current_user.id:
        raise HTTPException(403, "Invalid trip")

    if trip.status != ScheduledTripStatus.IN_PROGRESS:
        raise HTTPException(400, "Trip not active")

    result = await session.execute(
        select(RouteStop).where(
            RouteStop.route_id == trip.route_id,
            RouteStop.stop_id == stop_id
        )
    )
    route_stop = result.scalar_one_or_none()

    result = await session.execute(
        select(TripEvent).where(
            TripEvent.scheduled_trip_id == trip_id,
            TripEvent.stop_id == stop_id
        )
    )
    event = result.scalar_one_or_none()

    current_time = now_utc()

    if mode == "arrive":
        if event.arrival_time:
            raise HTTPException(400, "Already arrived")

        event.arrival_time = current_time

    elif mode == "depart":
        if not event.arrival_time:
            raise HTTPException(400, "Arrive first")

        assume_minutes = route_stop.assume_time_diff_minutes or 0
        min_time = event.arrival_time + timedelta(minutes=assume_minutes)

        if current_time < min_time:
            raise HTTPException(400, "Cannot depart early")

        event.departure_time = current_time

    else:
        raise HTTPException(400, "Invalid mode")

    await session.commit()

    return {"message": f"{mode} success", "time": to_ist(current_time)}


# ============================================================
# END TRIP (WITH GEO)
# ============================================================

@router.post("/{trip_id}/end")
async def end_trip(
    trip_id: str,
    lat: float = Form(...),
    lng: float = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    trip = await session.get(ScheduledTrip, trip_id)

    if not trip or trip.driver_user_id != current_user.id:
        raise HTTPException(403, "Invalid trip")

    if trip.status != ScheduledTripStatus.IN_PROGRESS:
        raise HTTPException(400, "Trip not active")

    result = await session.execute(
        select(RouteStop)
        .where(RouteStop.route_id == trip.route_id)
        .order_by(RouteStop.sequence_no.desc())
    )
    last_stop_rs = result.scalars().first()
    last_stop = await session.get(Stop, last_stop_rs.stop_id)

    if not is_within_radius(last_stop, lat, lng):
        raise HTTPException(400, "Use emergency end")

    trip.actual_end_at = now_utc()
    trip.ended_near_stop_id = last_stop.id
    trip.ended_at_lat = lat
    trip.ended_at_long = lng
    trip.status = ScheduledTripStatus.COMPLETED

    await session.commit()

    return {"message": "Trip completed", "time": to_ist(trip.actual_end_at)}


# ============================================================
# EMERGENCY END (MERGED)
# ============================================================

@router.post("/{trip_id}/emergency-end")
async def emergency_end_trip(
    trip_id: str,
    reason: str = Form(...),
    lat: float = Form(...),
    lng: float = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    trip = await session.get(ScheduledTrip, trip_id)

    if not trip or trip.driver_user_id != current_user.id:
        raise HTTPException(403, "Invalid trip")

    if trip.status != ScheduledTripStatus.IN_PROGRESS:
        raise HTTPException(400, "Trip not active")

    # CANCEL BOOKINGS
    await session.execute(
        update(TripBooking)
        .where(
            TripBooking.scheduled_trip_id == trip_id,
            TripBooking.booking_status.in_([
                BookingStatus.BOOKED,
                BookingStatus.BOARDED
            ])
        )
        .values(
            booking_status=BookingStatus.CANCELLED,
            cancelled_at=now_utc()
        )
    )

    trip.actual_end_at = now_utc()
    trip.ended_at_lat = lat
    trip.ended_at_long = lng
    trip.status = ScheduledTripStatus.PREMATURE_END
    trip.premature_end_reason = reason

    await session.commit()

    return {
        "message": "Emergency ended",
        "reason": reason,
        "time": to_ist(trip.actual_end_at)
    }