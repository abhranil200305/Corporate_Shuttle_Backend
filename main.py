from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.admin.endpoints.router import router as admin_router
from app.auth import router as auth_router
from app.db.database import dispose_database_engine, ping_database
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





UPLOADS_DIR = Path.cwd().resolve() / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.ws_hub = WSHub()
    try:
        await ping_database(retries=3, delay_seconds=1.0)
        print("✅ Database connected successfully")
    except Exception as e:
        print("❌ Database connection failed:", e)
        # fail fast so the app does not boot into a half-broken state
        await dispose_database_engine()
        raise
         
    reconcile_task = asyncio.create_task(
        payment_reconcile_loop(app.state.ws_hub),
        name="payment-reconcile-loop",
    )
    app.state.payment_reconcile_task = reconcile_task

    cancelled_booking_refund_task = asyncio.create_task(
        cancelled_booking_refund_loop(app.state.ws_hub),
        name="cancelled-booking-refund-loop",
    )
    app.state.cancelled_booking_refund_task = cancelled_booking_refund_task

    unstarted_trip_cancel_task = asyncio.create_task(
        unstarted_trip_cancel_loop(app.state.ws_hub),
        name="unstarted-trip-cancel-loop",
    )
    app.state.unstarted_trip_cancel_task = unstarted_trip_cancel_task

    driver_trip_reminder_task = asyncio.create_task(
        driver_trip_reminder_loop(app.state.ws_hub),
        name="driver-trip-reminder-loop",
    )
    app.state.driver_trip_reminder_task = driver_trip_reminder_task

    try:
        yield
    finally:
        reconcile_task.cancel()
        cancelled_booking_refund_task.cancel()
        unstarted_trip_cancel_task.cancel()
        driver_trip_reminder_task.cancel()

        with suppress(asyncio.CancelledError):
            await reconcile_task

        with suppress(asyncio.CancelledError):
            await cancelled_booking_refund_task

        with suppress(asyncio.CancelledError):
            await unstarted_trip_cancel_task

        with suppress(asyncio.CancelledError):
            await driver_trip_reminder_task

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
app.include_router(trip_stops_router)
app.include_router(
    trip_details.router,
    prefix="/driver/trips",
    tags=["Driver Trips"],
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