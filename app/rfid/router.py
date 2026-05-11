from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_async_session
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
    db: AsyncSession = Depends(get_async_session),
):
    service = RFIDScanService(db)
    response = await service.record_scan(payload)

    await db.commit()

    return response