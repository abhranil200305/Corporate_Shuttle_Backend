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

ADMIN_OVERVIEW = RefreshInstruction(
    resources=(
        "dashboard",
        "analytics",
        "drivers",
        "passengers",
        "vehicles",
        "trips",
        "bookings",
        "support_tickets",
        "payout_dashboard",
    ),
    endpoints=(
        "/admin/view/all-drivers",
        "/admin/view/all-passengers",
        "/admin/vehicles/inspection-statuses",
        "/admin/trips/monitor",
        "/admin/tickets",
        "/admin/payouts/dashboard",
        "/admin/analytics/most-booked-routes",
        "/admin/analytics/top-pickup-stops",
    ),
)

ADMIN_USERS = RefreshInstruction(
    resources=("users", "drivers", "passengers", "user_details", "devices"),
    endpoints=(
        "/admin/view/all-drivers",
        "/admin/view/all-passengers",
        "/admin/passengers",
        "/admin/reports/inactive-users",
        "/admin/{user_id}/full_details",
        "/admin/devices",
        "/admin/users/{user_id}/devices",
    ),
)

ADMIN_DRIVERS = RefreshInstruction(
    resources=(
        "drivers",
        "driver_details",
        "vehicles",
        "vehicle_inspections",
        "available_vehicles",
        "driver_ratings",
        "payout_drivers",
    ),
    endpoints=(
        "/admin/view/all-drivers",
        "/admin/driver/{user_id}",
        "/admin/driver/vehicle/{user_id}",
        "/admin/drivers/verified_data",
        "/admin/vehicles/inspection-statuses",
        "/admin/available_vehicles",
        "/admin/driver-ratings",
        "/admin/payouts/drivers",
    ),
)

ADMIN_PASSENGERS = RefreshInstruction(
    resources=(
        "passengers",
        "passenger_details",
        "user_details",
        "passenger_current_trip",
        "passenger_bookings",
        "transactions",
    ),
    endpoints=(
        "/admin/view/all-passengers",
        "/admin/passengers",
        "/admin/passenger/{user_id}",
        "/admin/{user_id}/full_details",
        "/admin/passengers/{user_id}/current-trip",
        "/admin/user/{user_id}/bookings/detailed",
        "/admin/{user_id}/transaction_history",
    ),
)

ADMIN_VEHICLES = RefreshInstruction(
    resources=(
        "vehicles",
        "vehicle_details",
        "vehicle_inspections",
        "available_vehicles",
        "drivers",
    ),
    endpoints=(
        "/admin/vehicles/inspection-statuses",
        "/admin/vehicle/details/{vehicle_id}",
        "/admin/available_vehicles",
        "/admin/view/all-drivers",
    ),
)

ADMIN_ROUTES = RefreshInstruction(
    resources=("stops", "routes", "route_details", "fares", "route_reports"),
    endpoints=(
        "/admin/stops/all",
        "/admin/routes/all",
        "/admin/routes/{route_id}",
        "/admin/routes/{route_id}/fares",
        "/admin/routes/{route_id}/full-report",
    ),
)

ADMIN_TRIPS = RefreshInstruction(
    resources=(
        "dashboard",
        "trips",
        "trip_details",
        "trip_manifest",
        "bookings",
        "incidents",
        "available_vehicles",
    ),
    endpoints=(
        "/admin/trips/monitor",
        "/admin/trips/{trip_id}",
        "/admin/trips/{trip_id}/bookings",
        "/admin/{trip_id}/passengers",
        "/admin/booking/{booking_id}",
        "/admin/trip/{trip_id}/status-only",
        "/admin/incidents",
        "/admin/available_vehicles",
    ),
)

ADMIN_BOOKINGS = RefreshInstruction(
    resources=(
        "dashboard",
        "bookings",
        "booking_sessions",
        "trip_manifest",
        "transactions",
        "passengers",
        "payout_bookings",
        "ratings",
        "analytics",
    ),
    endpoints=(
        "/admin/booking-sessions",
        "/admin/booking-sessions/{booking_session_id}",
        "/admin/trips/{trip_id}/bookings",
        "/admin/bookings/{booking_id}/trip-detail",
        "/admin/booking/{booking_id}",
        "/admin/bookings/{booking_id}/rating",
        "/admin/user/{user_id}/bookings/detailed",
        "/admin/transactions/all",
        "/admin/{user_id}/transaction_history",
        "/admin/analytics/most-booked-routes",
        "/admin/analytics/top-pickup-stops",
        "/admin/payouts/bookings",
    ),
)

