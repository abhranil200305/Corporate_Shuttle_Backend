# app/driver/fines/fine.py

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case
from decimal import Decimal
import json

from app.db.database import get_async_session
from app.db.schema import (
    User,
    UserRole,
    PayoutAdjustment,
    PayoutAdjustmentType,
    PayoutAdjustmentDecision,
    TripBooking,
    ScheduledTrip,
    PlatformSettings,
)
from app.auth.dependencies import get_current_active_user


router = APIRouter(prefix="/driver/fines", tags=["Driver Fines"])


# ---------------------------
# Status Mapping (Driver Friendly)
# ---------------------------
def map_status(status: PayoutAdjustmentDecision) -> str:
    return {
        PayoutAdjustmentDecision.PENDING: "under_review",
        PayoutAdjustmentDecision.INCLUDED: "applied",
        PayoutAdjustmentDecision.EXCLUDED: "rejected",
    }.get(status, str(status))


# ---------------------------
# Serializer
# ---------------------------
def serialize_fine(fine: PayoutAdjustment) -> dict:
    return {
        "id": fine.id,
        "booking_id": fine.origin_booking_id,
        "amount": str(fine.amount),  # ✅ FIXED (no float)
        "reason": fine.reason_text,
        "reason_code": fine.reason_code,
        "status": map_status(fine.decision_status),
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
    status: PayoutAdjustmentDecision | None = Query(default=None),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    if current_user.role != UserRole.DRIVER:
        raise HTTPException(
            status_code=403,
            detail={"error": "driver_only", "message": "Only drivers can access fines."},
        )

    stmt = (
        select(PayoutAdjustment)
        .join(TripBooking, PayoutAdjustment.origin_booking_id == TripBooking.id)
        .join(ScheduledTrip, TripBooking.scheduled_trip_id == ScheduledTrip.id)
        .where(
            ScheduledTrip.driver_user_id == current_user.id,
            PayoutAdjustment.adjustment_type == PayoutAdjustmentType.FINE,
        )
    )

    if status:
        stmt = stmt.where(PayoutAdjustment.decision_status == status)

    stmt = stmt.order_by(PayoutAdjustment.created_at.desc()).limit(limit).offset(offset)

    result = await db.execute(stmt)
    fines = result.scalars().all()

    page_total_amount = sum((f.amount for f in fines), Decimal("0.00"))

    return {
        "items": [serialize_fine(f) for f in fines],
        "count": len(fines),
        "page_total_amount": str(page_total_amount),  # ✅ FIXED
    }


# ============================================================
# fines total summary (OPTIMIZED)
# ============================================================

@router.get("/summary")
async def get_driver_fines_summary(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    if current_user.role != UserRole.DRIVER:
        raise HTTPException(
            status_code=403,
            detail={"error": "driver_only", "message": "Only drivers can access fines."},
        )

    stmt = (
        select(
            func.coalesce(
                func.sum(
                    case(
                        (PayoutAdjustment.decision_status == PayoutAdjustmentDecision.PENDING, PayoutAdjustment.amount),
                        else_=0,
                    )
                ),
                0,
            ).label("total_pending"),

            func.coalesce(
                func.sum(
                    case(
                        (PayoutAdjustment.decision_status == PayoutAdjustmentDecision.INCLUDED, PayoutAdjustment.amount),
                        else_=0,
                    )
                ),
                0,
            ).label("total_applied"),

            func.coalesce(
                func.sum(
                    case(
                        (PayoutAdjustment.decision_status == PayoutAdjustmentDecision.EXCLUDED, PayoutAdjustment.amount),
                        else_=0,
                    )
                ),
                0,
            ).label("total_rejected"),
        )
        .join(TripBooking, PayoutAdjustment.origin_booking_id == TripBooking.id)
        .join(ScheduledTrip, TripBooking.scheduled_trip_id == ScheduledTrip.id)
        .where(
            ScheduledTrip.driver_user_id == current_user.id,
            PayoutAdjustment.adjustment_type == PayoutAdjustmentType.FINE,
        )
    )

    result = await db.execute(stmt)
    row = result.one()

    return {
        "total_pending": str(row.total_pending),
        "total_applied": str(row.total_applied),
        "total_rejected": str(row.total_rejected),
    }


# ============================================================
# RULE MESSAGE HELPER (Optional UX)
# ============================================================

def build_rule_message(rule: dict) -> str:
    config = rule.get("config", {})
    rule_type = rule.get("rule_type")

    if rule_type == "driver_trip_cancel":
        return f"Cancel within {config.get('max_minutes_before')} mins → fine {config.get('fine_value')}"

    if rule_type == "trip_latency":
        return f"Late beyond {config.get('grace_minutes')} mins → fine applies"

    return "Fine rule applied"


# ============================================================
# fine_rules
# ============================================================

@router.get("/rules")
async def get_driver_fine_rules(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    if current_user.role != UserRole.DRIVER:
        raise HTTPException(
            status_code=403,
            detail={"error": "driver_only", "message": "Only drivers can access fine rules."},
        )

    stmt = (
        select(PlatformSettings)
        .where(PlatformSettings.settings_key == "default")
        .limit(1)
    )

    result = await db.execute(stmt)
    settings = result.scalar_one_or_none()

    if not settings or not settings.commercial_policy_json:
        return {"rules": [], "count": 0}

    try:
        parsed = json.loads(settings.commercial_policy_json)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "invalid_policy_json",
                "message": "Commercial policy JSON is invalid.",
            },
        )

    rules_output = []

    for rule in parsed.get("rules", []):
        if not isinstance(rule, dict):
            continue
        if not rule.get("is_active"):
            continue

        rules_output.append({
            "id": rule.get("id"),
            "code": rule.get("code"),
            "title": rule.get("title"),
            "type": rule.get("rule_type"),
            "priority": rule.get("priority"),
            "config": rule.get("config"),
            "message": build_rule_message(rule),  # ✅ added
        })

    # ✅ sort by priority
    rules_output.sort(key=lambda r: r.get("priority", 100))

    return {
        "rules": rules_output,
        "count": len(rules_output),
    }