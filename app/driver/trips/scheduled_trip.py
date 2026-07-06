# app/driver/trips/scheduled_trip.py

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from geopy.distance import geodesic
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.auth.dependencies import get_current_user
from app.db.database import get_async_session

#from app.db.schema import RFIDTripRide, RFIDRideStatus
from app.db.schema import (
    BookingStatus,
    PlatformSettings,
    Route,
    RouteStop,
    ScanType,
    ScheduledTrip,
    ScheduledTripStatus,
    Stop,
    TripBooking,
    TripEvent,
    TripScanEvent,
    User,
    UserRole,
    Vehicle,
    VehicleInspectionStatus,
    VehicleVerificationStatus,
)
from app.notifications.service import NotificationService
from app.realtime.events import (
    get_api_refresh_hub,
    publish_departure_allowed_if_eligible,
    publish_trip_event,
    schedule_next_stop_departure_check,
    schedule_start_allowed,
)
from app.rfid.scan_service import RFIDScanService

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
    request: Request,
    route_name: str = Form(...),

    planned_start_at: datetime = Form(...),
    planned_end_at: datetime = Form(...),

    rfid_reserved_seat_count: int | None = Form(None),

    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    # ---------------------------------------------------
    # ROLE CHECK
    # ---------------------------------------------------
    if current_user.role != UserRole.DRIVER:
        raise HTTPException(
            status_code=403,
            detail="Only drivers allowed"
        )

    now = now_utc()

    planned_start_at = to_utc(planned_start_at)
    planned_end_at = to_utc(planned_end_at)

    # ---------------------------------------------------
    # TIME VALIDATION
    # ---------------------------------------------------
    if planned_start_at < now:
        raise HTTPException(
            status_code=400,
            detail="Cannot schedule in past"
        )

    if planned_start_at > now + timedelta(hours=24):
        raise HTTPException(
            status_code=400,
            detail="Only allowed within 24 hours"
        )

    if planned_end_at <= planned_start_at:
        raise HTTPException(
            status_code=400,
            detail="End must be after start"
        )

    # ---------------------------------------------------
    # 1. GET VEHICLE
    # ---------------------------------------------------
    result = await session.execute(
        select(Vehicle).where(
            Vehicle.driver_user_id == current_user.id
        )
    )

    vehicle = result.scalar_one_or_none()

    if not vehicle:
        raise HTTPException(
            status_code=400,
            detail="No vehicle found"
        )

    # ---------------------------------------------------
    # VEHICLE ACTIVE CHECK
    # ---------------------------------------------------
    if not vehicle.is_active:
        raise HTTPException(
            status_code=400,
            detail=(
                "Vehicle is inactive. "
                "Contact admin and raise a support ticket"
            )
        )

    # ---------------------------------------------------
    # VEHICLE VERIFICATION CHECK
    # ---------------------------------------------------
    if (
        vehicle.verification_status
        != VehicleVerificationStatus.VERIFIED
    ):
        raise HTTPException(
            status_code=400,
            detail="Vehicle is not verified"
        )

    # ---------------------------------------------------
    # ✅ VEHICLE INSPECTION APPROVAL CHECK
    # Driver can create trip only if
    # inspection_status == APPROVED
    # ---------------------------------------------------
    if (
        vehicle.inspection_status
        != VehicleInspectionStatus.APPROVED
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Vehicle inspection is not approved. "
                "Trip creation is not allowed"
            )
        )

    # ---------------------------------------------------
    # 2. GET ROUTE
    # ---------------------------------------------------
    result = await session.execute(
        select(Route).where(
            func.lower(Route.name) == route_name.lower(),
            Route.is_active.is_(True)
        )
    )

    route = result.scalar_one_or_none()

    if not route:
        raise HTTPException(
            status_code=404,
            detail="Route not found"
        )

    # ---------------------------------------------------
    # 3. AC / NON-AC VALIDATION
    # ---------------------------------------------------
    if route.has_ac != vehicle.has_ac:
        raise HTTPException(
            status_code=400,
            detail=(
                "Vehicle type does not match "
                "route type (AC / NON-AC mismatch)"
            )
        )

    # ---------------------------------------------------
    # 4. VALIDATE ROUTE STOPS
    # ---------------------------------------------------
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

    # ---------------------------------------------------
    # 5. CHECK PREVIOUS TRIP
    # ---------------------------------------------------
    result = await session.execute(
        select(ScheduledTrip)
        .where(
            ScheduledTrip.driver_user_id == current_user.id
        )
        .order_by(ScheduledTrip.created_at.desc())
    )

    last_trip = result.scalars().first()

    if last_trip and last_trip.status not in [
        ScheduledTripStatus.COMPLETED,
        ScheduledTripStatus.CANCELLED,
        ScheduledTripStatus.PREMATURE_END
    ]:
        raise HTTPException(
            status_code=400,
            detail="Previous trip not finished"
        )

    # ---------------------------------------------------
    # 6. PLATFORM SETTINGS
    # ---------------------------------------------------
    result = await session.execute(
        select(PlatformSettings).where(
            PlatformSettings.settings_key == "default"
        )
    )

    platform_settings = result.scalar_one_or_none()

    # ✅ fallback
    allow_driver_rfid_seat_reservation = True

    if platform_settings:
        allow_driver_rfid_seat_reservation = (
            platform_settings.allow_driver_rfid_seat_reservation
        )

    # ---------------------------------------------------
    # 7. RFID RESERVED SEAT LOGIC
    # ---------------------------------------------------
    # ✅ CASE 1:
    # Platform allows driver RFID reservation
    # Driver can choose custom value
    # ---------------------------------------------------
    if allow_driver_rfid_seat_reservation:

        final_rfid_reserved_seat_count = (
            rfid_reserved_seat_count
            if rfid_reserved_seat_count is not None
            else 0
        )

        # -------------------------------
        # VALIDATION
        # -------------------------------
        if final_rfid_reserved_seat_count < 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "RFID reserved seat count "
                    "cannot be negative"
                )
            )

        if final_rfid_reserved_seat_count > vehicle.seat_count:
            raise HTTPException(
                status_code=400,
                detail=(
                    "RFID reserved seat count "
                    "cannot exceed vehicle seat count"
                )
            )

    # ---------------------------------------------------
    # ✅ CASE 2:
    # Platform disabled RFID seat reservation
    # Driver CANNOT choose seat count
    # Automatically force 0
    # ---------------------------------------------------
    else:

        # ❌ Driver trying to send value
        if rfid_reserved_seat_count is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "RFID seat reservation "
                    "is disabled by platform admin"
                )
            )

        final_rfid_reserved_seat_count = 0

    # ---------------------------------------------------
    # 8. CREATE TRIP
    # ---------------------------------------------------
    trip = ScheduledTrip(
        route_id=route.id,

        vehicle_id=vehicle.id,

        driver_user_id=current_user.id,

        planned_start_at=planned_start_at,

        planned_end_at=planned_end_at,

        status=ScheduledTripStatus.SCHEDULED,

        # ✅ FINAL RFID RESERVED SEAT COUNT
        rfid_reserved_seat_count=(
            final_rfid_reserved_seat_count
        ),
    )

    session.add(trip)

    await session.commit()
    await session.refresh(trip)

    refresh_hub = get_api_refresh_hub(request.app)
    await publish_trip_event(
        refresh_hub,
        session,
        event="trip.created",
        trip_id=trip.id,
        data={"route_id": trip.route_id},
        broadcast_passengers=True,
    )
    await schedule_start_allowed(
        refresh_hub,
        trip_id=trip.id,
        driver_user_id=trip.driver_user_id,
        planned_start_at=trip.planned_start_at,
    )

    # ---------------------------------------------------
    # RESPONSE
    # ---------------------------------------------------
    return {
        "trip_id": trip.id,

        "rfid_reserved_seat_count": (
            trip.rfid_reserved_seat_count
        ),

        "allow_driver_rfid_seat_reservation": (
            allow_driver_rfid_seat_reservation
        ),

        "planned_start_at_ist": to_ist(
            trip.planned_start_at
        ),
    }
