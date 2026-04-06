# app/driver/ratings/driver_ratings.py

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.db.database import get_async_session
from app.auth.dependencies import get_current_user
from app.db.schema import BookingRating, ScheduledTrip, User, UserRole

router = APIRouter(prefix="/driver/ratings", tags=["driver-ratings"])


@router.get("")
async def get_driver_ratings(
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    # =========================
    # ROLE CHECK
    # =========================
    if current_user.role != UserRole.DRIVER:
        raise HTTPException(status_code=403, detail="Only drivers allowed")

    # =========================
    # FETCH RATINGS (with trip)
    # =========================
    stmt = (
        select(BookingRating)
        .where(BookingRating.driver_user_id == current_user.id)
        .options(selectinload(BookingRating.scheduled_trip))  # load trip info
    )

    result = await session.execute(stmt)
    ratings = result.scalars().all()

    if not ratings:
        return {
            "average_driver_rating": None,
            "total_reviews": 0,
            "reviews": [],
        }

    # =========================
    # AGGREGATE AVERAGE DRIVER RATING
    # =========================
    total_reviews = len(ratings)
    avg_driver_rating = sum(r.driver_rating for r in ratings) / total_reviews

    # =========================
    # GROUP REVIEWS BY TRIP
    # =========================
    reviews_by_trip = {}
    for r in ratings:
        trip_id = r.scheduled_trip_id
        trip_name = getattr(r.scheduled_trip, "route_id", trip_id)  # use route_id if name unavailable
        if trip_id not in reviews_by_trip:
            reviews_by_trip[trip_id] = {
                "trip_id": trip_id,
                "trip_name": trip_name,
                "reviews": [],
            }
        if r.review_text:  # only include non-empty reviews
            reviews_by_trip[trip_id]["reviews"].append(r.review_text)

    # =========================
    # RESPONSE
    # =========================
    return {
        "average_driver_rating": round(avg_driver_rating, 2),
        "total_reviews": total_reviews,
        "trips": list(reviews_by_trip.values()),
    }