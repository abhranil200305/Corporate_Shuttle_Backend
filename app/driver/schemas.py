from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from app.db.schema import VehicleVerificationStatus, VehicleOwnershipType


class VehicleBase(BaseModel):
    registration_number: str
    vehicle_name: str
    registration_valid_till: datetime | None
    vehicle_model: str
    color: str
    seat_count: int
    has_ac: bool
    rc_file_path: str
    rear_photo_file_path: str
    ownership_type: VehicleOwnershipType | None
    authentication_file_path: str | None
    verification_status: VehicleVerificationStatus
    is_active: bool


class VehicleOut(VehicleBase):
    id: str
    driver_user_id: str
    verification_requested_at: Optional[datetime] = None
    reviewed_by_admin_id: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None

    model_config = {
        "from_attributes": True
    }