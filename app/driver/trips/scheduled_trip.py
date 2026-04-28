# app/driver/trips/scheduled_trip.py

from fastapi import APIRouter, Depends, HTTPException, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, update
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import selectinload

from fastapi import Request
from app.notifications.service import NotificationService

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
    TripScanEvent,
)
from app.db.schema import ScanType  
from app.db.schema import EmergencyStopRequestStatus
from app.auth.dependencies import get_current_user
from geopy.distance import geodesic


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
# CREATE TRIP (WITH AC/NON-AC VALIDATION)
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

    # ---------------------------------------------------
    # 1. Get vehicle (REMOVE is_active filter ❗)
    # ---------------------------------------------------
    result = await session.execute(
        select(Vehicle).where(
            Vehicle.driver_user_id == current_user.id
        )
    )
    vehicle = result.scalar_one_or_none()

    if not vehicle:
        raise HTTPException(400, "No vehicle found")

    # ---------------------------------------------------
    # 🔥 NEW VALIDATION: vehicle must be active
    # ---------------------------------------------------
    if not vehicle.is_active:
        raise HTTPException(
            status_code=400,
            detail="Vehicle is inactive. Contact admin and raise a support ticket"
        )

    # ---------------------------------------------------
    # 2. Get route
    # ---------------------------------------------------
    result = await session.execute(
        select(Route).where(
            func.lower(Route.name) == route_name.lower(),
            Route.is_active == True
        )
    )
    route = result.scalar_one_or_none()

    if not route:
        raise HTTPException(404, "Route not found")

    # ---------------------------------------------------
    # 🔥 3. AC / NON-AC VALIDATION
    # ---------------------------------------------------
    if route.has_ac != vehicle.has_ac:
        raise HTTPException(
            status_code=400,
            detail="Vehicle type does not match route type (AC / NON-AC mismatch)"
        )

    # ---------------------------------------------------
    # 4. Validate stops
    # ---------------------------------------------------
    result = await session.execute(
        select(RouteStop)
        .where(RouteStop.route_id == route.id)
        .order_by(RouteStop.sequence_no)
    )
    stops = result.scalars().all()

    if len(stops) < 2:
        raise HTTPException(400, "Route must have at least 2 stops")

    # ---------------------------------------------------
    # 5. Check previous trip
    # ---------------------------------------------------
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

    # ---------------------------------------------------
    # 6. Create trip
    # ---------------------------------------------------
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
    request: Request,   # ✅ FIXED POSITION
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

    current_time = now_utc()

    # ❌ Cannot start before planned time
    if current_time < trip.planned_start_at:
        raise HTTPException(
            400,
            f"Cannot start before planned time ({to_ist(trip.planned_start_at)})"
        )

    # ✅ NEW: 15 min grace window logic
    grace_limit = trip.planned_start_at + timedelta(minutes=15)

    if current_time > grace_limit:
        raise HTTPException(
            400,
            f"Start window expired. Allowed till {to_ist(grace_limit)}"
        )

    # ❌ Cannot start after trip already ended
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
    # =========================
    # SEND NOTIFICATIONS
    # =========================

    notification_service = NotificationService(
    db=session,
    ws_hub=request.app.state.ws_hub
    )

    result = await session.execute(
    select(TripBooking).where(
        TripBooking.scheduled_trip_id == trip.id,
        TripBooking.booking_status == BookingStatus.BOOKED
        )
    )

    bookings = result.scalars().all()

    for booking in bookings:
        await notification_service.notify_user(
        user_id=booking.passenger_user_id,
        title="Trip Started",
        message="Your bus has started.",
        data={"trip_id": trip.id}
    )
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
    request: Request,
    stop_id: str = Form(...),
    mode: str = Form(...),
    driver_lat: float = Form(...),
    driver_lng: float = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    # -------------------------------
    # Fetch trip
    # -------------------------------
    trip = await session.get(ScheduledTrip, trip_id)
    if not trip or trip.driver_user_id != current_user.id:
        raise HTTPException(403, "Invalid trip")

    if trip.status != ScheduledTripStatus.IN_PROGRESS:
        raise HTTPException(400, "Trip not active")

    # -------------------------------
    # Fetch route stop
    # -------------------------------
    result = await session.execute(
        select(RouteStop)
        .where(
            RouteStop.route_id == trip.route_id,
            RouteStop.stop_id == stop_id
        )
        .options(selectinload(RouteStop.stop))
    )
    route_stop = result.scalar_one_or_none()
    if not route_stop:
        raise HTTPException(400, "Stop not found in this route")

    stop = route_stop.stop
    current_sequence = route_stop.sequence_no

    # -------------------------------
    # GEO VALIDATION
    # -------------------------------
    distance = geodesic(
        (float(stop.lat), float(stop.lng)),
        (driver_lat, driver_lng)
    ).meters

    gps_buffer = 50
    allowed_radius = (stop.radius_meters or 0) + gps_buffer

    if distance > allowed_radius:
        raise HTTPException(
            400,
            f"Driver not within stop radius. Distance: {int(distance)}m, Allowed: {allowed_radius}m"
        )

    # -------------------------------
    # Fetch trip event
    # -------------------------------
    result = await session.execute(
        select(TripEvent).where(
            TripEvent.scheduled_trip_id == trip_id,
            TripEvent.stop_id == stop_id
        )
    )
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(400, "Trip event not found")

    current_time = now_utc()

    # -------------------------------
    # Load ALL route stops
    # -------------------------------
    result = await session.execute(
        select(RouteStop).where(RouteStop.route_id == trip.route_id)
    )
    route_stops = result.scalars().all()

    route_stop_map = {str(rs.stop_id): rs for rs in route_stops}
    valid_stop_ids = set(route_stop_map.keys())

    # =========================================================
    # ROUTE VALIDATION
    # =========================================================
    result = await session.execute(
        select(TripBooking).where(
            TripBooking.scheduled_trip_id == trip_id,
            TripBooking.booking_status.in_([
                BookingStatus.BOOKED,
                BookingStatus.BOARDED
            ])
        )
    )
    all_bookings = result.scalars().all()

    for booking in all_bookings:
        if (
            str(booking.pickup_stop_id) not in valid_stop_ids
            or str(booking.dropoff_stop_id) not in valid_stop_ids
        ):
            raise HTTPException(400, "Invalid booking: stop not part of route")

    # =========================================================
    # BOARDING / DEBOARDING RULES
    # =========================================================
    if mode == "arrive" and not route_stop.boarding_allowed:
        raise HTTPException(400, "Boarding not allowed at this stop")

    if mode == "depart" and not route_stop.deboarding_allowed:
        raise HTTPException(400, "Deboarding not allowed at this stop")

    # =========================================================
    # BLOCK ARRIVE IF PREVIOUS STOP NOT DEPARTED
    # =========================================================
    previous_route_stop = None
    previous_event = None

    if current_sequence > 1:
        previous_route_stop = next(
            (rs for rs in route_stops if rs.sequence_no == current_sequence - 1),
            None
        )

        if not previous_route_stop:
            raise HTTPException(400, "Previous route stop not found")

        previous_event_result = await session.execute(
            select(TripEvent).where(
                TripEvent.scheduled_trip_id == trip_id,
                TripEvent.stop_id == previous_route_stop.stop_id
            )
        )
        previous_event = previous_event_result.scalar_one_or_none()

    if mode == "arrive":
        if current_sequence > 1 and (not previous_event or not previous_event.departure_time):
            raise HTTPException(400, "Cannot arrive. Previous stop not departed yet.")

    # =========================================================
    # BLOCK DEPART IF PASSENGER NOT DROPPED
    # =========================================================
    if mode == "depart":
        result = await session.execute(
            select(TripBooking).where(
                TripBooking.scheduled_trip_id == trip_id,
                TripBooking.booking_status == BookingStatus.BOARDED,
                TripBooking.dropoff_stop_id == stop_id
            )
        )
        drop_pending_bookings = result.scalars().all()

        booking_ids = [b.id for b in drop_pending_bookings]

        if booking_ids:
            result = await session.execute(
                select(TripScanEvent.booking_id).where(
                    TripScanEvent.booking_id.in_(booking_ids),
                    TripScanEvent.scan_type == ScanType.DROP
                )
            )
            dropped_ids = set(result.scalars().all())

            not_dropped = [b for b in drop_pending_bookings if b.id not in dropped_ids]

            if not_dropped:
                raise HTTPException(400, "Cannot depart. Passenger not dropped at this stop.")

    # -------------------------------
    # ARRIVE / DEPART
    # -------------------------------
    if mode == "arrive":
        if event.arrival_time:
            raise HTTPException(400, "Already arrived")

        event.arrival_time = current_time

    elif mode == "depart":
        if not event.arrival_time:
            raise HTTPException(400, "Arrive first")

        if event.departure_time:
            raise HTTPException(400, "Already departed")

        # =====================================================
        # SEGMENT TIME VALIDATION
        # assume_time_diff_minutes means:
        # previous stop departure -> current stop departure
        # =====================================================
        if current_sequence > 1:
            if not previous_event or not previous_event.departure_time:
                raise HTTPException(400, "Previous stop departure missing")

            assume_minutes = route_stop.assume_time_diff_minutes or 0
            min_departure_time = previous_event.departure_time + timedelta(minutes=assume_minutes)

            if current_time < min_departure_time:
                raise HTTPException(
                    400,
                    f"Cannot depart early. Minimum departure allowed after {assume_minutes} minutes from previous stop departure."
                )

        event.departure_time = current_time

    else:
        raise HTTPException(400, "Invalid mode")

    await session.commit()

    # -------------------------------
    # Notifications
    # -------------------------------
    notification_service = NotificationService(
        db=session,
        ws_hub=getattr(request.app.state, "ws_hub", None)
    )

    for booking in all_bookings:
        pickup_rs = route_stop_map.get(str(booking.pickup_stop_id))
        drop_rs = route_stop_map.get(str(booking.dropoff_stop_id))
        current_rs = route_stop_map.get(str(stop_id))

        if not pickup_rs or not drop_rs or not current_rs:
            continue

        if mode == "arrive" and str(booking.pickup_stop_id) == str(stop_id):
            await notification_service.notify_user(
                user_id=booking.passenger_user_id,
                title="Bus Arrived",
                message="Bus has arrived at your boarding stop.",
                data={"trip_id": trip.id, "stop_id": stop_id},
            )

        if mode == "depart" and str(booking.dropoff_stop_id) == str(stop_id):
            await notification_service.notify_user(
                user_id=booking.passenger_user_id,
                title="Next Stop Approaching",
                message="Bus is leaving your drop stop.",
                data={"trip_id": trip.id, "stop_id": stop_id},
            )

        if (
            mode == "depart"
            and booking.booking_status == BookingStatus.BOOKED
            and str(booking.pickup_stop_id) == str(stop_id)
        ):
            scan_result = await session.execute(
                select(TripScanEvent).where(
                    TripScanEvent.booking_id == booking.id,
                    TripScanEvent.scan_type == ScanType.BOARD
                )
            )
            if not scan_result.scalar_one_or_none():
                booking.booking_status = BookingStatus.MISSED

                await notification_service.notify_user(
                    user_id=booking.passenger_user_id,
                    title="Missed Bus",
                    message="You missed your ride.",
                    data={"trip_id": trip.id},
                )

        if mode == "depart" and booking.booking_status == BookingStatus.BOARDED:
            if drop_rs.sequence_no < current_rs.sequence_no:
                await notification_service.notify_user(
                    user_id=booking.passenger_user_id,
                    title="Missed Drop",
                    message="Bus passed your stop!",
                    data={"trip_id": trip.id},
                )

    await session.commit()

    return {
        "message": f"{mode} success",
        "time": to_ist(current_time),
        "distance_from_stop_meters": int(distance)
    }

