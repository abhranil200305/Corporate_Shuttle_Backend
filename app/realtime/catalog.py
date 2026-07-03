from __future__ import annotations

from dataclasses import dataclass

from app.db.schema import UserRole


@dataclass(frozen=True)
class RefreshInstruction:
    resources: tuple[str, ...]
    endpoints: tuple[str, ...]


PASSENGER_ROUTE_CATALOG = RefreshInstruction(
    resources=("stops", "routes", "route_trip_options", "scheduled_trips"),
    endpoints=(
        "/passenger/stops",
        "/passenger/routes",
        "/passenger/route-trip-options",
        "/passenger/rfid/route-trip-options",
        "/passenger/scheduled-trips",
    ),
)

DRIVER_ROUTE_CATALOG = RefreshInstruction(
    resources=("routes", "route_details"),
    endpoints=(
        "/driver/routes/",
        "/driver/routes/{route_id}/trips/details",
    ),
)

PASSENGER_TRIP_CATALOG = RefreshInstruction(
    resources=("route_trip_options", "scheduled_trips", "seat_availability"),
    endpoints=(
        "/passenger/route-trip-options",
        "/passenger/rfid/route-trip-options",
        "/passenger/scheduled-trips",
        "/passenger/scheduled-trips/{trip_id}",
        "/passenger/scheduled-trips/{trip_id}/available-seats",
    ),
)

PASSENGER_TRIP_LIFECYCLE = RefreshInstruction(
    resources=(
        "current_bookings",
        "booking_sessions",
        "trip_status",
        "trip_location",
        "scheduled_trips",
    ),
    endpoints=(
        "/passenger/bookings/current",
        "/passenger/booking-sessions/current",
        "/passenger/bookings/{booking_id}/current-status",
        "/passenger/booking-sessions/{booking_session_id}/current-status",
        "/passenger/bookings/{booking_id}/live-location",
        "/passenger/scheduled-trips/{trip_id}",
    ),
)

PASSENGER_BOOKINGS = RefreshInstruction(
    resources=("bookings", "booking_sessions", "transactions"),
    endpoints=(
        "/passenger/bookings",
        "/passenger/bookings/upcoming",
        "/passenger/bookings/current",
        "/passenger/booking-sessions",
        "/passenger/booking-sessions/current",
        "/passenger/history",
        "/passenger/transactions",
    ),
)

PASSENGER_RFID = RefreshInstruction(
    resources=("rfid_summary", "rfid_rides", "rfid_ledger"),
    endpoints=(
        "/passenger/rfid/summary",
        "/passenger/rfid/me",
        "/passenger/rfid/rides",
        "/passenger/rfid/ledger",
    ),
)

DRIVER_CURRENT_TRIP = RefreshInstruction(
    resources=("current_trip", "trip_details", "trip_stops"),
    endpoints=(
        "/driver/trips/current",
        "/driver/trips/{trip_id}/details",
        "/driver/trips/{trip_id}/stops",
        "/driver/routes/{route_id}/trips/details",
    ),
)

DRIVER_MANIFEST = RefreshInstruction(
    resources=(
        "trip_bookings",
        "current_trip_passengers",
        "stop_passengers",
        "drop_events",
    ),
    endpoints=(
        "/driver/trips/{trip_id}/bookings",
        "/driver/trips/current/passengers",
        "/driver/trips/stop-passengers",
        "/driver/trips/{trip_id}/drop-events",
    ),
)

DRIVER_RFID = RefreshInstruction(
    resources=("rfid_scan_details", "trip_details"),
    endpoints=(
        "/driver/rfid/scan-details",
        "/driver/trips/{trip_id}/details",
    ),
)


