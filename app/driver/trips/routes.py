# app/driver/trips/routes.py

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.database import get_async_session
from app.db.schema import Route, RouteStop

router = APIRouter(prefix="/driver/routes", tags=["Driver Routes"])


@router.get("/")
async def get_all_routes(
    session: AsyncSession = Depends(get_async_session),
):
    query = (
        select(Route)
        .where(Route.is_active.is_(True))  # ✅ better boolean check
        .options(
            selectinload(Route.route_stops)
            .selectinload(RouteStop.stop)
        )
    )

    result = await session.execute(query)
    routes = result.scalars().unique().all()

    response = []

    for route in routes:
        # ✅ Sort stops safely
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
            "has_ac": route.has_ac,  # ✅ ADDED FIELD
            "stops": stops_data
        })

    return response