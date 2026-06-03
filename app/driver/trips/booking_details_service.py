# app/driver/trips/booking_details.py

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.dependencies import get_current_user
from app.db.database import get_async_session
from app.db.schema import (
    BookingPaymentStatus,
    BookingStatus,
    Route,
    RouteStop,
    ScheduledTrip,
    TripBooking,
    User,
)

router = APIRouter()


def _money(value: Decimal | None) -> Decimal:
    return (value or Decimal("0.00")).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def _build_eta_by_stop_id(trip: ScheduledTrip) -> dict[str, object]:
    """
    Rule:
    - first stop ETA = planned_start_at
    - next stop ETA = previous ETA + assume_time_diff_minutes
    """
    ordered_route_stops = sorted(
        trip.route.route_stops,
        key=lambda item: item.sequence_no,
    )

    eta_by_stop_id: dict[str, object] = {}
    current_eta = trip.planned_start_at

    for index, route_stop in enumerate(ordered_route_stops):
        if index == 0:
            eta_by_stop_id[route_stop.stop_id] = current_eta
            continue

        diff_minutes = max(
            0,
            int(route_stop.assume_time_diff_minutes or 0),
        )

        current_eta = current_eta + timedelta(
            minutes=diff_minutes
        )

        eta_by_stop_id[route_stop.stop_id] = current_eta

    return eta_by_stop_id


def _resolve_driver_traveller_display(
    booking: TripBooking,
) -> tuple[str, str | None]:
    owner_name = None

    if (
        booking.passenger
        and booking.passenger.passenger_profile
    ):
        owner_name = (
            booking.passenger.passenger_profile.full_name
            or ""
        ).strip() or None

    traveller_name = (
        booking.traveller_name_snapshot
        or owner_name
        or (
            booking.passenger.email
            if booking.passenger
            else None
        )
        or "Passenger"
    )

    return traveller_name, owner_name


def _resolve_fare_paid(
    booking: TripBooking,
) -> Decimal:
    paid_payments = [
        payment
        for payment in booking.payments
        if payment.status == BookingPaymentStatus.PAID
    ]

    if not paid_payments:
        return Decimal("0.00")

    paid_payments.sort(
        key=lambda payment: (
            payment.updated_at,
            payment.created_at,
        ),
        reverse=True,
    )

    return _money(
        paid_payments[0].amount
    )


