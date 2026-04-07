# app/driver/trips/route_stops.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import timedelta

from app.db.schema import ScheduledTrip, RouteStop
from app.auth.dependencies import get_async_session, get_current_user
from app.db.schema import User

router = APIRouter(prefix="/driver/trips", tags=["trips"])

@router.get("/{trip_id}/stops")
async def get_trip_stops(
    trip_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    # Fetch trip with related route_stops
    result = await db.execute(
        """
        SELECT rt.id AS route_stop_id, rt.sequence_no, rt.assume_time_diff_minutes,
               s.id AS stop_id, s.name, s.lat, s.lng
        FROM route_stops rt
        JOIN stops s ON s.id = rt.stop_id
        JOIN routes r ON r.id = rt.route_id
        JOIN scheduled_trips st ON st.route_id = r.id
        WHERE st.id = :trip_id
        ORDER BY rt.sequence_no ASC
        """,
        {"trip_id": trip_id},
    )
    rows = result.fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="Trip or stops not found.")

    # Calculate cumulative minutes
    cumulative = 0
    stops_list = []
    for row in rows:
        cumulative += row.assume_time_diff_minutes or 0
        stops_list.append({
            "route_stop_id": row.route_stop_id,
            "sequence_no": row.sequence_no,
            "assume_time_diff_minutes": row.assume_time_diff_minutes,
            "minutes_from_trip_start": cumulative,
            "stop": {
                "id": row.stop_id,
                "name": row.name,
                "lat": row.lat,
                "lng": row.lng,
            }
        })

    return {"trip_id": trip_id, "stops": stops_list}