from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from app.db.schema import VehicleVerificationStatus

class VehicleBase(BaseModel):
    registration_number: str
    vehicle_name: str
    vehicle_model: str
    color: str
    seat_count: int
    has_ac: bool
    rc_file_path: str
    rear_photo_file_path: str
    verification_status: VehicleVerificationStatus
    is_active: bool

class VehicleOut(VehicleBase):
    id: str
    driver_user_id: str
    verification_requested_at: Optional[datetime] = None
    reviewed_by_admin_id: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None

    class Config:
        orm_mode = True