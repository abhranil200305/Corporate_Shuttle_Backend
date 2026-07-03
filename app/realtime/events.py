from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import AsyncSessionLocal
from app.db.schema import (
    BookingStatus,
    RouteStop,
    ScanType,
    ScheduledTrip,
    ScheduledTripStatus,
    TripBooking,
    TripEvent,
    TripScanEvent,
    UserRole,
)
from app.realtime.catalog import supports_audience
from app.realtime.hub import APIRefreshHub, as_utc, utcnow


def get_api_refresh_hub(app: Any) -> APIRefreshHub:
    hub = getattr(app.state, "api_refresh_hub", None)
    if hub is None:
        raise RuntimeError("API refresh WebSocket hub is not initialized.")
    return hub


async def passenger_user_ids_for_trip(
    db: AsyncSession,
    trip_id: str,
) -> list[str]:
    result = await db.execute(
        select(distinct(TripBooking.passenger_user_id)).where(
            TripBooking.scheduled_trip_id == trip_id
        )
    )
    return [str(user_id) for user_id in result.scalars().all() if user_id]


async def driver_user_id_for_trip(
    db: AsyncSession,
    trip_id: str,
) -> str | None:
    result = await db.execute(
        select(ScheduledTrip.driver_user_id).where(ScheduledTrip.id == trip_id)
    )
    driver_user_id = result.scalar_one_or_none()
    return str(driver_user_id) if driver_user_id else None


async def publish_route_event(
    hub: APIRefreshHub,
    *,
    event: str,
    data: dict[str, Any],
) -> None:
    await hub.publish(UserRole.PASSENGER, event=event, data=data)
    await hub.publish(UserRole.DRIVER, event=event, data=data)


async def publish_trip_event(
    hub: APIRefreshHub,
    db: AsyncSession,
    *,
    event: str,
    trip_id: str,
    data: dict[str, Any] | None = None,
    notify_passengers: bool = True,
    broadcast_passengers: bool = False,
    broadcast_catalog: bool = False,
    notify_driver: bool = True,
) -> None:
    event_data = {"trip_id": trip_id, **(data or {})}

    if notify_driver and supports_audience(event, UserRole.DRIVER):
        driver_user_id = await driver_user_id_for_trip(db, trip_id)
        if driver_user_id:
            await hub.publish(
                UserRole.DRIVER,
                event=event,
                data=event_data,
                user_ids=[driver_user_id],
            )

    if notify_passengers and supports_audience(event, UserRole.PASSENGER):
        passenger_ids = None
        if not broadcast_passengers:
            passenger_ids = await passenger_user_ids_for_trip(db, trip_id)
        await hub.publish(
            UserRole.PASSENGER,
            event=event,
            data=event_data,
            user_ids=passenger_ids,
        )

    if broadcast_catalog:
        await hub.publish(
            UserRole.PASSENGER,
            event="trip.catalog_changed",
            data=event_data,
        )


async def publish_booking_change(
    hub: APIRefreshHub,
    db: AsyncSession,
    *,
    trip_id: str,
    passenger_user_id: str,
    reason: str,
    booking_id: str | None = None,
    booking_session_id: str | None = None,
    route_id: str | None = None,
) -> None:
    private_data = {
        "trip_id": trip_id,
        "booking_id": booking_id,
        "booking_session_id": booking_session_id,
        "reason": reason,
    }
    await hub.publish(
        UserRole.PASSENGER,
        event="booking.changed",
        data=private_data,
        user_ids=[passenger_user_id],
    )

    driver_user_id = await driver_user_id_for_trip(db, trip_id)
    if driver_user_id:
        await hub.publish(
            UserRole.DRIVER,
            event="booking.changed",
            data=private_data,
            user_ids=[driver_user_id],
        )

    await hub.publish(
        UserRole.PASSENGER,
        event="trip.seat_availability_changed",
        data={
            "trip_id": trip_id,
            "route_id": route_id,
            "reason": reason,
        },
    )


