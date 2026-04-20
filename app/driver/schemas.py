#app/driver/schemas.py
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from app.db.schema import (
    VehicleVerificationStatus,
    VehicleOwnershipType,
    VehicleInspectionStatus
)


# ---------------------------
# BASE
# ---------------------------
class VehicleBase(BaseModel):
    registration_number: str
    registration_valid_till: datetime | None

    # ✅ basic details
    vehicle_name: str
    vehicle_model: str
    color: str
    seat_count: int
    has_ac: bool

    # ✅ existing files
    rc_file_path: str
    rear_photo_file_path: str

    # ✅ ownership
    ownership_type: VehicleOwnershipType | None
    authentication_file_path: str | None

    # ✅ NEW: vehicle images
    front_photo_file_path: Optional[str] = None
    interior_photo_file_path: Optional[str] = None
    left_side_file_path: Optional[str] = None
    right_side_file_path: Optional[str] = None

    # ✅ NEW: documents
    insurance_document: Optional[str] = None
    pollution_document: Optional[str] = None
    owner_aadhaar_card: Optional[str] = None

    # ✅ NEW: owner info
    owner_name: Optional[str] = None

    # ✅ status
    verification_status: VehicleVerificationStatus
    is_active: bool


# ---------------------------
# OUTPUT
# ---------------------------
class VehicleOut(VehicleBase):
    id: str
    driver_user_id: str

    # verification lifecycle
    verification_requested_at: Optional[datetime] = None
    reviewed_by_admin_id: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None

    # ✅ NEW: inspection
    inspection_status: Optional[VehicleInspectionStatus] = None
    inspection_reason: Optional[str] = None
    inspection_created_at: Optional[datetime] = None
    inspection_reviewed_at: Optional[datetime] = None

    # timestamps
    created_at: datetime
    updated_at: datetime

    model_config = {
        "from_attributes": True
    }