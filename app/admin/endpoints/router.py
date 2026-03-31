from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.admin.logic.service import AdminService
from app.auth.dependencies import get_current_admin
from app.db.database import get_db

router = APIRouter(prefix="/admin", tags=["Admin - Route Management"])
router = APIRouter(
    prefix="/admin/view",
    tags=["Admin - User Management"],
    dependencies=[Depends(get_current_admin)] # Secure lockdown
)

@router.get("/all-drivers")
def get_all_drivers_info(db: Session = Depends(get_db)):
    """Comprehensive list for the Admin to review Bus Owners."""
    service = AdminService(db)
    drivers = service.fetch_detailed_drivers()
    
    results = []
    for d in drivers:
        results.append({
            "user_id": d.id,
            "email": d.email,
            "is_active": d.is_active,
            "profile": {
                "name": d.driver_profile.full_name if d.driver_profile else None,
                "phone": d.driver_profile.phone if d.driver_profile else None,
                "verification": d.driver_profile.verification_status if d.driver_profile else "N/A",
                "docs": {
                    "aadhaar": d.driver_profile.aadhaar_file_path if d.driver_profile else None,
                    "pan": d.driver_profile.pan_file_path if d.driver_profile else None
                }
            },
            "bus_details": {
                "reg_no": d.vehicle.registration_number if d.vehicle else None,
                "model": d.vehicle.vehicle_model if d.vehicle else None,
                "capacity": d.vehicle.seat_count if d.vehicle else 0,
                "ac": d.vehicle.has_ac if d.vehicle else False,
                "status": d.vehicle.verification_status if d.vehicle else "N/A"
            },
            "payout_info": {
                "bank": d.payout_details.account_holder_name if d.payout_details else None,
                "account": d.payout_details.bank_account_number if d.payout_details else None,
                "ifsc": d.payout_details.ifsc_code if d.payout_details else None,
                "payout_status": d.payout_details.linked_account_status if d.payout_details else "NOT_READY"
            }
        })
    return results

@router.get("/all-passengers")
def get_all_passengers_info(db: Session = Depends(get_db)):
    """List of corporate employees registered on the platform."""
    service = AdminService(db)
    passengers = service.fetch_detailed_passengers()
    
    return [
        {
            "user_id": p.id,
            "email": p.email,
            "joined_at": p.created_at,
            "is_active": p.is_active,
            "total_trips_booked": len(p.passenger_bookings)
        } for p in passengers
    ]
