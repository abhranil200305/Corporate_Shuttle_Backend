from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.admin.endpoints.router import router as admin_router
from app.system_user import ensure_system_fine_register_user
from app.auth import router as auth_router
from app.db.database import AsyncSessionLocal, dispose_database_engine, ping_database
from app.db.schema import User, UserRole
from app.driver import driver_kyc, vehicle
from app.driver.driverprofile import router as driverprofile_router
from app.driver.driverprofileshow import router as driverprofileshow_router
from app.driver.trips import booking_details_service, payout_details, route_trip_details, trip_details
from app.driver.trips.routes import router as driver_routes_router
from app.driver.trips.scheduled_trip import router as scheduled_trip_router
from app.jobs.payment_reconciler import payment_reconcile_loop
from app.jobs.cancelled_booking_refund_reconciler import cancelled_booking_refund_loop
from app.jobs.unstarted_scheduled_trip_canceller import unstarted_trip_cancel_loop
from app.jobs.driver_trip_start_reminder import driver_trip_reminder_loop
from app.jobs.vehicle_registration_expiry_reminder import (
    vehicle_registration_expiry_reminder_loop,
)
from app.jobs.vehicle_inspection_status_reminder import (
    vehicle_inspection_status_reminder_loop,
)
from app.passenger.router import router as passenger_route
from app.driver.support.support import router as support_router
from app.driver.trips import cancel_trip  
from app.driver.scan_events.scan import router as driver_scan_router
from app.driver.trips.current_trip import router as driver_current_trip_router
from app.driver.trips.current_trip import router as current_trip_router
from app.driver.stats.driver_stats import router as driver_stats_router
from app.driver.ratings.driver_ratings import router as driver_ratings_router
from app.notifications import router as notifications_router
from app.notifications.hub import WSHub
from app.driver.trips.route_stops import router as trip_stops_router
from app.driver.trips.emergencytrip_status import router as emergency_status_router
from app.driver.scan_events.otp import router as otp_router
from app.driver.trips.near_stop import router as driver_trip_router
from app.driver.fines.fine import router as fines_router
from app.driver.trips.current_trip_passengers import router as driver_trip_passengers_router
from app.driver.trips.trip_bookings import router as trip_bookings_router
from app.driver.trips.stop_passengers import router as stop_passengers_router



import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    force=True,
)

UPLOADS_DIR = Path.cwd().resolve() / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger(__name__)


def _get_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _should_run_background_jobs() -> bool:
    return _get_bool_env("RUN_BACKGROUND_JOBS", True)


def _create_background_job_tasks(app: FastAPI) -> list[asyncio.Task]:
    ws_hub = app.state.ws_hub

    task_specs: list[tuple[str, object]] = [
        ("payment-reconcile-loop", payment_reconcile_loop),
        ("cancelled-booking-refund-loop", cancelled_booking_refund_loop),
        ("unstarted-trip-cancel-loop", unstarted_trip_cancel_loop),
        ("driver-trip-reminder-loop", driver_trip_reminder_loop),
        (
            "vehicle-registration-expiry-reminder-loop",
            vehicle_registration_expiry_reminder_loop,
        ),
        (
            "vehicle-inspection-status-reminder-loop",
            vehicle_inspection_status_reminder_loop,
        ),
    ]

    tasks: list[asyncio.Task] = []

    for task_name, runner in task_specs:
        task = asyncio.create_task(
            runner(ws_hub),
            name=task_name,
        )
        tasks.append(task)

    return tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.ws_hub = WSHub()
    try:
        await ping_database(retries=3, delay_seconds=1.0)
        print("✅ Database connected successfully")
        await ensure_system_fine_register_user(AsyncSessionLocal)
    except Exception as e:
        print("❌ Database connection failed:", e)
        # fail fast so the app does not boot into a half-broken state
        await dispose_database_engine()
        raise
         
    app.state.background_job_tasks = []

    if _should_run_background_jobs():
        app.state.background_job_tasks = _create_background_job_tasks(app)
        logger.info(
            "Background jobs started count=%s",
            len(app.state.background_job_tasks),
        )
    else:
        logger.info("Background jobs are disabled by RUN_BACKGROUND_JOBS")

    try:
        yield
    finally:
        background_job_tasks: list[asyncio.Task] = list(
            getattr(app.state, "background_job_tasks", [])
        )

        for task in background_job_tasks:
            task.cancel()

        for task in background_job_tasks:
            with suppress(asyncio.CancelledError):
                await task

        ws_hub = getattr(app.state, "ws_hub", None)
        if ws_hub is not None:
            with suppress(Exception):
                await ws_hub.shutdown(code=1001)
        
        await dispose_database_engine()
        print("✅ Database engine disposed")


app = FastAPI(
    title="Kolkata Corporate Shuttle - Driver API",
    lifespan=lifespan,
)

# ---------------------------
# Allow all origins
# ---------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------
# Static files
# ---------------------------
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")

# ---------------------------
# Routers
# ---------------------------
app.include_router(auth_router)
app.include_router(notifications_router)
app.include_router(admin_router)
app.include_router(driverprofile_router)
app.include_router(driver_kyc.router)
app.include_router(driverprofileshow_router)
app.include_router(passenger_route)
app.include_router(vehicle.router)
app.include_router(scheduled_trip_router)
app.include_router(current_trip_router)
app.include_router(driver_routes_router)
app.include_router(support_router)
app.include_router(cancel_trip.router, prefix="/driver", tags=["Driver"])
app.include_router(driver_scan_router)
app.include_router(driver_stats_router)
app.include_router(driver_ratings_router)
app.include_router(driver_trip_passengers_router)
app.include_router(trip_bookings_router)
app.include_router(trip_stops_router)
app.include_router(otp_router)
app.include_router(driver_trip_router)
app.include_router(fines_router)
app.include_router(stop_passengers_router)
app.include_router(
    trip_details.router,
    prefix="/driver/trips",
    tags=["Driver Trips"],
)
app.include_router(
    emergency_status_router,
    prefix="/driver/trips",
    tags=["Driver Trips"]
)
app.include_router(driver_current_trip_router)

app.include_router(
    route_trip_details.router,
    prefix="/driver",
    tags=["Driver Trips"],
)

app.include_router(
    booking_details_service.router,
    prefix="/driver/trips",
    tags=["Driver Trips"],
)

app.include_router(
    payout_details.router,
    prefix="/driver/trips",
    tags=["Driver Trips"],
)

# ---------------------------
# Health / root
# ---------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"message": "Backend Running 🚀"}