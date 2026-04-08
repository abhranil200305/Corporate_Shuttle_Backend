# app/driver/trips/trip_details.py

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.database import get_async_session
from app.db.schema import (
    ScheduledTrip,
    Route,
    RouteStop,
    Stop,
    TripEvent,
    User,
    Vehicle,
)

from app.auth.dependencies import get_current_user

router = APIRouter()


@router.get("/{trip_id}/details")
async def get_trip_details(
    trip_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    # 1️⃣ Fetch trip with route + stops + vehicle + driver
    result = await db.execute(
        select(ScheduledTrip)
        .where(ScheduledTrip.id == trip_id)
        .options(
            selectinload(ScheduledTrip.route)
            .selectinload(Route.route_stops)
            .selectinload(RouteStop.stop),
            selectinload(ScheduledTrip.vehicle),
            selectinload(ScheduledTrip.driver),
        )
    )

    trip = result.scalar_one_or_none()

    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    # 🔒 Driver access check
    if trip.driver_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not allowed")

    # 2️⃣ Fetch trip events (actual timings)
    events_result = await db.execute(
        select(TripEvent).where(TripEvent.scheduled_trip_id == trip_id)
    )
    events = events_result.scalars().all()

    # Convert events to dict for fast lookup
    event_map = {
        e.stop_id: {
            "arrival_time": e.arrival_time,
            "departure_time": e.departure_time,
        }
        for e in events
    }

    # 3️⃣ Build stops list (sorted + merged planned + actual)
    stops_data = []

    sorted_stops = sorted(trip.route.route_stops, key=lambda x: x.sequence_no)

    for rs in sorted_stops:
        stop = rs.stop
        event = event_map.get(stop.id)

        stops_data.append(
            {
                "stop_id": stop.id,
                "name": stop.name,
                "lat": float(stop.lat),
                "lng": float(stop.lng),
                "sequence": rs.sequence_no,
                "assume_time_diff_minutes": rs.assume_time_diff_minutes,  # ✅ added
                "boarding_allowed": rs.boarding_allowed,
                "deboarding_allowed": rs.deboarding_allowed,
                "arrival_time": event["arrival_time"] if event else None,
                "departure_time": event["departure_time"] if event else None,
            }
        )

    # 4️⃣ Final response
    return {
        "trip_id": trip.id,
        "status": trip.status,
        "planned_start": trip.planned_start_at,
        "planned_end": trip.planned_end_at,
        "actual_start": trip.actual_start_at,
        "actual_end": trip.actual_end_at,

        "driver": {
            "driver_id": trip.driver.id,
            "email": trip.driver.email,
        },

        "vehicle": {
            "vehicle_id": trip.vehicle.id,
            "name": trip.vehicle.vehicle_name,
            "model": trip.vehicle.vehicle_model,
            "registration_number": trip.vehicle.registration_number,
        },

        "route": {
            "route_id": trip.route.id,
            "name": trip.route.name,
            "stops": stops_data,
        },
    }