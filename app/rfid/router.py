from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_async_session
from app.db.schema import UserRole
from app.realtime.events import get_api_refresh_hub
from app.rfid.scan_schemas import RFIDScanRequest, RFIDScanResponse
from app.rfid.scan_service import RFIDScanService

router = APIRouter(
    prefix="/rfid",
    tags=["RFID Scans"],
)


@router.post(
    "/scans",
    response_model=RFIDScanResponse,
)
async def record_rfid_scan(
    payload: RFIDScanRequest,
    request: Request,
    db: AsyncSession = Depends(get_async_session),
):
    service = RFIDScanService(db)
    response = await service.record_scan(payload)

    await db.commit()

    if response.get("accepted"):
        refresh_hub = get_api_refresh_hub(request.app)
        scan_type = response.get("scan_type")
        if hasattr(scan_type, "value"):
            scan_type = scan_type.value
        event_data = {
            "trip_id": response.get("scheduled_trip_id"),
            "route_id": response.get("route_id"),
            "stop_id": response.get("matched_stop_id"),
            "scan_type": scan_type,
        }
        passenger_user_id = response.get("passenger_user_id")
        if passenger_user_id:
            await refresh_hub.publish(
                UserRole.PASSENGER,
                event="rfid.scan_completed",
                data=event_data,
                user_ids=[passenger_user_id],
            )
        driver_user_id = response.get("driver_user_id")
        if driver_user_id:
            await refresh_hub.publish(
                UserRole.DRIVER,
                event="rfid.scan_completed",
                data=event_data,
                user_ids=[driver_user_id],
            )
        if response.get("scheduled_trip_id"):
            await refresh_hub.publish(
                UserRole.PASSENGER,
                event="trip.rfid_occupancy_changed",
                data=event_data,
            )

    return response
