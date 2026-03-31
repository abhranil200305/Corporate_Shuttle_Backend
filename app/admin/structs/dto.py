from decimal import Decimal
from typing import List

from pydantic import BaseModel


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
