from decimal import Decimal
from typing import List

from pydantic import BaseModel, Field

from app.db import schema


class stopCreate(BaseModel):
    name: str
    lat: Decimal
    lng: Decimal
    radius_meters: int = 100

class RouteCreateSchema(BaseModel):
    name: str
    code: str
    stop_ids: list[str]

class RouteStopCreate(BaseModel):
    stop_id: str
    boarding_allowed: bool = True
    deboarding_allowed: bool = True

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

class StopCreate(BaseModel):
    name: str = Field(..., example="Technopolis - Main Gate")
    latitude: float = Field(..., example=22.5815)
    longitude: float = Field(..., example=88.4355)
    radius_meters: int = Field(default=150, description="Geofence radius in meters")

class FareEntry(BaseModel):
    pickup_stop_id: str
    dropoff_stop_id: str
    amount: Decimal = Field(..., ge=0)

class RouteFareCreate(BaseModel):
    route_id: str
    fares: List[FareEntry]

class RouteStatusUpdate(BaseModel):
    is_active: bool