async def _departure_allowed(
    db: AsyncSession,
    *,
    trip: ScheduledTrip,
    route_stop: RouteStop,
    event: TripEvent,
) -> bool:
    if trip.status != ScheduledTripStatus.IN_PROGRESS:
        return False
    if event.arrival_time is None or event.departure_time is not None:
        return False

    if route_stop.sequence_no > 1:
        previous_route_stop_result = await db.execute(
            select(RouteStop).where(
                RouteStop.route_id == trip.route_id,
                RouteStop.sequence_no == route_stop.sequence_no - 1,
            )
        )
        previous_route_stop = previous_route_stop_result.scalar_one_or_none()
        if previous_route_stop is None:
            return False

        previous_event_result = await db.execute(
            select(TripEvent).where(
                TripEvent.scheduled_trip_id == trip.id,
                TripEvent.stop_id == previous_route_stop.stop_id,
            )
        )
        previous_event = previous_event_result.scalar_one_or_none()
        if previous_event is None or previous_event.departure_time is None:
            return False

        eligible_at = as_utc(previous_event.departure_time) + timedelta(
            minutes=route_stop.assume_time_diff_minutes or 0
        )
        if utcnow() < eligible_at:
            return False

    pending_result = await db.execute(
        select(TripBooking.id).where(
            TripBooking.scheduled_trip_id == trip.id,
            TripBooking.booking_status == BookingStatus.BOARDED,
            TripBooking.dropoff_stop_id == route_stop.stop_id,
        )
    )
    pending_booking_ids = list(pending_result.scalars().all())
    if not pending_booking_ids:
        return True

    dropped_result = await db.execute(
        select(TripScanEvent.booking_id).where(
            TripScanEvent.booking_id.in_(pending_booking_ids),
            TripScanEvent.scan_type == ScanType.DROP,
        )
    )
    dropped_booking_ids = set(dropped_result.scalars().all())
    return all(
        booking_id in dropped_booking_ids for booking_id in pending_booking_ids
    )


async def publish_departure_allowed_if_eligible(
    hub: APIRefreshHub,
    db: AsyncSession,
    *,
    trip_id: str,
    stop_id: str,
) -> bool:
    trip = await db.get(ScheduledTrip, trip_id)
    if trip is None:
        return False

    route_stop_result = await db.execute(
        select(RouteStop).where(
            RouteStop.route_id == trip.route_id,
            RouteStop.stop_id == stop_id,
        )
    )
    route_stop = route_stop_result.scalar_one_or_none()
    if route_stop is None:
        return False

    event_result = await db.execute(
        select(TripEvent).where(
            TripEvent.scheduled_trip_id == trip_id,
            TripEvent.stop_id == stop_id,
        )
    )
    trip_event = event_result.scalar_one_or_none()
    if trip_event is None or not await _departure_allowed(
        db,
        trip=trip,
        route_stop=route_stop,
        event=trip_event,
    ):
        return False

    await hub.publish(
        UserRole.DRIVER,
        event="trip.departure_allowed",
        data={
            "trip_id": trip.id,
            "route_id": trip.route_id,
            "stop_id": stop_id,
            "sequence_no": route_stop.sequence_no,
        },
        user_ids=[trip.driver_user_id],
    )
    return True


async def schedule_start_allowed(
    hub: APIRefreshHub,
    *,
    trip_id: str,
    driver_user_id: str,
    planned_start_at: datetime,
) -> None:
    async def callback() -> None:
        async with AsyncSessionLocal() as db:
            trip = await db.get(ScheduledTrip, trip_id)
            if trip is None or trip.status != ScheduledTripStatus.SCHEDULED:
                return
            now = utcnow()
            start_at = as_utc(trip.planned_start_at)
            grace_ends_at = start_at + timedelta(minutes=15)
            if not start_at <= now <= grace_ends_at:
                return
            await hub.publish(
                UserRole.DRIVER,
                event="trip.start_allowed",
                data={
                    "trip_id": trip.id,
                    "route_id": trip.route_id,
                    "planned_start_at": start_at.isoformat(),
                    "start_window_ends_at": grace_ends_at.isoformat(),
                    "gps_check_still_required": True,
                },
                user_ids=[driver_user_id],
            )

    await hub.schedule_callback(
        f"trip-start-{trip_id}",
        planned_start_at,
        callback,
    )


async def schedule_departure_allowed_check(
    hub: APIRefreshHub,
    *,
    trip_id: str,
    stop_id: str,
    eligible_at: datetime,
) -> None:
    async def callback() -> None:
        async with AsyncSessionLocal() as db:
            await publish_departure_allowed_if_eligible(
                hub,
                db,
                trip_id=trip_id,
                stop_id=stop_id,
            )

    await hub.schedule_callback(
        f"trip-depart-{trip_id}-{stop_id}",
        eligible_at,
        callback,
    )


