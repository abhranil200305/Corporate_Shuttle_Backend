# app/driver/socket/background_tasks.py

import asyncio
import logging
from datetime import datetime, timezone
from typing import Set

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.schema import ScheduledTrip, ScheduledTripStatus
from app.db.database import async_session  # proper async session factory
from app.driver.socket.driver_events import notify_trip_started

# Configure logger
logger = logging.getLogger("driver_socket")
logger.setLevel(logging.INFO)

# In-memory cache to avoid duplicate notifications: trip_id -> notified
_notified_trips: Set[str] = set()


async def trip_start_watcher(poll_interval: int = 5):
    """
    Background task that polls for trips with status IN_PROGRESS
    and notifies passengers if not already notified.
    Runs continuously every `poll_interval` seconds.
    """
    global _notified_trips
    logger.info("[Background Task] trip_start_watcher started")

    while True:
        try:
            async with async_session() as session:  # proper async session
                async with session.begin():  # ensure transactions
                    result = await session.execute(
                        select(ScheduledTrip).where(
                            ScheduledTrip.status == ScheduledTripStatus.IN_PROGRESS
                        )
                    )
                    trips = result.scalars().all()

                    for trip in trips:
                        if trip.id not in _notified_trips:
                            # Notify passengers via socket events
                            await notify_trip_started(session, trip.id)
                            _notified_trips.add(trip.id)
                            logger.info(f"[Background Task] Notified passengers for trip {trip.id}")

            await asyncio.sleep(poll_interval)

        except Exception as e:
            logger.exception(f"[Background Task] Error in trip_start_watcher: {e}")
            await asyncio.sleep(poll_interval)