# ============================================================
# END TRIP (WITH GEO + PASSENGER + STOP VALIDATION)
# ============================================================

@router.post("/{trip_id}/end")
async def end_trip(
    trip_id: str,
    lat: float = Form(...),
    lng: float = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    # =========================
    # FETCH TRIP
    # =========================
    trip = await session.get(ScheduledTrip, trip_id)

    if not trip or trip.driver_user_id != current_user.id:
        raise HTTPException(403, "Invalid trip")

    if trip.status != ScheduledTripStatus.IN_PROGRESS:
        raise HTTPException(400, "Trip not active")

    current_time = now_utc()

    # =========================
    # CHECK STOPS
    # =========================
    result = await session.execute(
        select(TripEvent).where(
            TripEvent.scheduled_trip_id == trip_id
        )
    )
    trip_events = result.scalars().all()

    incomplete_stops = [
        e.stop_id for e in trip_events
        if e.arrival_time is None or e.departure_time is None
    ]

    if incomplete_stops:
        raise HTTPException(
            400,
            f"Stops incomplete: {len(incomplete_stops)} stop(s) missing arrival/departure"
        )

    # =========================
    # CHECK PASSENGERS
    # =========================
    boarded_result = await session.execute(
        select(func.count()).select_from(TripScanEvent).where(
            TripScanEvent.scheduled_trip_id == trip_id,
            TripScanEvent.scan_type == ScanType.BOARD
        )
    )
    boarded_count = boarded_result.scalar() or 0

    dropped_result = await session.execute(
        select(func.count()).select_from(TripScanEvent).where(
            TripScanEvent.scheduled_trip_id == trip_id,
            TripScanEvent.scan_type == ScanType.DROP
        )
    )
    dropped_count = dropped_result.scalar() or 0

    if boarded_count > dropped_count:
        raise HTTPException(
            400,
            f"{boarded_count - dropped_count} passenger(s) still inside bus"
        )

    # =========================
    # TIME CHECK (STRICT)
    # =========================
    if current_time < trip.planned_end_at:
        raise HTTPException(
            400,
            f"Too early to end trip. Planned end at {to_ist(trip.planned_end_at)}"
        )

    # =========================
    # GET LAST STOP
    # =========================
    result = await session.execute(
        select(RouteStop)
        .where(RouteStop.route_id == trip.route_id)
        .order_by(RouteStop.sequence_no.desc())
    )
    last_stop_rs = result.scalars().first()

    if not last_stop_rs:
        raise HTTPException(400, "No stops found for route")

    last_stop = await session.get(Stop, last_stop_rs.stop_id)

    if not last_stop:
        raise HTTPException(400, "Last stop not found")

    # =========================
    # GEO VALIDATION (FIXED)
    # =========================
    distance = geodesic(
        (float(last_stop.lat), float(last_stop.lng)),
        (lat, lng)
    ).meters

    base_radius = last_stop.radius_meters or 0
    gps_buffer = 50  # you can tune this
    allowed_radius = base_radius + gps_buffer

    if distance > allowed_radius:
        raise HTTPException(
            400,
            f"Driver not at last stop | distance={round(distance,2)}m | allowed={allowed_radius}m"
        )

    # =========================
    # UPDATE TRIP
    # =========================
    trip.actual_end_at = current_time
    trip.ended_near_stop_id = last_stop.id
    trip.ended_at_lat = lat
    trip.ended_at_long = lng
    trip.status = ScheduledTripStatus.COMPLETED

    await session.commit()

    return {
        "message": "Trip completed",
        "time": to_ist(trip.actual_end_at),
        "geo_debug": {
            "distance_m": round(distance, 2),
            "allowed_radius_m": allowed_radius
        },
        "passenger_check": {
            "boarded": boarded_count,
            "dropped": dropped_count
        },
        "stops_checked": len(trip_events)
    }

# ============================================================
# EMERGENCY END (MERGED)
# ============================================================

@router.post("/{trip_id}/emergency-end")
async def emergency_end_trip(
    trip_id: str,
    request: Request,
    reason: str = Form(...),
    lat: float = Form(...),
    lng: float = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    # -----------------------------
    # 1. Validate reason
    # -----------------------------
    if len(reason.strip()) < 5:
        raise HTTPException(
            status_code=400,
            detail="Reason must be at least 5 characters long."
        )

    # -----------------------------
    # 2. Validate trip
    # -----------------------------
    trip = await session.get(ScheduledTrip, trip_id)
    if not trip or trip.driver_user_id != current_user.id:
        raise HTTPException(403, "Invalid trip")

    if trip.status != ScheduledTripStatus.IN_PROGRESS:
        raise HTTPException(400, "Trip not active")

    now = now_utc()

    # -----------------------------
    # 3. CANCEL BOOKINGS (OPTIMIZED)
    # -----------------------------
    await session.execute(
        update(TripBooking)
        .where(
            TripBooking.scheduled_trip_id == trip_id,
            (
                (TripBooking.booking_status == BookingStatus.BOOKED)
                |
                (
                    (TripBooking.booking_status == BookingStatus.BOARDED)
                    &
                    (TripBooking.completed_at.is_(None))
                )
            )
        )
        .values(
            booking_status=BookingStatus.CANCELLED,
            cancelled_at=now
        )
    )

    # -----------------------------
    # 4. Update trip
    # -----------------------------
    trip.actual_end_at = now
    trip.ended_at_lat = lat
    trip.ended_at_long = lng
    trip.status = ScheduledTripStatus.PREMATURE_END
    trip.premature_end_reason = reason

    await session.commit()

    # -----------------------------
    # 5. Notify admins
    # -----------------------------
    notification_service = NotificationService(
        db=session,
        ws_hub=request.app.state.ws_hub
    )

    result = await session.execute(
        select(User).where(User.role == UserRole.ADMIN)
    )
    admins = result.scalars().all()

    for admin in admins:
        await notification_service.notify_user(
            user_id=admin.id,
            title="Trip Premature End",
            message="Trip ended early due to emergency",
            data={"trip_id": trip.id}
        )

    # -----------------------------
    # 6. Response
    # -----------------------------
    return {
        "message": "Emergency ended",
        "reason": reason,
        "time": to_ist(trip.actual_end_at)
    }