async def schedule_next_stop_departure_check(
    hub: APIRefreshHub,
    db: AsyncSession,
    *,
    trip: ScheduledTrip,
    departed_route_stop: RouteStop,
    departed_at: datetime,
) -> None:
    next_stop_result = await db.execute(
        select(RouteStop).where(
            RouteStop.route_id == trip.route_id,
            RouteStop.sequence_no == departed_route_stop.sequence_no + 1,
        )
    )
    next_route_stop = next_stop_result.scalar_one_or_none()
    if next_route_stop is None:
        return
    eligible_at = as_utc(departed_at) + timedelta(
        minutes=next_route_stop.assume_time_diff_minutes or 0
    )
    await schedule_departure_allowed_check(
        hub,
        trip_id=trip.id,
        stop_id=next_route_stop.stop_id,
        eligible_at=eligible_at,
    )


async def bootstrap_api_refresh_schedules(
    hub: APIRefreshHub,
    db: AsyncSession,
) -> None:
    future_trips_result = await db.execute(
        select(ScheduledTrip).where(
            ScheduledTrip.status == ScheduledTripStatus.SCHEDULED,
            ScheduledTrip.planned_start_at > utcnow(),
        )
    )
    for trip in future_trips_result.scalars().all():
        await schedule_start_allowed(
            hub,
            trip_id=trip.id,
            driver_user_id=trip.driver_user_id,
            planned_start_at=trip.planned_start_at,
        )

    in_progress_result = await db.execute(
        select(ScheduledTrip).where(
            ScheduledTrip.status == ScheduledTripStatus.IN_PROGRESS
        )
    )
    for trip in in_progress_result.scalars().all():
        route_stops_result = await db.execute(
            select(RouteStop)
            .where(RouteStop.route_id == trip.route_id)
            .order_by(RouteStop.sequence_no)
        )
        route_stops = list(route_stops_result.scalars().all())
        events_result = await db.execute(
            select(TripEvent).where(TripEvent.scheduled_trip_id == trip.id)
        )
        event_by_stop = {
            event.stop_id: event for event in events_result.scalars().all()
        }
        for route_stop in route_stops:
            event = event_by_stop.get(route_stop.stop_id)
            if event is None or event.departure_time is None:
                continue
            await schedule_next_stop_departure_check(
                hub,
                db,
                trip=trip,
                departed_route_stop=route_stop,
                departed_at=event.departure_time,
            )


async def send_current_driver_eligibility(
    hub: APIRefreshHub,
    *,
    driver_user_id: str,
    connection_id: str,
) -> None:
    async with AsyncSessionLocal() as db:
        trip_result = await db.execute(
            select(ScheduledTrip)
            .where(
                ScheduledTrip.driver_user_id == driver_user_id,
                ScheduledTrip.status.in_(
                    [
                        ScheduledTripStatus.SCHEDULED,
                        ScheduledTripStatus.IN_PROGRESS,
                    ]
                ),
            )
            .order_by(ScheduledTrip.planned_start_at)
            .limit(1)
        )
        trip = trip_result.scalar_one_or_none()
        if trip is None:
            return

        now = utcnow()
        if trip.status == ScheduledTripStatus.SCHEDULED:
            start_at = as_utc(trip.planned_start_at)
            grace_ends_at = start_at + timedelta(minutes=15)
            if start_at <= now <= grace_ends_at:
                await hub.send_event_to_connection(
                    UserRole.DRIVER,
                    driver_user_id,
                    connection_id,
                    event="trip.start_allowed",
                    data={
                        "trip_id": trip.id,
                        "route_id": trip.route_id,
                        "planned_start_at": start_at.isoformat(),
                        "start_window_ends_at": grace_ends_at.isoformat(),
                        "gps_check_still_required": True,
                    },
                )
            return

        active_event_result = await db.execute(
            select(TripEvent).where(
                TripEvent.scheduled_trip_id == trip.id,
                TripEvent.arrival_time.is_not(None),
                TripEvent.departure_time.is_(None),
            )
        )
        active_event = active_event_result.scalar_one_or_none()
        if active_event is None:
            return

        route_stop_result = await db.execute(
            select(RouteStop).where(
                RouteStop.route_id == trip.route_id,
                RouteStop.stop_id == active_event.stop_id,
            )
        )
        route_stop = route_stop_result.scalar_one_or_none()
        if route_stop is not None and await _departure_allowed(
            db,
            trip=trip,
            route_stop=route_stop,
            event=active_event,
        ):
            await hub.send_event_to_connection(
                UserRole.DRIVER,
                driver_user_id,
                connection_id,
                event="trip.departure_allowed",
                data={
                    "trip_id": trip.id,
                    "route_id": trip.route_id,
                    "stop_id": route_stop.stop_id,
                    "sequence_no": route_stop.sequence_no,
                },
            )
