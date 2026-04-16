# app/driver/trips/near_stop.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from math import radians, cos, sin, asin, sqrt

from app.db.database import get_async_session
from app.db.schema import (
    ScheduledTrip,
    RouteStop,
    Route,   # ✅ IMPORTANT (added)
    Stop,
    User,
)
from app.auth.dependencies import get_current_user

router = APIRouter(prefix="/driver/trips", tags=["Driver Trips"])


# ---------------------------------------
# Haversine Distance (in meters)
# ---------------------------------------
def haversine_distance(lat1, lng1, lat2, lng2):
    R = 6371000  # Earth radius in meters

    lat1, lng1, lat2, lng2 = map(radians, [lat1, lng1, lat2, lng2])

    dlat = lat2 - lat1
    dlng = lng2 - lng1

    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
    c = 2 * asin(sqrt(a))

    return R * c


# ---------------------------------------
# API: Check Near Stop
# ---------------------------------------
@router.get("/{trip_id}/near-stop")
async def check_near_stop(
    trip_id: str,
    lat: float,
    lng: float,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    # ---------------------------------------
    # 1. Get trip with route + stops
    # ---------------------------------------
    stmt = (
        select(ScheduledTrip)
        .where(ScheduledTrip.id == trip_id)
        .options(
            selectinload(ScheduledTrip.route)
            .selectinload(Route.route_stops)   # ✅ FIXED
            .selectinload(RouteStop.stop)
        )
    )

    result = await session.execute(stmt)
    trip = result.scalar_one_or_none()

    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    if not trip.route:
        raise HTTPException(status_code=400, detail="Trip has no route")

    # ---------------------------------------
    # 2. Sort stops by sequence
    # ---------------------------------------
    route_stops = sorted(
        trip.route.route_stops,
        key=lambda x: x.sequence_no
    )

    if not route_stops:
        raise HTTPException(status_code=400, detail="No stops found")

    # ---------------------------------------
    # 3. Find nearest stop
    # ---------------------------------------
    nearest_stop = None
    min_distance = float("inf")

    for rs in route_stops:
        stop: Stop = rs.stop

        distance = haversine_distance(
            lat,
            lng,
            float(stop.lat),
            float(stop.lng),
        )

        if distance < min_distance:
            min_distance = distance
            nearest_stop = stop

    # ---------------------------------------
    # 4. Check radius
    # ---------------------------------------
    if nearest_stop is None:
        raise HTTPException(status_code=404, detail="No stop found")

    if min_distance > nearest_stop.radius_meters:
        raise HTTPException(
            status_code=200,
            detail={
                "message": "Not within stop radius",
                "distance_meters": round(min_distance, 2),
                "allowed_radius": nearest_stop.radius_meters,
            },
        )

    # ---------------------------------------
    # 5. Inside radius → SUCCESS
    # ---------------------------------------
    return {
        "message": "You are near the stop",
        "stop": {
            "id": nearest_stop.id,
            "name": nearest_stop.name,
            "lat": float(nearest_stop.lat),
            "lng": float(nearest_stop.lng),
            "radius_meters": nearest_stop.radius_meters,
        },
        "distance_meters": round(min_distance, 2),
    }