# app/driver/trips/payout_details.py

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.dependencies import get_current_user
from app.db.database import get_async_session
from app.db.schema import (
    BookingStatus,
    BookingTransferStatus,
    DriverPayoutDetails,
    PayoutAdjustmentApplication,
    ScheduledTrip,
    TransferStatus,
    TripBooking,
    User,
    UserRole,
)

router = APIRouter()

IST = timezone(timedelta(hours=5, minutes=30))


def _money(value: Decimal | None) -> Decimal:
    return (value or Decimal("0.00")).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def _validate_month_year_pair(
    *,
    month: int | None,
    year: int | None,
    label: str,
) -> None:
    if (month is None) != (year is None):
        raise HTTPException(
            status_code=400,
            detail=f"{label}_month and {label}_year must be provided together",
        )

    if month is not None and not (1 <= month <= 12):
        raise HTTPException(
            status_code=400,
            detail=f"{label}_month must be between 1 and 12",
        )

    if year is not None and not (2000 <= year <= 2100):
        raise HTTPException(
            status_code=400,
            detail=f"{label}_year must be between 2000 and 2100",
        )


def _month_start_ist(year: int, month: int) -> datetime:
    return datetime(year, month, 1, 0, 0, 0, tzinfo=IST)


def _next_month_start_ist(year: int, month: int) -> datetime:
    if month == 12:
        return datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=IST)
    return datetime(year, month + 1, 1, 0, 0, 0, tzinfo=IST)


def _build_trip_window_utc(
    *,
    from_month: int | None,
    from_year: int | None,
    to_month: int | None,
    to_year: int | None,
) -> tuple[datetime | None, datetime | None]:
    _validate_month_year_pair(month=from_month, year=from_year, label="from")
    _validate_month_year_pair(month=to_month, year=to_year, label="to")

    start_utc = None
    end_utc = None

    if from_month is not None and from_year is not None:
        start_utc = _month_start_ist(from_year, from_month).astimezone(timezone.utc)

    if to_month is not None and to_year is not None:
        end_utc = _next_month_start_ist(to_year, to_month).astimezone(timezone.utc)

    if start_utc is not None and end_utc is not None and start_utc >= end_utc:
        raise HTTPException(
            status_code=400,
            detail="from month/year must be before or equal to to month/year",
        )

    return start_utc, end_utc


def _derive_payout_status(booking: TripBooking) -> str:
    transfer = booking.transfer

    if booking.transfer_status == TransferStatus.TRANSFERRED:
        return "paid"

    if transfer is not None and transfer.status == BookingTransferStatus.PROCESSED:
        return "paid"

    if booking.transfer_status == TransferStatus.REVERSED:
        return "reversed"

    if transfer is not None and transfer.status == BookingTransferStatus.REVERSED:
        return "reversed"

    return "pending"


def _serialize_driver_payout_profile(
    payout_details: DriverPayoutDetails | None,
) -> dict | None:
    if payout_details is None:
        return None

    account_number = (payout_details.bank_account_number or "").strip()
    masked_account_number = None
    if account_number:
        if len(account_number) <= 4:
            masked_account_number = account_number
        else:
            masked_account_number = f"{'*' * (len(account_number) - 4)}{account_number[-4:]}"

    return {
        "account_holder_name": payout_details.account_holder_name,
        "masked_bank_account_number": masked_account_number,
        "ifsc_code": payout_details.ifsc_code,
        "phone_number": payout_details.phone_number,
        "linked_account_status": payout_details.linked_account_status.value,
        "is_payout_eligible": payout_details.is_payout_eligible,
        "has_linked_account_id": bool(
            (payout_details.razorpay_linked_account_id or "").strip()
        ),
    }


def _resolve_passenger_name(booking: TripBooking) -> str:
    passenger = booking.passenger
    if passenger is None:
        return booking.passenger_user_id

    if passenger.passenger_profile is not None:
        full_name = (passenger.passenger_profile.full_name or "").strip()
        if full_name:
            return full_name

    return passenger.email


def _get_applied_adjustment_amount(booking: TripBooking) -> Decimal:
    total = Decimal("0.00")

    for application in (
        getattr(booking, "applied_payout_adjustment_applications", []) or []
    ):
        total += _money(application.applied_amount)

    return _money(total)


def _serialize_applied_adjustment(
    application: PayoutAdjustmentApplication,
) -> dict:
    adjustment = application.adjustment

    return {
        "application_id": application.id,
        "payout_adjustment_id": application.payout_adjustment_id,
        "applied_on_booking_id": application.applied_on_booking_id,
        "booking_transfer_id": application.booking_transfer_id,
        "applied_by_admin_id": application.applied_by_admin_id,
        "applied_amount": _money(application.applied_amount),
        "applied_at": application.applied_at,
        "created_at": application.created_at,
        "updated_at": application.updated_at,
        "adjustment": None if adjustment is None else {
            "adjustment_id": adjustment.id,
            "origin_booking_id": adjustment.origin_booking_id,
            "adjustment_type": adjustment.adjustment_type.value,
            "amount": _money(adjustment.amount),
            "reason_code": adjustment.reason_code,
            "reason_text": adjustment.reason_text,
            "admin_note": adjustment.admin_note,
            "decision_status": adjustment.decision_status.value,
            "created_by_admin_id": adjustment.created_by_admin_id,
            "decided_by_admin_id": adjustment.decided_by_admin_id,
            "decided_at": adjustment.decided_at,
            "created_at": adjustment.created_at,
            "updated_at": adjustment.updated_at,
        },
    }


