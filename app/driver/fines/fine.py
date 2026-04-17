# app/driver/fines/fine.py

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.database import get_async_session
from app.db.schema import (
    User,
    UserRole,
    PayoutAdjustment,
    PayoutAdjustmentType,
    TripBooking,
    BookingTransfer,
)
from app.auth.dependencies import get_current_active_user


router = APIRouter(prefix="/driver/fines", tags=["Driver Fines"])


# ---------------------------
# Serializer
# ---------------------------
def serialize_fine(fine: PayoutAdjustment) -> dict:
    return {
        "id": fine.id,
        "amount": float(fine.amount),
        "reason": fine.reason_text,  # ✅ FIXED
        "status": fine.decision_status,  # ✅ FIXED
        "created_at": fine.created_at,
        "decided_at": fine.decided_at,
    }


# ---------------------------
# GET Driver Fines
# ---------------------------
@router.get("")
async def get_driver_fines(
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    # ✅ Only driver allowed
    if current_user.role != UserRole.DRIVER:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "driver_only",
                "message": "Only drivers can access fines.",
            },
        )

    # ✅ CORRECT JOIN FLOW
    stmt = (
        select(PayoutAdjustment)
        .join(TripBooking, PayoutAdjustment.origin_booking_id == TripBooking.id)
        .join(BookingTransfer, BookingTransfer.booking_id == TripBooking.id)
        .where(
            BookingTransfer.driver_user_id == current_user.id,
            PayoutAdjustment.adjustment_type == PayoutAdjustmentType.FINE,
        )
        .order_by(PayoutAdjustment.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    result = await db.execute(stmt)
    fines = result.scalars().all()

    total_amount = sum(float(f.amount) for f in fines)

    return {
        "items": [serialize_fine(f) for f in fines],
        "count": len(fines),
        "total_amount": total_amount,
    }