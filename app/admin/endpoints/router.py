from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.admin.logic.service import AdminService
from app.auth.dependencies import get_current_admin
from app.db.database import get_db

# Create ONE router for all admin tasks
router = APIRouter(
    prefix="/admin",
    tags=["Admin Management"],
    dependencies=[Depends(get_current_admin)],  # Protects everything in this file
)


# fetch all drivers details
@router.get("/view/all-drivers")
def get_all_drivers_info(db: Session = Depends(get_db)):
    service = AdminService(db)
    drivers = service.fetch_detailed_drivers()

    results = []
    for d in drivers:
        results.append(
            {
                "user_id": d.id,
                "email": d.email,
                "is_active": d.is_active,
                "profile": {
                    "name": d.driver_profile.full_name if d.driver_profile else None,
                    "phone": d.driver_profile.phone if d.driver_profile else None,
                    "verification": d.driver_profile.verification_status
                    if d.driver_profile
                    else "N/A",
                    "docs": {
                        "aadhaar": d.driver_profile.aadhaar_file_path
                        if d.driver_profile
                        else None,
                        "pan": d.driver_profile.pan_file_path
                        if d.driver_profile
                        else None,
                    },
                },
                "bus_details": {
                    "reg_no": d.vehicle.registration_number if d.vehicle else None,
                    "model": d.vehicle.vehicle_model if d.vehicle else None,
                    "capacity": d.vehicle.seat_count if d.vehicle else 0,
                    "ac": d.vehicle.has_ac if d.vehicle else False,
                    "status": d.vehicle.verification_status if d.vehicle else "N/A",
                },
                "payout_info": {
                    "bank": d.payout_details.account_holder_name
                    if d.payout_details
                    else None,
                    "account": d.payout_details.bank_account_number
                    if d.payout_details
                    else None,
                    "ifsc": d.payout_details.ifsc_code if d.payout_details else None,
                    "payout_status": d.payout_details.linked_account_status
                    if d.payout_details
                    else "NOT_READY",
                },
            }
        )
    return results


# fetch all passengers details
@router.get("/view/all-passengers")
def get_all_passengers_info(db: Session = Depends(get_db)):
    service = AdminService(db)
    passengers = service.fetch_detailed_passengers()

    return [
        {
            "user_id": p.id,
            "email": p.email,
            "is_active": p.is_active,
            "joined_at": p.created_at,
            "profile": {
                # Pulling from the PassengerProfile table
                "full_name": p.passenger_profile.full_name
                if p.passenger_profile
                else "Not Set",
                "avatar": p.passenger_profile.profile_picture_path
                if p.passenger_profile
                else None,
            },
            "stats": {
                "total_trips_booked": len(p.passenger_bookings),
                "is_new_user": len(p.passenger_bookings) == 0,
            },
        }
        for p in passengers
    ]


# get driver data using their user_id
@router.get("/driver/{user_id}")
def get_driver_details(user_id: str, db: Session = Depends(get_db)):
    service = AdminService(db)
    d = service.fetch_driver_by_id(user_id)

    if not d:
        raise HTTPException(status_code=404, detail="Driver not found")

    return {
        "user_id": d.id,
        "email": d.email,
        "is_active": d.is_active,
        "profile": {
            "name": d.driver_profile.full_name if d.driver_profile else None,
            "phone": d.driver_profile.phone if d.driver_profile else None,
            "verification": d.driver_profile.verification_status
            if d.driver_profile
            else "N/A",
            "docs": {
                "aadhaar": d.driver_profile.aadhaar_file_path
                if d.driver_profile
                else None,
                "pan": d.driver_profile.pan_file_path if d.driver_profile else None,
            },
        },
        "bus": {
            "reg_no": d.vehicle.registration_number if d.vehicle else None,
            "model": d.vehicle.vehicle_model if d.vehicle else None,
            "capacity": d.vehicle.seat_count if d.vehicle else 0,
            "ac": d.vehicle.has_ac if d.vehicle else False,
        },
        "bank": {
            "account_holder": d.payout_details.account_holder_name
            if d.payout_details
            else None,
            "account_no": d.payout_details.bank_account_number
            if d.payout_details
            else None,
            "ifsc": d.payout_details.ifsc_code if d.payout_details else None,
            "razorpay_status": d.payout_details.linked_account_status
            if d.payout_details
            else "NOT_LINKED",
        },
    }


# get passenger data using their user_id
@router.get("/passenger/{user_id}")
def get_passenger_details(user_id: str, db: Session = Depends(get_db)):
    service = AdminService(db)
    p = service.fetch_passenger_by_id(user_id)

    if not p:
        raise HTTPException(status_code=404, detail="Passenger not found")

    return {
        "user_id": p.id,
        "email": p.email,
        "joined_at": p.created_at,
        "is_active": p.is_active,
        "profile": {
            "full_name": p.passenger_profile.full_name
            if p.passenger_profile
            else "Not Set",
            "avatar": p.passenger_profile.profile_picture_path
            if p.passenger_profile
            else None,
        },
        "booking_summary": {
            "total_count": len(p.passenger_bookings),
            # You can expand this to show actual booking IDs if needed
            "bookings": [
                {"id": b.id, "status": b.booking_status} for b in p.passenger_bookings
            ],
        },
    }