ADMIN_RFID = RefreshInstruction(
    resources=(
        "rfid_devices",
        "rfid_cards",
        "rfid_ledger",
        "rfid_recharges",
        "rfid_rides",
        "rfid_payouts",
        "rfid_settings",
    ),
    endpoints=(
        "/admin/rfid/device-vehicle-options",
        "/admin/rfid/card-options",
        "/admin/rfid/devices",
        "/admin/rfid/cards",
        "/admin/rfid/cards/{card_id}",
        "/admin/rfid/cards/{card_id}/ledger",
        "/admin/rfid/cards/{card_id}/recharges",
        "/admin/rfid/rides/payout-ready",
        "/admin/rfid/payout-transfers",
        "/admin/rfid/payout-transfer-reversals",
        "/admin/rfid/payout-transfers/{transfer_id}",
        "/admin/rfid/rides/{rfid_ride_id}/money-detail",
        "/admin/rfid/payout-operations-summary",
        "/admin/rfid/seat-policy",
    ),
)

ADMIN_SUPPORT = RefreshInstruction(
    resources=("support_tickets", "incidents"),
    endpoints=("/admin/tickets", "/admin/incidents"),
)

ADMIN_REVIEWS = RefreshInstruction(
    resources=("reviews", "driver_ratings", "review_stats"),
    endpoints=(
        "/admin/reviews",
        "/admin/reviews/drivers",
        "/admin/reviews/stats",
        "/admin/driver-ratings",
    ),
)

ADMIN_PAYOUTS = RefreshInstruction(
    resources=(
        "payout_settings",
        "payout_drivers",
        "payout_bookings",
        "payout_adjustments",
        "payout_transfers",
        "refunds",
        "payout_dashboard",
        "transactions",
    ),
    endpoints=(
        "/admin/payouts/settings",
        "/admin/payouts/drivers",
        "/admin/payouts/drivers/{driver_user_id}",
        "/admin/payouts/bookings",
        "/admin/payouts/bookings/{booking_id}",
        "/admin/payouts/bookings/{booking_id}/adjustments",
        "/admin/payouts/drivers/{driver_user_id}/open-adjustments",
        "/admin/payouts/transfers",
        "/admin/payouts/transfers/{transfer_id}",
        "/admin/payouts/refunds",
        "/admin/payouts/dashboard",
        "/admin/payouts/drivers/{driver_user_id}/linked-account/provider",
        "/admin/transactions/all",
    ),
)

