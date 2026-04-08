# app/driver/trips/routes.py

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.database import get_async_session
from app.db.schema import Route, RouteStop, Stop

router = APIRouter(prefix="/driver/routes", tags=["Driver Routes"])


@router.get("/")
async def get_all_routes(
    session: AsyncSession = Depends(get_async_session),
):
    query = (
        select(Route)
        .where(Route.is_active == True)
        .options(
            selectinload(Route.route_stops)
            .selectinload(RouteStop.stop)
        )
    )

    result = await session.execute(query)
    routes = result.scalars().unique().all()

    response = []

    for route in routes:
        stops_data = []

        # Sort stops by sequence_no
        sorted_stops = sorted(route.route_stops, key=lambda x: x.sequence_no)

        for rs in sorted_stops:
            stops_data.append({
                "stop_id": rs.stop.id,
                "name": rs.stop.name,
                "sequence_no": rs.sequence_no,
                "assume_time_diff_minutes": rs.assume_time_diff_minutes,  # ✅ added correctly
                "boarding_allowed": rs.boarding_allowed,
                "deboarding_allowed": rs.deboarding_allowed
            })

        response.append({
            "route_id": route.id,
            "name": route.name,
            "code": route.code,
            "stops": stops_data
        })

    return response