@router.get("/payout-details")
async def get_driver_payout_details(
    payout_status: str | None = Query(
        default=None,
        pattern="^(pending|paid)$",
        description="Filter by derived payout status: pending or paid",
    ),
    from_month: int | None = Query(default=None),
    from_year: int | None = Query(default=None),
    to_month: int | None = Query(default=None),
    to_year: int | None = Query(default=None),
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.DRIVER:
        raise HTTPException(status_code=403, detail="Only drivers allowed")

    trip_start_utc, trip_end_utc = _build_trip_window_utc(
        from_month=from_month,
        from_year=from_year,
        to_month=to_month,
        to_year=to_year,
    )

    payout_profile_result = await db.execute(
        select(DriverPayoutDetails).where(
            DriverPayoutDetails.driver_user_id == current_user.id
        )
    )
    payout_profile = payout_profile_result.scalar_one_or_none()

    stmt = (
        select(TripBooking)
        .join(ScheduledTrip, TripBooking.scheduled_trip_id == ScheduledTrip.id)
        .where(ScheduledTrip.driver_user_id == current_user.id)
        .where(
            or_(
                TripBooking.booking_status == BookingStatus.COMPLETED,
                TripBooking.driver_payout_amount > 0,
                TripBooking.transfer_status != TransferStatus.NOT_READY,
            )
        )
        .options(
            selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.route),
            selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.vehicle),
            selectinload(TripBooking.pickup_stop),
            selectinload(TripBooking.dropoff_stop),
            selectinload(TripBooking.transfer),
            selectinload(TripBooking.passenger).selectinload(User.passenger_profile),
            selectinload(TripBooking.applied_payout_adjustment_applications)
            .selectinload(PayoutAdjustmentApplication.adjustment),
        )
        .order_by(
            ScheduledTrip.planned_start_at.desc(),
            TripBooking.created_at.desc(),
        )
    )

    if trip_start_utc is not None:
        stmt = stmt.where(ScheduledTrip.planned_start_at >= trip_start_utc)

    if trip_end_utc is not None:
        stmt = stmt.where(ScheduledTrip.planned_start_at < trip_end_utc)

    result = await db.execute(stmt)
    bookings = result.scalars().unique().all()

    filtered_bookings: list[TripBooking] = []
    for booking in bookings:
        derived_status = _derive_payout_status(booking)

        if payout_status is not None and derived_status != payout_status:
            continue

        filtered_bookings.append(booking)

    trips_map: dict[str, dict] = {}

    total_fare_amount = Decimal("0.00")
    total_commission_amount = Decimal("0.00")
    total_driver_payout_amount = Decimal("0.00")
    total_applied_adjustment_amount = Decimal("0.00")
    total_net_payout_amount = Decimal("0.00")
    total_paid_out_amount = Decimal("0.00")
    total_pending_payout_amount = Decimal("0.00")

    for booking in filtered_bookings:
        trip = booking.scheduled_trip
        transfer = booking.transfer
        derived_status = _derive_payout_status(booking)

        fare_amount = _money(booking.fare_amount)
        commission_amount = _money(booking.commission_amount)
        driver_payout_amount = _money(booking.driver_payout_amount)
        applied_adjustment_amount = _get_applied_adjustment_amount(booking)

        net_payout_amount = _money(driver_payout_amount - applied_adjustment_amount)
        if net_payout_amount < Decimal("0.00"):
            net_payout_amount = Decimal("0.00")

        if derived_status == "paid":
            paid_out_amount = _money(
                transfer.amount if transfer is not None else net_payout_amount
            )
            pending_amount = Decimal("0.00")
        elif derived_status == "pending":
            paid_out_amount = Decimal("0.00")
            pending_amount = net_payout_amount
        else:
            paid_out_amount = Decimal("0.00")
            pending_amount = Decimal("0.00")

        total_fare_amount += fare_amount
        total_commission_amount += commission_amount
        total_driver_payout_amount += driver_payout_amount
        total_applied_adjustment_amount += applied_adjustment_amount
        total_net_payout_amount += net_payout_amount
        total_paid_out_amount += paid_out_amount
        total_pending_payout_amount += pending_amount

        if trip.id not in trips_map:
            trips_map[trip.id] = {
                "trip_id": trip.id,
                "planned_start_at": trip.planned_start_at,
                "planned_end_at": trip.planned_end_at,
                "actual_start_at": trip.actual_start_at,
                "actual_end_at": trip.actual_end_at,
                "trip_status": trip.status.value,
                "route": None if trip.route is None else {
                    "route_id": trip.route.id,
                    "route_name": trip.route.name,
                    "route_code": trip.route.code,
                },
                "vehicle": None if trip.vehicle is None else {
                    "vehicle_id": trip.vehicle.id,
                    "registration_number": trip.vehicle.registration_number,
                    "vehicle_name": trip.vehicle.vehicle_name,
                    "vehicle_model": trip.vehicle.vehicle_model,
                    "vehicle_color": trip.vehicle.color,
                },
                "trip_totals": {
                    "booking_count": 0,
                    "fare_amount": Decimal("0.00"),
                    "commission_amount": Decimal("0.00"),
                    "driver_payout_amount": Decimal("0.00"),
                    "applied_adjustment_amount": Decimal("0.00"),
                    "net_payout_amount": Decimal("0.00"),
                    "paid_out_amount": Decimal("0.00"),
                    "pending_payout_amount": Decimal("0.00"),
                },
                "bookings": [],
            }

        trip_entry = trips_map[trip.id]

        trip_entry["trip_totals"]["booking_count"] += 1
        trip_entry["trip_totals"]["fare_amount"] += fare_amount
        trip_entry["trip_totals"]["commission_amount"] += commission_amount
        trip_entry["trip_totals"]["driver_payout_amount"] += driver_payout_amount
        trip_entry["trip_totals"]["applied_adjustment_amount"] += applied_adjustment_amount
        trip_entry["trip_totals"]["net_payout_amount"] += net_payout_amount
        trip_entry["trip_totals"]["paid_out_amount"] += paid_out_amount
        trip_entry["trip_totals"]["pending_payout_amount"] += pending_amount

        trip_entry["bookings"].append(
            {
                "booking_id": booking.id,
                "passenger_name": _resolve_passenger_name(booking),
                "pickup_stop": None if booking.pickup_stop is None else {
                    "stop_id": booking.pickup_stop.id,
                    "name": booking.pickup_stop.name,
                },
                "dropoff_stop": None if booking.dropoff_stop is None else {
                    "stop_id": booking.dropoff_stop.id,
                    "name": booking.dropoff_stop.name,
                },
                "booking_status": booking.booking_status.value,
                "fare_amount": fare_amount,
                "commission_amount": commission_amount,
                "driver_payout_amount": driver_payout_amount,
                "applied_adjustment_amount": applied_adjustment_amount,
                "net_payout_amount": net_payout_amount,
                "applied_adjustments": [
                    _serialize_applied_adjustment(application)
                    for application in (
                        getattr(booking, "applied_payout_adjustment_applications", []) or []
                    )
                ],
                "payout_status": derived_status,
                "transfer_status": booking.transfer_status.value,
                "transfer_ready_at": booking.transfer_ready_at,
                "transfer_processed_at": booking.transfer_processed_at,
                "completed_at": booking.completed_at,
                "cancelled_at": booking.cancelled_at,
                "transfer": None if transfer is None else {
                    "booking_transfer_id": transfer.id,
                    "amount": _money(transfer.amount),
                    "status": transfer.status.value,
                    "processed_at": transfer.processed_at,
                    "reversed_at": transfer.reversed_at,
                    "failure_reason": transfer.failure_reason,
                    "razorpay_transfer_id": transfer.razorpay_transfer_id,
                },
            }
        )

    items = list(trips_map.values())

    for trip_entry in items:
        trip_entry["trip_totals"]["fare_amount"] = _money(
            trip_entry["trip_totals"]["fare_amount"]
        )
        trip_entry["trip_totals"]["commission_amount"] = _money(
            trip_entry["trip_totals"]["commission_amount"]
        )
        trip_entry["trip_totals"]["driver_payout_amount"] = _money(
            trip_entry["trip_totals"]["driver_payout_amount"]
        )
        trip_entry["trip_totals"]["applied_adjustment_amount"] = _money(
            trip_entry["trip_totals"]["applied_adjustment_amount"]
        )
        trip_entry["trip_totals"]["net_payout_amount"] = _money(
            trip_entry["trip_totals"]["net_payout_amount"]
        )
        trip_entry["trip_totals"]["paid_out_amount"] = _money(
            trip_entry["trip_totals"]["paid_out_amount"]
        )
        trip_entry["trip_totals"]["pending_payout_amount"] = _money(
            trip_entry["trip_totals"]["pending_payout_amount"]
        )

    return {
        "driver_user_id": current_user.id,
        "filters": {
            "payout_status": payout_status,
            "from_month": from_month,
            "from_year": from_year,
            "to_month": to_month,
            "to_year": to_year,
            "trip_month_basis": "scheduled_trip.planned_start_at_ist",
        },
        "driver_payout_profile": _serialize_driver_payout_profile(payout_profile),
        "summary": {
            "trip_count": len(items),
            "booking_count": len(filtered_bookings),
            "total_fare_amount": _money(total_fare_amount),
            "total_commission_amount": _money(total_commission_amount),
            "total_driver_payout_amount": _money(total_driver_payout_amount),
            "total_applied_adjustment_amount": _money(total_applied_adjustment_amount),
            "total_net_payout_amount": _money(total_net_payout_amount),
            "total_paid_out_amount": _money(total_paid_out_amount),
            "total_pending_payout_amount": _money(total_pending_payout_amount),
        },
        "items": items,
    }