ADMIN_SETTINGS = RefreshInstruction(
    resources=("device_settings", "commercial_rules", "rfid_settings"),
    endpoints=(
        "/admin/device-settings",
        "/admin/commercial-rules",
        "/admin/commercial-rules/{rule_id}",
        "/admin/rfid/seat-policy",
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
        UserRole.ADMIN: ADMIN_OVERVIEW,
    },
    "route.created": {
        UserRole.PASSENGER: PASSENGER_ROUTE_CATALOG,
        UserRole.DRIVER: DRIVER_ROUTE_CATALOG,
        UserRole.ADMIN: ADMIN_ROUTES,
    },
    "route.updated": {
        UserRole.PASSENGER: PASSENGER_ROUTE_CATALOG,
        UserRole.DRIVER: DRIVER_ROUTE_CATALOG,
        UserRole.ADMIN: ADMIN_ROUTES,
    },
    "trip.created": {
        UserRole.PASSENGER: PASSENGER_TRIP_CATALOG,
        UserRole.DRIVER: DRIVER_CURRENT_TRIP,
        UserRole.ADMIN: ADMIN_TRIPS,
    },
    "trip.catalog_changed": {
        UserRole.PASSENGER: PASSENGER_TRIP_CATALOG,
    },
    "trip.start_allowed": {
        UserRole.DRIVER: DRIVER_CURRENT_TRIP,
        UserRole.ADMIN: ADMIN_TRIPS,
    },
    "trip.started": {
        UserRole.PASSENGER: PASSENGER_TRIP_LIFECYCLE,
        UserRole.DRIVER: DRIVER_CURRENT_TRIP,
        UserRole.ADMIN: ADMIN_TRIPS,
    },
    "trip.stop_arrived": {
        UserRole.PASSENGER: PASSENGER_TRIP_LIFECYCLE,
        UserRole.DRIVER: DRIVER_CURRENT_TRIP,
        UserRole.ADMIN: ADMIN_TRIPS,
    },
    "trip.departure_allowed": {
        UserRole.DRIVER: RefreshInstruction(
            resources=("trip_details", "stop_passengers", "departure_action"),
            endpoints=(
                "/driver/trips/{trip_id}/details",
                "/driver/trips/stop-passengers",
            ),
        ),
        UserRole.ADMIN: ADMIN_TRIPS,
    },
    "trip.stop_departed": {
        UserRole.PASSENGER: PASSENGER_TRIP_LIFECYCLE,
        UserRole.DRIVER: DRIVER_CURRENT_TRIP,
        UserRole.ADMIN: ADMIN_TRIPS,
    },
    "trip.completed": {
        UserRole.PASSENGER: PASSENGER_TRIP_LIFECYCLE,
        UserRole.DRIVER: DRIVER_CURRENT_TRIP,
        UserRole.ADMIN: ADMIN_TRIPS,
    },
    "trip.cancelled": {
        UserRole.PASSENGER: PASSENGER_TRIP_LIFECYCLE,
        UserRole.DRIVER: DRIVER_CURRENT_TRIP,
        UserRole.ADMIN: ADMIN_TRIPS,
    },
    "trip.premature_ended": {
        UserRole.PASSENGER: PASSENGER_TRIP_LIFECYCLE,
        UserRole.DRIVER: DRIVER_CURRENT_TRIP,
        UserRole.ADMIN: ADMIN_TRIPS,
    },
    "booking.changed": {
        UserRole.PASSENGER: PASSENGER_BOOKINGS,
        UserRole.DRIVER: DRIVER_MANIFEST,
        UserRole.ADMIN: ADMIN_BOOKINGS,
    },
    "trip.seat_availability_changed": {
        UserRole.PASSENGER: PASSENGER_TRIP_CATALOG,
    },
    "passenger.scan_completed": {
        UserRole.PASSENGER: PASSENGER_TRIP_LIFECYCLE,
        UserRole.DRIVER: DRIVER_MANIFEST,
        UserRole.ADMIN: ADMIN_BOOKINGS,
    },
    "rfid.scan_completed": {
        UserRole.PASSENGER: PASSENGER_RFID,
        UserRole.DRIVER: DRIVER_RFID,
        UserRole.ADMIN: ADMIN_RFID,
    },
    "trip.rfid_occupancy_changed": {
        UserRole.PASSENGER: PASSENGER_TRIP_CATALOG,
    },
    "admin.users_changed": {UserRole.ADMIN: ADMIN_USERS},
    "admin.drivers_changed": {UserRole.ADMIN: ADMIN_DRIVERS},
    "admin.passengers_changed": {UserRole.ADMIN: ADMIN_PASSENGERS},
    "admin.vehicles_changed": {UserRole.ADMIN: ADMIN_VEHICLES},
    "admin.rfid_changed": {UserRole.ADMIN: ADMIN_RFID},
    "admin.support_changed": {UserRole.ADMIN: ADMIN_SUPPORT},
    "admin.reviews_changed": {UserRole.ADMIN: ADMIN_REVIEWS},
    "admin.payouts_changed": {UserRole.ADMIN: ADMIN_PAYOUTS},
    "admin.settings_changed": {UserRole.ADMIN: ADMIN_SETTINGS},
    "admin.incidents_changed": {
        UserRole.ADMIN: RefreshInstruction(
            resources=("incidents", "trips", "trip_details"),
            endpoints=(
                "/admin/incidents",
                "/admin/trips/monitor",
                "/admin/trips/{trip_id}",
            ),
        )
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
