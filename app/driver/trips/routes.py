# app/driver/trips/routes.py

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.database import get_async_session
from app.db.schema import Route, RouteStop, Vehicle, User
from app.auth.dependencies import get_current_active_user

router = APIRouter(prefix="/driver/routes", tags=["Driver Routes"])


@router.get("/")
async def get_all_routes(
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_active_user),  # ✅ get driver
):
    # ---------------------------------------------------
    # 1. Get driver's vehicle
    # ---------------------------------------------------
    vehicle_query = select(Vehicle).where(
        Vehicle.driver_user_id == current_user.id,
        Vehicle.is_active.is_(True)
    )
    vehicle_result = await session.execute(vehicle_query)
    vehicle = vehicle_result.scalar_one_or_none()

    if not vehicle:
        raise HTTPException(
            status_code=400,
            detail="Driver vehicle not found. Please register vehicle first."
        )

    # ---------------------------------------------------
    # 2. Filter routes based on AC / NON-AC
    # ---------------------------------------------------
    query = (
        select(Route)
        .where(
            Route.is_active.is_(True),
            Route.has_ac == vehicle.has_ac   # ✅ MAIN LOGIC
        )
        .options(
            selectinload(Route.route_stops)
            .selectinload(RouteStop.stop)
        )
    )

    result = await session.execute(query)
    routes = result.scalars().unique().all()

    # ---------------------------------------------------
    # 3. Prepare response
    # ---------------------------------------------------
    response = []

    for route in routes:
        sorted_stops = sorted(
            route.route_stops,
            key=lambda x: x.sequence_no or 0
        )

        stops_data = [
            {
                "stop_id": rs.stop.id,
                "name": rs.stop.name,
                "sequence_no": rs.sequence_no,
                "assume_time_diff_minutes": rs.assume_time_diff_minutes,
                "boarding_allowed": rs.boarding_allowed,
                "deboarding_allowed": rs.deboarding_allowed,
            }
            for rs in sorted_stops
        ]

        response.append({
            "route_id": route.id,
            "name": route.name,
            "code": route.code,
            "has_ac": route.has_ac,
            "stops": stops_data
        })

    return response