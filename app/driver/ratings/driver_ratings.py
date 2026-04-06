# app/driver/ratings/driver_ratings.py

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.db.database import get_async_session
from app.auth.dependencies import get_current_user
from app.db.schema import (
    BookingRating,
    User,
    UserRole,
)

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
    # FETCH RATINGS
    # =========================
    stmt = (
        select(BookingRating)
        .where(BookingRating.driver_user_id == current_user.id)
        .options(selectinload(BookingRating.passenger))
        .order_by(BookingRating.created_at.desc())
    )

    result = await session.execute(stmt)
    ratings = result.scalars().all()

    # =========================
    # AGGREGATE (IMPORTANT 🔥)
    # =========================
    avg_driver_rating = None
    total_reviews = len(ratings)

    if total_reviews > 0:
        avg_stmt = select(func.avg(BookingRating.driver_rating)).where(
            BookingRating.driver_user_id == current_user.id
        )
        avg_result = await session.execute(avg_stmt)
        avg_driver_rating = float(avg_result.scalar() or 0)

    # =========================
    # RESPONSE
    # =========================
    return {
        "summary": {
            "average_driver_rating": avg_driver_rating,
            "total_reviews": total_reviews,
        },
        "reviews": [
            {
                "id": r.id,
                "booking_id": r.booking_id,
                "trip_rating": r.trip_rating,
                "driver_rating": r.driver_rating,
                "review_text": r.review_text,
                "created_at": r.created_at,
                "passenger": {
                    "id": r.passenger.id,
                    "email": r.passenger.email,
                } if r.passenger else None,
            }
            for r in ratings
        ],
    }