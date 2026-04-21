# app/driver/trips/drop_events.py

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.database import get_async_session
from app.db.schema import (
    ScheduledTrip,
    TripScanEvent,
    TripBooking,
    Stop,
    RouteStop,
    User,
    ScanType,
)

router = APIRouter(prefix="/driver/trips", tags=["Driver Trips - Drop Events"])


@router.get("/{scheduled_trip_id}/drop-events")
async def get_drop_events(
    scheduled_trip_id: str,
    db: AsyncSession = Depends(get_async_session),
):
    """
    Driver API:
    See where passengers actually dropped (even if earlier than booked stop)
    """

    # 1. Validate trip exists
    trip_res = await db.execute(
        select(ScheduledTrip).where(ScheduledTrip.id == scheduled_trip_id)
    )
    trip = trip_res.scalar_one_or_none()

    if not trip:
        raise HTTPException(status_code=404, detail="Scheduled trip not found")

    # 2. Fetch all DROP scan events with booking + passenger
    result = await db.execute(
        select(TripScanEvent)
        .where(
            TripScanEvent.scheduled_trip_id == scheduled_trip_id,
            TripScanEvent.scan_type == ScanType.DROP,
        )
        .options(
            selectinload(TripScanEvent.booking).selectinload(TripBooking.passenger),
            selectinload(TripScanEvent.booking).selectinload(TripBooking.route),
        )
    )

    drop_events = result.scalars().all()

    if not drop_events:
        return {
            "message": "No drop events found",
            "data": [],
        }

    # 3. Fetch route stops for sequence comparison
    route_stops_res = await db.execute(
        select(RouteStop)
        .where(RouteStop.route_id == trip.route_id)
        .options(selectinload(RouteStop.stop))
    )

    route_stops = route_stops_res.scalars().all()

    # Create stop_id -> sequence mapping
    stop_sequence_map = {
        rs.stop_id: rs.sequence_no for rs in route_stops
    }

    # 4. Build response
    response = []

    for event in drop_events:
        booking = event.booking

        # Passenger
        passenger = booking.passenger

        # Booked drop stop
        booked_drop_stop_id = booking.dropoff_stop_id

        # Actual drop stop (from scan)
        actual_drop_stop_id = event.matched_stop_id

        # Fetch stop details
        booked_stop = None
        actual_stop = None

        if booked_drop_stop_id:
            res = await db.execute(
                select(Stop).where(Stop.id == booked_drop_stop_id)
            )
            booked_stop = res.scalar_one_or_none()

        if actual_drop_stop_id:
            res = await db.execute(
                select(Stop).where(Stop.id == actual_drop_stop_id)
            )
            actual_stop = res.scalar_one_or_none()

        # Sequence logic
        booked_seq = stop_sequence_map.get(booked_drop_stop_id)
        actual_seq = stop_sequence_map.get(actual_drop_stop_id)

        is_early_drop = False
        is_exact_drop = False

        if booked_seq and actual_seq:
            if actual_seq < booked_seq:
                is_early_drop = True
            elif actual_seq == booked_seq:
                is_exact_drop = True

        response.append({
            "booking_id": booking.id,
            "passenger": {
                "id": passenger.id,
                "email": passenger.email,
            },
            "booked_drop": {
                "stop_id": booked_drop_stop_id,
                "name": booked_stop.name if booked_stop else None,
                "sequence": booked_seq,
            },
            "actual_drop": {
                "stop_id": actual_drop_stop_id,
                "name": actual_stop.name if actual_stop else None,
                "sequence": actual_seq,
            },
            "flags": {
                "early_drop": is_early_drop,
                "exact_drop": is_exact_drop,
            },
            "scan_info": {
                "lat": float(event.scan_lat),
                "lng": float(event.scan_lng),
                "within_radius": event.within_radius,
                "scanned_at": event.created_at,
            },
        })

    return {
        "message": "Drop events fetched successfully",
        "total": len(response),
        "data": response,
    }