EVENT_CATALOG: dict[str, dict[UserRole, RefreshInstruction]] = {
    "channel.connected": {
        UserRole.PASSENGER: RefreshInstruction(
            resources=(
                "routes",
                "scheduled_trips",
                "bookings",
                "booking_sessions",
            ),
            endpoints=(
                "/passenger/routes",
                "/passenger/scheduled-trips",
                "/passenger/bookings/current",
                "/passenger/booking-sessions/current",
            ),
        ),
        UserRole.DRIVER: RefreshInstruction(
            resources=("routes", "current_trip", "trip_details"),
            endpoints=(
                "/driver/routes/",
                "/driver/trips/current",
                "/driver/trips/{trip_id}/details",
            ),
        ),
    },
    "route.created": {
        UserRole.PASSENGER: PASSENGER_ROUTE_CATALOG,
        UserRole.DRIVER: DRIVER_ROUTE_CATALOG,
    },
    "route.updated": {
        UserRole.PASSENGER: PASSENGER_ROUTE_CATALOG,
        UserRole.DRIVER: DRIVER_ROUTE_CATALOG,
    },
    "trip.created": {
        UserRole.PASSENGER: PASSENGER_TRIP_CATALOG,
        UserRole.DRIVER: DRIVER_CURRENT_TRIP,
    },
    "trip.catalog_changed": {
        UserRole.PASSENGER: PASSENGER_TRIP_CATALOG,
    },
    "trip.start_allowed": {UserRole.DRIVER: DRIVER_CURRENT_TRIP},
    "trip.started": {
        UserRole.PASSENGER: PASSENGER_TRIP_LIFECYCLE,
        UserRole.DRIVER: DRIVER_CURRENT_TRIP,
    },
    "trip.stop_arrived": {
        UserRole.PASSENGER: PASSENGER_TRIP_LIFECYCLE,
        UserRole.DRIVER: DRIVER_CURRENT_TRIP,
    },
    "trip.departure_allowed": {
        UserRole.DRIVER: RefreshInstruction(
            resources=("trip_details", "stop_passengers", "departure_action"),
            endpoints=(
                "/driver/trips/{trip_id}/details",
                "/driver/trips/stop-passengers",
            ),
        )
    },
    "trip.stop_departed": {
        UserRole.PASSENGER: PASSENGER_TRIP_LIFECYCLE,
        UserRole.DRIVER: DRIVER_CURRENT_TRIP,
    },
    "trip.completed": {
        UserRole.PASSENGER: PASSENGER_TRIP_LIFECYCLE,
        UserRole.DRIVER: DRIVER_CURRENT_TRIP,
    },
    "trip.cancelled": {
        UserRole.PASSENGER: PASSENGER_TRIP_LIFECYCLE,
        UserRole.DRIVER: DRIVER_CURRENT_TRIP,
    },
    "trip.premature_ended": {
        UserRole.PASSENGER: PASSENGER_TRIP_LIFECYCLE,
        UserRole.DRIVER: DRIVER_CURRENT_TRIP,
    },
    "booking.changed": {
        UserRole.PASSENGER: PASSENGER_BOOKINGS,
        UserRole.DRIVER: DRIVER_MANIFEST,
    },
    "trip.seat_availability_changed": {
        UserRole.PASSENGER: PASSENGER_TRIP_CATALOG,
    },
    "passenger.scan_completed": {
        UserRole.PASSENGER: PASSENGER_TRIP_LIFECYCLE,
        UserRole.DRIVER: DRIVER_MANIFEST,
    },
    "rfid.scan_completed": {
        UserRole.PASSENGER: PASSENGER_RFID,
        UserRole.DRIVER: DRIVER_RFID,
    },
    "trip.rfid_occupancy_changed": {
        UserRole.PASSENGER: PASSENGER_TRIP_CATALOG,
    },
}


def get_instruction(event: str, audience: UserRole) -> RefreshInstruction:
    audience_catalog = EVENT_CATALOG.get(event)
    if audience_catalog is None or audience not in audience_catalog:
        raise ValueError(
            f"Refresh event '{event}' is not defined for audience "
            f"'{audience.value}'."
        )
    return audience_catalog[audience]


def supports_audience(event: str, audience: UserRole) -> bool:
    return audience in EVENT_CATALOG.get(event, {})
