# app/driver/rfid/rf_scan_details.py

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_active_user, get_db
from app.db import schema


router = APIRouter(
    prefix="/driver/rfid",
    tags=["Driver RFID"],
)


@router.get("/scan-details")
async def get_driver_rfid_scan_details(
    scheduled_trip_id: str = Query(...),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: schema.User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:

    # ============================================================
    # role validation
    # ============================================================

    if current_user.role != schema.UserRole.DRIVER:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "driver_access_required",
                "message": "Only drivers can access RFID scan details.",
            },
        )

    # ============================================================
    # verify scheduled trip ownership
    # ============================================================

    trip_stmt = select(schema.ScheduledTrip).where(
        schema.ScheduledTrip.id == scheduled_trip_id,
        schema.ScheduledTrip.driver_user_id == current_user.id,
    )

    trip_result = await db.execute(trip_stmt)
    scheduled_trip = trip_result.scalar_one_or_none()

    if scheduled_trip is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "scheduled_trip_not_found",
                "message": "Scheduled trip not found for this driver.",
            },
        )

    # ============================================================
    # aliases
    # ============================================================

    pickup_stop = schema.Stop.__table__.alias("pickup_stop")
    dropoff_stop = schema.Stop.__table__.alias("dropoff_stop")
    matched_stop = schema.Stop.__table__.alias("matched_stop")

    # ============================================================
    # total count
    # ============================================================

    count_stmt = (
        select(func.count(schema.RFIDScanEvent.id))
        .where(
            schema.RFIDScanEvent.scheduled_trip_id == scheduled_trip_id,
            schema.RFIDScanEvent.driver_user_id == current_user.id,
            schema.RFIDScanEvent.accepted.is_(True),
        )
    )

    count_result = await db.execute(count_stmt)
    total_count = int(count_result.scalar_one() or 0)

    # ============================================================
    # main query
    # ============================================================

    stmt = (
        select(
            schema.RFIDScanEvent.id.label("scan_event_id"),

            schema.RFIDScanEvent.scan_type,
            schema.RFIDScanEvent.created_at,
            schema.RFIDScanEvent.accepted,
            schema.RFIDScanEvent.rejection_reason,

            schema.PassengerProfile.full_name.label(
                "passenger_name"
            ),

            pickup_stop.c.name.label("pickup_stop_name"),
            dropoff_stop.c.name.label("dropoff_stop_name"),
            matched_stop.c.name.label("matched_stop_name"),

            schema.Route.name.label("route_name"),

            schema.RFIDScanEvent.scan_lat,
            schema.RFIDScanEvent.scan_lng,

            schema.RFIDScanEvent.distance_from_stop_meters,
            schema.RFIDScanEvent.within_radius,

            # ====================================================
            # RFID fare details
            # ====================================================

            schema.RFIDTripRide.hold_amount,
            schema.RFIDTripRide.fare_amount,
            schema.RFIDTripRide.fare_reversed_amount,

            schema.RFIDTripRide.commission_amount,

            schema.RFIDTripRide.driver_payout_amount,
            schema.RFIDTripRide.driver_payout_reversed_amount,

            schema.RFIDTripRide.platform_amount,
            schema.RFIDTripRide.platform_amount_reversed,

            schema.RFIDTripRide.status.label("ride_status"),

            schema.RFIDTripRide.transfer_status,
        )

        # --------------------------------------------------------
        # passenger
        # --------------------------------------------------------

        .join(
            schema.PassengerProfile,
            schema.PassengerProfile.user_id
            == schema.RFIDScanEvent.passenger_user_id,
        )

        # --------------------------------------------------------
        # RFID ride
        # --------------------------------------------------------

        .outerjoin(
            schema.RFIDTripRide,
            schema.RFIDTripRide.id
            == schema.RFIDScanEvent.rfid_ride_id,
        )

        # --------------------------------------------------------
        # pickup stop
        # --------------------------------------------------------

        .outerjoin(
            pickup_stop,
            pickup_stop.c.id
            == schema.RFIDTripRide.pickup_stop_id,
        )

        # --------------------------------------------------------
        # dropoff stop
        # --------------------------------------------------------

        .outerjoin(
            dropoff_stop,
            dropoff_stop.c.id
            == schema.RFIDTripRide.dropoff_stop_id,
        )

        # --------------------------------------------------------
        # matched stop from scan event
        # --------------------------------------------------------

        .outerjoin(
            matched_stop,
            matched_stop.c.id
            == schema.RFIDScanEvent.matched_stop_id,
        )

        # --------------------------------------------------------
        # route
        # --------------------------------------------------------

        .outerjoin(
            schema.Route,
            schema.Route.id == schema.RFIDScanEvent.route_id,
        )

        # --------------------------------------------------------
        # filters
        # --------------------------------------------------------

        .where(
            schema.RFIDScanEvent.scheduled_trip_id
            == scheduled_trip_id,

            schema.RFIDScanEvent.driver_user_id
            == current_user.id,

            schema.RFIDScanEvent.accepted.is_(True),
        )

        # --------------------------------------------------------
        # ordering + pagination
        # --------------------------------------------------------

        .order_by(desc(schema.RFIDScanEvent.created_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    result = await db.execute(stmt)
    rows = result.all()

    # ============================================================
    # response build
    # ============================================================

    items: list[dict[str, Any]] = []

    for row in rows:

        board_stop_name = None
        drop_stop_name = None

        # --------------------------------------------------------
        # derive stop names from RFID ride + scan event
        # --------------------------------------------------------

        if row.scan_type == schema.RFIDScanType.BOARD:

            board_stop_name = (
                row.pickup_stop_name
                or row.matched_stop_name
            )

        elif row.scan_type == schema.RFIDScanType.DROP:

            drop_stop_name = (
                row.dropoff_stop_name
                or row.matched_stop_name
            )

        items.append(
            {
                "scan_event_id": row.scan_event_id,

                "scan_type": (
                    row.scan_type.value
                    if row.scan_type
                    else None
                ),

                "passenger_name": row.passenger_name,

                "board_stop_name": board_stop_name,
                "drop_stop_name": drop_stop_name,

                "route_name": row.route_name,

                "accepted": row.accepted,
                "rejection_reason": row.rejection_reason,

                # =================================================
                # RFID amount details
                # =================================================

                "ride_status": (
                    row.ride_status.value
                    if row.ride_status
                    else None
                ),

                "hold_amount": (
                    float(row.hold_amount)
                    if row.hold_amount is not None
                    else 0.0
                ),

                "fare_amount": (
                    float(row.fare_amount)
                    if row.fare_amount is not None
                    else 0.0
                ),

                "fare_reversed_amount": (
                    float(row.fare_reversed_amount)
                    if row.fare_reversed_amount is not None
                    else 0.0
                ),

                "fare_net_amount": (
                    float(
                        (row.fare_amount or 0)
                        - (row.fare_reversed_amount or 0)
                    )
                    if row.fare_amount is not None
                    else 0.0
                ),

                "commission_amount": (
                    float(row.commission_amount)
                    if row.commission_amount is not None
                    else 0.0
                ),

                "driver_payout_amount": (
                    float(row.driver_payout_amount)
                    if row.driver_payout_amount is not None
                    else 0.0
                ),

                "driver_payout_reversed_amount": (
                    float(row.driver_payout_reversed_amount)
                    if row.driver_payout_reversed_amount is not None
                    else 0.0
                ),

                "driver_payout_net_amount": (
                    float(
                        (row.driver_payout_amount or 0)
                        - (row.driver_payout_reversed_amount or 0)
                    )
                    if row.driver_payout_amount is not None
                    else 0.0
                ),

                "platform_amount": (
                    float(row.platform_amount)
                    if row.platform_amount is not None
                    else 0.0
                ),

                "platform_amount_reversed": (
                    float(row.platform_amount_reversed)
                    if row.platform_amount_reversed is not None
                    else 0.0
                ),

                "platform_net_amount": (
                    float(
                        (row.platform_amount or 0)
                        - (row.platform_amount_reversed or 0)
                    )
                    if row.platform_amount is not None
                    else 0.0
                ),

                "transfer_status": (
                    row.transfer_status.value
                    if row.transfer_status
                    else None
                ),

                # =================================================
                # scan location
                # =================================================

                "scan_lat": (
                    float(row.scan_lat)
                    if row.scan_lat is not None
                    else None
                ),

                "scan_lng": (
                    float(row.scan_lng)
                    if row.scan_lng is not None
                    else None
                ),

                "distance_from_stop_meters": (
                    float(row.distance_from_stop_meters)
                    if row.distance_from_stop_meters is not None
                    else None
                ),

                "within_radius": row.within_radius,

                "scanned_at": (
                    row.created_at.isoformat()
                    if isinstance(row.created_at, datetime)
                    else None
                ),
            }
        )

    return {
        "scheduled_trip_id": scheduled_trip_id,
        "page": page,
        "page_size": page_size,
        "count": total_count,
        "items": items,
    }