# ============================================================
# START TRIP
# ============================================================

@router.post("/{trip_id}/start")
async def start_trip(
    trip_id: str,
    request: Request,   
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
    refresh_hub = get_api_refresh_hub(request.app)
    await refresh_hub.cancel_scheduled(f"trip-start-{trip.id}")
    await publish_trip_event(
        refresh_hub,
        session,
        event="trip.started",
        trip_id=trip.id,
        data={"route_id": trip.route_id},
        broadcast_catalog=True,
    )
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

    refresh_hub = get_api_refresh_hub(request.app)
    stop_event_name = (
        "trip.stop_arrived" if mode == "arrive" else "trip.stop_departed"
    )
    stop_event_data = {
        "route_id": trip.route_id,
        "stop_id": stop_id,
        "sequence_no": current_sequence,
        "mode": mode,
    }

    # Arrival and its resulting departure eligibility are operational state,
    # so publish them immediately after the arrival commit. Passenger
    # notification work below must not delay or suppress the driver's action.
    if mode == "arrive":
        await publish_trip_event(
            refresh_hub,
            session,
            event=stop_event_name,
            trip_id=trip.id,
            data=stop_event_data,
        )
        await publish_departure_allowed_if_eligible(
            refresh_hub,
            session,
            trip_id=trip.id,
            stop_id=stop_id,
        )

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

    if mode == "depart":
        await publish_trip_event(
            refresh_hub,
            session,
            event=stop_event_name,
            trip_id=trip.id,
            data=stop_event_data,
        )
        await refresh_hub.cancel_scheduled(
            f"trip-depart-{trip.id}-{stop_id}"
        )
        await schedule_next_stop_departure_check(
            refresh_hub,
            session,
            trip=trip,
            departed_route_stop=route_stop,
            departed_at=current_time,
        )

    return {
        "message": f"{mode} success",
        "time": to_ist(current_time),
        "distance_from_stop_meters": int(distance)
    }

# ============================================================
# END TRIP (WITH GEO + PASSENGER + STOP VALIDATION)
# ============================================================

# ============================================================
# END TRIP (WITH GEO + PASSENGER + RFID VALIDATION)
# ============================================================

@router.post("/{trip_id}/end")
async def end_trip(
    trip_id: str,
    request: Request,
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
        e.stop_id
        for e in trip_events
        if e.arrival_time is None
        or e.departure_time is None
    ]

    if incomplete_stops:
        raise HTTPException(
            400,
            (
                f"Stops incomplete: "
                f"{len(incomplete_stops)} stop(s) "
                f"missing arrival/departure"
            )
        )

    # =====================================================
    # NORMAL BOOKED PASSENGER CHECK
    # =====================================================
    boarded_result = await session.execute(
        select(func.count())
        .select_from(TripScanEvent)
        .where(
            TripScanEvent.scheduled_trip_id == trip_id,
            TripScanEvent.scan_type == ScanType.BOARD
        )
    )

    boarded_count = boarded_result.scalar() or 0

    dropped_result = await session.execute(
        select(func.count())
        .select_from(TripScanEvent)
        .where(
            TripScanEvent.scheduled_trip_id == trip_id,
            TripScanEvent.scan_type == ScanType.DROP
        )
    )

    dropped_count = dropped_result.scalar() or 0

    remaining_normal_passengers = (
        boarded_count - dropped_count
    )

    if remaining_normal_passengers > 0:
        raise HTTPException(
            400,
            (
                f"{remaining_normal_passengers} "
                f"normal passenger(s) still inside bus"
            )
        )

    # =====================================================
    # RFID PASSENGER SETTLEMENT
    # =====================================================
    # Missing RFID drop scans are settled after all trip-end
    # validations pass, so failed end attempts do not mutate
    # RFID wallets/rides.
    # =====================================================
    rfid_settlement_summary = {
        "settled_count": 0,
        "settled_amount": "0.00",
        "settled_ride_ids": [],
    }

    # =========================
    # TIME CHECK (STRICT)
    # =========================
    if current_time < trip.planned_end_at:
        raise HTTPException(
            400,
            (
                "Too early to end trip. "
                f"Planned end at "
                f"{to_ist(trip.planned_end_at)}"
            )
        )

    # =========================
    # GET LAST STOP
    # =========================
    result = await session.execute(
        select(RouteStop)
        .where(
            RouteStop.route_id == trip.route_id
        )
        .order_by(
            RouteStop.sequence_no.desc()
        )
    )

    last_stop_rs = result.scalars().first()

    if not last_stop_rs:
        raise HTTPException(
            400,
            "No stops found for route"
        )

    last_stop = await session.get(
        Stop,
        last_stop_rs.stop_id
    )

    if not last_stop:
        raise HTTPException(
            400,
            "Last stop not found"
        )

    # =========================
    # GEO VALIDATION
    # =========================
    distance = geodesic(
        (
            float(last_stop.lat),
            float(last_stop.lng)
        ),
        (lat, lng)
    ).meters

    base_radius = (
        last_stop.radius_meters or 0
    )

    gps_buffer = 50

    allowed_radius = (
        base_radius + gps_buffer
    )

    if distance > allowed_radius:
        raise HTTPException(
            400,
            (
                "Driver not at last stop | "
                f"distance={round(distance, 2)}m | "
                f"allowed={allowed_radius}m"
            )
        )

    # =====================================================
    # SETTLE RFID RIDES MISSING DROP SCAN
    # =====================================================
    try:
        rfid_settlement_summary = (
            await RFIDScanService(
                session
            ).settle_unclosed_rfid_rides_for_scheduled_trip(
                scheduled_trip_id=trip_id,
            )
        )

    except ValueError as exc:
        raise HTTPException(
            400,
            str(exc),
        ) from exc

    # =========================
    # UPDATE TRIP
    # =========================
    trip.actual_end_at = current_time
    trip.ended_near_stop_id = last_stop.id
    trip.ended_at_lat = lat
    trip.ended_at_long = lng
    trip.status = ScheduledTripStatus.COMPLETED

    await session.commit()

    refresh_hub = get_api_refresh_hub(request.app)
    await publish_trip_event(
        refresh_hub,
        session,
        event="trip.completed",
        trip_id=trip.id,
        data={"route_id": trip.route_id},
        broadcast_catalog=True,
    )

    # =========================
    # RESPONSE
    # =========================
    return {
        "message": "Trip completed",

        "time": to_ist(
            trip.actual_end_at
        ),

        "geo_debug": {
            "distance_m": round(distance, 2),
            "allowed_radius_m": allowed_radius
        },

        "normal_passenger_check": {
            "boarded": boarded_count,
            "dropped": dropped_count,
            "remaining": remaining_normal_passengers
        },

        "rfid_passenger_check": {
            "auto_settled_missing_drop_rides": (
                rfid_settlement_summary["settled_count"]
            ),

            "auto_settled_amount": (
                rfid_settlement_summary["settled_amount"]
            ),

            "auto_settled_ride_ids": (
                rfid_settlement_summary["settled_ride_ids"]
            ),
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

    refresh_hub = get_api_refresh_hub(request.app)
    await publish_trip_event(
        refresh_hub,
        session,
        event="trip.premature_ended",
        trip_id=trip.id,
        data={
            "route_id": trip.route_id,
            "reason": reason,
        },
        broadcast_catalog=True,
    )

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
