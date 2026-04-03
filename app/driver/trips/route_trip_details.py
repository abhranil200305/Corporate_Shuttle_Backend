# app/driver/trips/route_trip_details.py

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from datetime import timedelta

from app.db.database import get_async_session
from app.db.schema import Route, ScheduledTrip, RouteStop, User
from app.auth.dependencies import get_current_user

router = APIRouter()


@router.get("/routes/{route_id}/trips/details")
async def get_route_trip_details(
    route_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),  # ✅ AUTH
):
    # 🔒 Allow only driver
    if current_user.role != "driver":
        raise HTTPException(status_code=403, detail="Only drivers allowed")

    # 1️⃣ Get Route with Stops + Fares
    route_result = await db.execute(
        select(Route)
        .options(
            selectinload(Route.route_stops).selectinload(RouteStop.stop),
            selectinload(Route.fares),
        )
        .where(Route.id == route_id)
    )
    route = route_result.scalar_one_or_none()

    if not route:
        raise HTTPException(status_code=404, detail="Route not found")

    # ✅ Sort stops
    route_stops = sorted(route.route_stops, key=lambda x: x.sequence_no)

    # 2️⃣ Compute cumulative time
    cumulative_time = 0
    stop_time_map = {}

    for rs in route_stops:
        cumulative_time += rs.assume_time_diff_minutes or 0
        stop_time_map[rs.stop.id] = cumulative_time

    # 3️⃣ Create fare map
    fare_map = {}
    for fare in route.fares:
        fare_map[(fare.pickup_stop_id, fare.dropoff_stop_id)] = float(fare.amount)

    # 4️⃣ Get ONLY this driver's trips
    trip_result = await db.execute(
        select(ScheduledTrip)
        .options(
            selectinload(ScheduledTrip.trip_events),
            selectinload(ScheduledTrip.driver),
            selectinload(ScheduledTrip.vehicle),
        )
        .where(
            ScheduledTrip.route_id == route_id,
            ScheduledTrip.driver_user_id == current_user.id  # 🔥 KEY FIX
        )
    )

    trips = trip_result.scalars().all()

    # 5️⃣ Build response
    return {
        "route": {
            "id": route.id,
            "name": route.name,
            "stops": [
                {
                    "sequence": rs.sequence_no,
                    "stop_id": rs.stop.id,
                    "stop_name": rs.stop.name,
                }
                for rs in route_stops
            ],
        },
        "trips": [
            {
                "trip_id": trip.id,
                "status": trip.status,
                "planned_start": trip.planned_start_at,
                "planned_end": trip.planned_end_at,
                "actual_start": trip.actual_start_at,
                "actual_end": trip.actual_end_at,

                # 🚀 Stops with timing + fares
                "stops": [
                    {
                        "sequence": rs.sequence_no,
                        "stop_id": rs.stop.id,
                        "stop_name": rs.stop.name,

                        # 🕒 Planned Time
                        "planned_arrival_time": (
                            trip.planned_start_at + timedelta(
                                minutes=stop_time_map.get(rs.stop.id, 0)
                            )
                            if trip.planned_start_at
                            else None
                        ),

                        # 📍 Actual Time
                        "actual_arrival_time": next(
                            (
                                e.arrival_time
                                for e in trip.trip_events
                                if e.stop_id == rs.stop.id
                            ),
                            None,
                        ),
                        "actual_departure_time": next(
                            (
                                e.departure_time
                                for e in trip.trip_events
                                if e.stop_id == rs.stop.id
                            ),
                            None,
                        ),

                        # 💰 Fare from this stop → next stops
                        "fares": [
                            {
                                "to_stop_id": next_rs.stop.id,
                                "to_stop_name": next_rs.stop.name,
                                "amount": fare_map.get(
                                    (rs.stop.id, next_rs.stop.id)
                                ),
                            }
                            for next_rs in route_stops
                            if next_rs.sequence_no > rs.sequence_no
                        ],
                    }
                    for rs in route_stops
                ],

                # 🔁 Raw events
                "events": [
                    {
                        "stop_id": event.stop_id,
                        "arrival_time": event.arrival_time,
                        "departure_time": event.departure_time,
                    }
                    for event in trip.trip_events
                ],
            }
            for trip in trips
        ],
    }