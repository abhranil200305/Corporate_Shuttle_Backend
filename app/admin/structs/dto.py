from decimal import Decimal
from typing import List

from pydantic import BaseModel

from app.db import schema


class stopCreate(BaseModel):
    name: str
    lat: Decimal
    lng: Decimal
    radius_meters: int = 100


class RouteStopCreate(BaseModel):
    stop_id: str
    sequence_no: int
    bording_allowed: bool = True
    debording_allowed: bool = True


class RouteCreate(BaseModel):
    name: str
    code: str
    stops: List[RouteStopCreate]

class VerificationUpdate(BaseModel):
    status: schema.DriverVerificationStatus
    rejection_reason: str | None = None

class VehicleVerificationUpdate(BaseModel):
    status: schema.VehicleVerificationStatus
    rejection_reason: str | None = None