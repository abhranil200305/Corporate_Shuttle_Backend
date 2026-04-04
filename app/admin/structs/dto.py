from decimal import Decimal
from typing import List, Optional

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


class RouteStopInput(BaseModel):
    stop_id: str
    boarding_allowed: bool = True
    deboarding_allowed: bool = True
    assume_time_diff_minutes: int = 10  # Default estimate


class AddRouteStopsRequest(BaseModel):
    stops: List[RouteStopInput]


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


# This allows the "bulk" selection


class RouteCreate(BaseModel):
    name: str
    code: str


class RouteStopInput(BaseModel):
    stop_id: str
    boarding_allowed: bool = True
    deboarding_allowed: bool = True
    assume_time_diff_minutes: int = 10


class BulkStopAddRequest(BaseModel):
    stops: List[RouteStopInput]


class RatingCreate(BaseModel):
    trip_rating: int = Field(ge=1, le=5)
    driver_rating: int = Field(ge=1, le=5)
    review_text: Optional[str] = None
