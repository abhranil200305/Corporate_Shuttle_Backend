from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.logic.service import AdminService
from app.auth.dependencies import get_current_admin
from app.db.database import get_async_session

# Create ONE router for all admin tasks
router = APIRouter(
    prefix="/admin",
    tags=["Admin Management"],
    dependencies=[Depends(get_current_admin)],  # Protects all routes
)


# -----------------------------
# Admin: All Drivers
# -----------------------------
@router.get("/view/all-drivers")
async def get_all_drivers_info(db: AsyncSession = Depends(get_async_session)):
    service = AdminService(db)
    drivers = await service.fetch_detailed_drivers()

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
                    # "docs": {
                    #     "aadhaar": d.driver_profile.aadhaar_file_path
                    #     if d.driver_profile
                    #     else None,
                    #     "pan": d.driver_profile.pan_file_path
                    #     if d.driver_profile
                    #     else None,
                    # },
                    "documents": {
                        # Added Aadhaar and PAN numbers here
                        "aadhaar_number": d.driver_profile.aadhaar_number
                        if d.driver_profile
                        else None,
                        "pan_number": d.driver_profile.pan_number
                        if d.driver_profile
                        else None,
                        "driving_license_number": d.driver_profile.driving_license_number
                        if d.driver_profile
                        else None,
                        # File paths/URLs
                        "aadhaar_url": d.driver_profile.aadhaar_file_path
                        if d.driver_profile
                        else None,
                        "pan_url": d.driver_profile.pan_file_path
                        if d.driver_profile
                        else None,
                        "dl_url": d.driver_profile.driving_license_file_path
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


# -----------------------------
# Admin: All Passengers
# -----------------------------
@router.get("/view/all-passengers")
async def get_all_passengers_info(db: AsyncSession = Depends(get_async_session)):
    service = AdminService(db)
    passengers = await service.fetch_detailed_passengers()

    return [
        {
            "user_id": p.id,
            "email": p.email,
            "is_active": p.is_active,
            "joined_at": p.created_at,
            "profile": {
                "name": p.passenger_profile.full_name
                if p.passenger_profile
                else "Not Set",
            },
            "total_trips_booked": len(p.passenger_bookings),
        }
        for p in passengers
    ]


# -----------------------------
# Admin: Specific Driver Details
# -----------------------------
@router.get("/driver/{user_id}")
async def get_driver_details(
    user_id: str, db: AsyncSession = Depends(get_async_session)
):
    service = AdminService(db)
    d = await service.fetch_driver_by_id(user_id)

    if not d:
        return {"error": "Driver not found"}

    return {
        "user_id": d.id,
        "email": d.email,
        "is_active": d.is_active,
        "profile": {
            "full_name": d.driver_profile.full_name if d.driver_profile else "Not Set",
            "phone": d.driver_profile.phone if d.driver_profile else None,
            "verification_status": d.driver_profile.verification_status
            if d.driver_profile
            else "draft",
            "documents": {
                # Added Aadhaar and PAN numbers here
                "aadhaar_number": d.driver_profile.aadhaar_number
                if d.driver_profile
                else None,
                "pan_number": d.driver_profile.pan_number if d.driver_profile else None,
                "driving_license_number": d.driver_profile.driving_license_number
                if d.driver_profile
                else None,
                # File paths/URLs
                "aadhaar_url": d.driver_profile.aadhaar_file_path
                if d.driver_profile
                else None,
                "pan_url": d.driver_profile.pan_file_path if d.driver_profile else None,
                "dl_url": d.driver_profile.driving_license_file_path
                if d.driver_profile
                else None,
            },
        },
        "vehicle": {
            "reg_no": d.vehicle.registration_number if d.vehicle else None,
            "model": d.vehicle.vehicle_model if d.vehicle else None,
            "capacity": d.vehicle.seat_count if d.vehicle else 0,
            "has_ac": d.vehicle.has_ac if d.vehicle else False,
            "verification": d.vehicle.verification_status if d.vehicle else "N/A",
        },
        "payout": {
            "account_holder": d.payout_details.account_holder_name
            if d.payout_details
            else None,
            "bank_account": d.payout_details.bank_account_number
            if d.payout_details
            else None,
            "ifsc": d.payout_details.ifsc_code if d.payout_details else None,
            "razorpay_status": d.payout_details.linked_account_status
            if d.payout_details
            else "NOT_CREATED",
        },
    }


# -----------------------------
# Admin: Specific Passenger Details
# -----------------------------
@router.get("/passenger/{user_id}")
async def get_passenger_details(
    user_id: str, db: AsyncSession = Depends(get_async_session)
):
    service = AdminService(db)
    p = await service.fetch_passenger_by_id(user_id)

    if not p:
        return {"error": "Passenger not found"}

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
        "booking_history": {
            "total_count": len(p.passenger_bookings),
            "bookings": [
                {
                    "booking_id": b.id,
                    "status": b.booking_status,
                    "fare": float(b.fare_amount),
                    "created_at": b.created_at,
                }
                for b in p.passenger_bookings
            ],
        },
    }


# check the users who has been inactive from last 3 months
@router.get("/reports/inactive-users")
async def get_inactive_users(
    months: int = 3, db: AsyncSession = Depends(get_async_session)
):
    service = AdminService(db)
    users = await service.fetch_inactive_users(months)

    return [
        {
            "user_id": u.id,
            "email": u.email,
            "phone_no": u.phone_number,  # Added phone number field
            "role": u.role,
            "name": (
                u.passenger_profile.full_name
                if u.passenger_profile
                else u.driver_profile.full_name
                if u.driver_profile
                else "Unknown"
            ),
            "status": "Inactive",
        }
        for u in users
    ]


# active,inactive for the driver and passenger
@router.post("/driver/activate/{user_id}")
async def activate_driver(user_id: str, db: AsyncSession = Depends(get_async_session)):
    service = AdminService(db)
    # Optional: Check if driver exists first
    driver = await service.fetch_driver_by_id(user_id)
    if not driver:
        return {"error": "Driver not found"}, 404

    await service.toggle_driver_status(user_id, active=True)
    return {"message": f"Driver {user_id} has been activated successfully"}


@router.post("/driver/deactivate/{user_id}")
async def deactivate_driver(
    user_id: str, db: AsyncSession = Depends(get_async_session)
):
    service = AdminService(db)
    # Optional: Check if driver exists first
    driver = await service.fetch_driver_by_id(user_id)
    if not driver:
        return {"error": "Driver not found"}, 404

    await service.toggle_driver_status(user_id, active=False)
    return {"message": f"Driver {user_id} has been deactivated successfully"}


# add