@router.get("/{trip_id}/booking-details")
async def get_booking_details(
    trip_id: str,
    db: AsyncSession = Depends(
        get_async_session
    ),
    current_user: User = Depends(
        get_current_user
    ),
):
    # 1️⃣ Fetch trip with route + stops + bookings + passenger + payments
    result = await db.execute(
        select(ScheduledTrip)
        .where(
            ScheduledTrip.id == trip_id
        )
        .options(
            selectinload(
                ScheduledTrip.route
            )
            .selectinload(
                Route.route_stops
            )
            .selectinload(
                RouteStop.stop
            ),

            selectinload(
                ScheduledTrip.driver
            ),

            selectinload(
                ScheduledTrip.vehicle
            ),

            selectinload(
                ScheduledTrip.bookings
            )
            .selectinload(
                TripBooking.pickup_stop
            ),

            selectinload(
                ScheduledTrip.bookings
            )
            .selectinload(
                TripBooking.dropoff_stop
            ),

            selectinload(
                ScheduledTrip.bookings
            )
            .selectinload(
                TripBooking.payments
            ),

            selectinload(
                ScheduledTrip.bookings
            )
            .selectinload(
                TripBooking.passenger
            )
            .selectinload(
                User.passenger_profile
            ),
        )
    )

    trip = result.scalar_one_or_none()

    if not trip:
        raise HTTPException(
            status_code=404,
            detail="Trip not found",
        )

    # 🔒 Driver access check
    if trip.driver_user_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Not allowed",
        )

    # 2️⃣ Build ETA map from route stop diffs
    eta_by_stop_id = _build_eta_by_stop_id(
        trip
    )

    # 3️⃣ Keep only driver-useful bookings
    # 3️⃣ Keep driver-visible bookings

    trip_finished = (
        getattr(trip.status, "value", trip.status)
        in (
            "completed",
            "premature_end",
        )
    )

    if trip_finished:
        # After trip ends → show history
        visible_bookings = [
            booking
            for booking in trip.bookings
            if booking.booking_status
            != BookingStatus.PENDING_PAYMENT
        ]
    else:
        # Active trip → only operational bookings
        visible_bookings = [
            booking
            for booking in trip.bookings
            if booking.booking_status in (
                BookingStatus.BOOKED,
                BookingStatus.BOARDED,
                BookingStatus.COMPLETED,
            )
        ]

    visible_bookings.sort(
        key=lambda booking: (
            booking.pickup_sequence_no_snapshot,
            booking.dropoff_sequence_no_snapshot,
            booking.created_at,
        )
    )

    # 4️⃣ Build booking list
    bookings_data = []

    total_fare = Decimal("0.00")
    total_fare_paid = Decimal("0.00")

    for booking in visible_bookings:
        fare = _money(
            booking.fare_amount
        )

        fare_paid = _resolve_fare_paid(
            booking
        )

        traveller_name, owner_name = (
            _resolve_driver_traveller_display(
                booking
            )
        )

        total_fare += fare
        total_fare_paid += fare_paid

        bookings_data.append(
            {
                # Existing fields
                "booking_id": booking.id,

                "seat_number": (
                    booking.seat_number
                ),

                # Existing field kept
                "name": traveller_name,

                "take_in": (
                    booking.pickup_stop.name
                    if booking.pickup_stop
                    else None
                ),

                "drop_off": (
                    booking.dropoff_stop.name
                    if booking.dropoff_stop
                    else None
                ),

                "estimated_pickup_time": (
                    eta_by_stop_id.get(
                        booking.pickup_stop_id
                    )
                ),

                "estimated_drop_off_time": (
                    eta_by_stop_id.get(
                        booking.dropoff_stop_id
                    )
                ),

                "fare": fare,

                "fare_paid": fare_paid,

                "booking_status": (
                    booking.booking_status
                ),

                "boarded_at": (
                    booking.boarded_at
                ),

                "completed_at": (
                    booking.completed_at
                ),


                # ------------------------------------------------
                # NEW MULTI-SEAT / TRAVELLER SNAPSHOT FIELDS
                # ------------------------------------------------
                "booking_session_id": (
                    booking.booking_session_id
                ),

                "passenger_id": (
                    booking.passenger_user_id
                ),

                "account_owner_user_id": (
                    booking.passenger_user_id
                ),

                "booked_by_user_id": (
                    booking.booked_by_user_id
                ),

                "passenger_name": (
                    traveller_name
                ),

                "traveller_name": (
                    traveller_name
                ),

                "traveller_phone": (
                    booking.traveller_phone_snapshot
                ),

                "traveller_email": (
                    booking.traveller_email_snapshot
                ),

                "traveller_relationship_label": (
                    booking.traveller_relationship_label_snapshot
                ),

                "account_owner_name": (
                    owner_name
                ),
            }
        )

    # 5️⃣ Final response
    return {
        "trip_id": trip.id,

        "status": trip.status,

        "planned_start": (
            trip.planned_start_at
        ),

        "planned_end": (
            trip.planned_end_at
        ),

        "actual_start": (
            trip.actual_start_at
        ),

        "actual_end": (
            trip.actual_end_at
        ),

        "driver": {
            "driver_id": trip.driver.id,
            "email": trip.driver.email,
        },

        "vehicle": {
            "vehicle_id": (
                trip.vehicle.id
            ),
            "name": (
                trip.vehicle.vehicle_name
            ),
            "model": (
                trip.vehicle.vehicle_model
            ),
            "registration_number": (
                trip.vehicle.registration_number
            ),
        },

        "route": {
            "route_id": trip.route.id,
            "name": trip.route.name,
        },

        "booking_count": len(
            bookings_data
        ),

        "bookings": bookings_data,

        "total_fare": _money(
            total_fare
        ),

        "total_fare_paid": _money(
            total_fare_paid
        ),
    }