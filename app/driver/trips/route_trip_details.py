# app/driver/trips/route_trip_details.py

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.db.database import get_async_session
from app.db.schema import Route, ScheduledTrip, RouteStop, Stop

router = APIRouter()


@router.get("/routes/{route_id}/trips/details")
async def get_route_trip_details(
    route_id: str,
    db: AsyncSession = Depends(get_async_session),
):
    # 1️⃣ Get Route with Stops
    route_result = await db.execute(
        select(Route)
        .options(
            selectinload(Route.route_stops).selectinload(RouteStop.stop)
        )
        .where(Route.id == route_id)
    )
    route = route_result.scalar_one_or_none()

    if not route:
        raise HTTPException(status_code=404, detail="Route not found")

    # 2️⃣ Get Trips with TripEvents
    trip_result = await db.execute(
        select(ScheduledTrip)
        .options(
            selectinload(ScheduledTrip.trip_events),
            selectinload(ScheduledTrip.driver),
            selectinload(ScheduledTrip.vehicle),
        )
        .where(ScheduledTrip.route_id == route_id)
    )

    trips = trip_result.scalars().all()

    # 3️⃣ Build response
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
                for rs in route.route_stops
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