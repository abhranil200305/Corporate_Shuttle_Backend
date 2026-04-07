from typing import Optional

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    HTTPException,
    Path,
    Query,
    UploadFile,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.admin.logic.service import AdminService
from app.admin.structs.dto import (
    BulkPayoutTriggerRequest,
    BulkStopAddRequest,
    DriverLinkedAccountUpdate,
    DriverPayoutDetailsUpsert,
    DriverPayoutEligibilityUpdate,
    PayoutSettingsUpdate,
    RouteCreate,
    RouteFareCreate,
    RouteStatusUpdate,
    StopCreate,
    TriggerBookingPayoutRequest,
    TriggerDriverMonthlyPayoutRequest,
    VehicleVerificationUpdate,
    VerificationUpdate,
)
from app.auth.dependencies import get_current_admin, get_current_user
from app.db import schema
from app.db.database import get_async_session
from app.payments.service import RoutePayoutService

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
        # Check if profiles exist to avoid NoneType errors
        p = d.driver_profile
        v = d.vehicle

        results.append(
            {
                "user_id": d.id,
                "email": d.email,
                "is_active": d.is_active,
                "profile": {
                    "name": p.full_name if p else None,
                    "phone": p.phone if p else None,
                    "verification": p.verification_status if p else "N/A",
                    "documents": {
                        "aadhaar_number": p.aadhaar_number if p else None,
                        "pan_number": p.pan_number if p else None,
                        "driving_license_number": p.driving_license_number
                        if p
                        else None,
                        "aadhaar_url": p.aadhaar_file_path if p else None,
                        "pan_url": p.pan_file_path if p else None,
                        "dl_url": p.driving_license_file_path if p else None,
                    },
                },
                "bus_details": {
                    "reg_no": v.registration_number if v else None,
                    "model": v.vehicle_model if v else None,
                    "capacity": v.seat_count if v else 0,
                    "ac": v.has_ac if v else False,
                    "status": v.verification_status if v else "Pending",
                    # FIXED: Checking v.rc_file_path instead of d.rc_file_path
                    "rc_file_path": v.rc_file_path if (v and v.rc_file_path) else "NA",
                    "rear_photo_file_path": v.rear_photo_file_path
                    if (v and v.rear_photo_file_path)
                    else "NA",
                },
                "account_info": {
                    "account_number": p.bank_account_number if p else None,
                    "IFSC_code": p.ifsc_code if p else None,
                    "passbook_url": p.passbook_file_path if p else None,
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
    v = d.vehicle
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
            "rc_file_path": v.rc_file_path if (v and v.rc_file_path) else "NA",
            "rear_photo_file_path": v.rear_photo_file_path
            if (v and v.rear_photo_file_path)
            else "NA",
        },
        "account_info": {
            "account_number": d.driver_profile.bank_account_number
            if d.driver_profile
            else None,
            "IFSC_code": d.driver_profile.ifsc_code if d.driver_profile else None,
            "passbook_url": d.driver_profile.passbook_file_path
            if d.driver_profile
            else None,
        },
    }


@router.get("/driver/vehicle/{user_id}")
async def get_driver_details_vechicals(
    user_id: str, db: AsyncSession = Depends(get_async_session)
):
    service = AdminService(db)
    d = await service.fetch_driver_by_id(user_id)

    if not d:
        return {"error": "Driver not found"}
    v = d.vehicle
    return {
        "user_id": d.id,
        "email": d.email,
        "is_active": d.is_active,
        "vehicle": {
            "reg_no": d.vehicle.registration_number if d.vehicle else None,
            "model": d.vehicle.vehicle_model if d.vehicle else None,
            "capacity": d.vehicle.seat_count if d.vehicle else 0,
            "has_ac": d.vehicle.has_ac if d.vehicle else False,
            "verification": d.vehicle.verification_status if d.vehicle else "N/A",
            "rc_file_path": v.rc_file_path if (v and v.rc_file_path) else "NA",
            "rear_photo_file_path": v.rear_photo_file_path
            if (v and v.rear_photo_file_path)
            else "NA",
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
            # "bookings": [
            #     {
            #         "booking_id": b.id,
            #         "status": b.booking_status,
            #         "fare": float(b.fare_amount),
            #         "created_at": b.created_at,
            #         "pickup_stop": {
            #             "id": b.pickup_stop.id,
            #             "name": b.pickup_stop.name,  # Assuming stop_name is the field
            #             "sequence": b.pickup_sequence_no_snapshot,
            #         },
            #         "dropoff_stop": {
            #             "id": b.pickup_stop.id,
            #             "name": b.pickup_stop.name,
            #             "sequence": b.dropoff_sequence_no_snapshot,
            #         },
            #     }
            #     for b in p.passenger_bookings
            # ],
            "bookings": [
                {
                    "booking_id": b.id,
                    "status": b.booking_status,
                    "fare": float(b.fare_amount),
                    "created_at": b.created_at,
                    "pickup_stop": {
                        "id": b.pickup_stop.id,
                        "name": b.pickup_stop.name,
                        "sequence": b.pickup_sequence_no_snapshot,
                    },
                    "dropoff_stop": {
                        "id": b.dropoff_stop.id,  # Ensure this says dropoff_stop
                        "name": b.dropoff_stop.name,  # Ensure this says dropoff_stop
                        "sequence": b.dropoff_sequence_no_snapshot,
                    },
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


# ----------------- driver profiles verification ---------------------------
@router.post("/driver/verify/{user_id}")
async def verify_driver(
    user_id: str,
    data: VerificationUpdate,
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)

    # 1. Check if the driver profile exists
    driver = await service.fetch_driver_by_id(user_id)
    if not driver or not driver.driver_profile:
        return {"error": "Driver profile not found"}, 404

    # 2. Update the status
    await service.update_driver_verification(
        user_id=user_id, status=data.status, rejection_reason=data.rejection_reason
    )

    return {
        "message": f"Driver verification status updated to {data.status}",
        "user_id": user_id,
    }


# ----------------- driver vehical verification ---------------------------
@router.post("/vehicle/verify/{user_id}")
async def verify_vehicle(
    user_id: str,
    data: VehicleVerificationUpdate,
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)

    # 1. Check if the vehicle exists for this user
    driver = await service.fetch_driver_by_id(user_id)  # Reuse your fetch logic
    if not driver or not driver.vehicle:
        return {"error": "Vehicle record not found for this user"}, 404

    # 2. Update the vehicle status
    await service.update_vehicle_verification(
        user_id=user_id, status=data.status, rejection_reason=data.rejection_reason
    )

    return {
        "message": f"Vehicle verification status updated to {data.status}",
        "user_id": user_id,
        "registration_number": driver.vehicle.registration_number,
    }


# ------------------- add stops and routes -----------------------------


@router.post("/stops/bulk-upload")
async def bulk_upload_stops(
    file: UploadFile = File(...), db: AsyncSession = Depends(get_async_session)
):
    content = await file.read()
    service = AdminService(db)
    total = await service.upsert_stops_from_jsonl(content.decode("utf-8"))
    return {"status": "success", "imported": total}


@router.get("/stops/all")
async def get_all_stops(db: AsyncSession = Depends(get_async_session)):
    # Fetch all active stops from the library
    result = await db.execute(
        select(schema.Stop).where(schema.Stop.is_active).order_by(schema.Stop.name)
    )
    stops = result.scalars().all()

    return [
        {
            "stop_id": s.id,
            "name": s.name,
            "latitude": float(s.lat),
            "longitude": float(s.lng),
        }
        for s in stops
    ]


@router.post("/stops/add-single")
async def add_single_stop(
    data: StopCreate, db: AsyncSession = Depends(get_async_session)
):
    # 1. Check if the stop already exists by name
    stmt = select(schema.Stop).where(schema.Stop.name == data.name)
    result = await db.execute(stmt)
    existing_stop = result.scalar_one_or_none()

    if existing_stop:
        # Update existing stop
        existing_stop.lat = data.latitude
        existing_stop.lng = data.longitude
        existing_stop.radius_meters = data.radius_meters
        message = f"Stop '{data.name}' updated successfully."
    else:
        # Create new stop
        new_stop = schema.Stop(
            name=data.name,
            lat=data.latitude,
            lng=data.longitude,
            radius_meters=data.radius_meters,
        )
        db.add(new_stop)
        message = f"Stop '{data.name}' created successfully."

    await db.commit()

    return {
        "status": "success",
        "message": message,
        "data": {"name": data.name, "coords": [data.latitude, data.longitude]},
    }


@router.delete("/stops/{stop_id}")
async def delete_stop(stop_id: str, db: AsyncSession = Depends(get_async_session)):
    # 1. Search for the stop
    stmt = select(schema.Stop).where(schema.Stop.id == stop_id)
    result = await db.execute(stmt)
    stop = result.scalar_one_or_none()

    if not stop:
        raise HTTPException(status_code=404, detail="Stop not found")

    # 2. Check if it's linked to any routes (Optional but Recommended)
    # This prevents breaking an active bus route accidentally
    route_check = await db.execute(
        select(schema.RouteStop).where(schema.RouteStop.stop_id == stop_id)
    )
    if route_check.scalars().first():
        raise HTTPException(
            status_code=400,
            detail="Cannot delete: This stop is part of an active route. Remove it from the route first.",
        )

    # 3. Delete the stop
    await db.delete(stop)
    await db.commit()

    return {
        "status": "success",
        "message": f"Stop '{stop.name}' has been deleted.",
        "deleted_id": stop_id,
    }


# @router.post("/routes/create")
# async def create_route(
#     data: RouteCreate, db: AsyncSession = Depends(get_async_session)
# ):
#     # 1. Create the Route Entry
#     new_route = schema.Route(name=data.name.strip(), code=data.code.strip())
#     db.add(new_route)
#     await db.flush()

#     # 2. Map the sequence in RouteStop
#     route_stops = []
#     for index, stop_data in enumerate(data.stops):
#         # Logic: If it's the first stop, force 0. Otherwise, use provided time.
#         time_diff = 0 if index == 0 else stop_data.assume_time_diff_minutes

#         rs = schema.RouteStop(
#             route_id=new_route.id,
#             stop_id=stop_data.stop_id,
#             sequence_no=index + 1,
#             boarding_allowed=stop_data.boarding_allowed,
#             deboarding_allowed=stop_data.deboarding_allowed,
#             assume_time_diff_minutes=time_diff,
#         )
#         route_stops.append(rs)

#     db.add_all(route_stops)
#     await db.commit()

#     return {
#         "status": "success",
#         "route_id": new_route.id,
#         "stops_count": len(data.stops),
#     }


# @router.post("/routes/create")
# async def create_route(
#     data: RouteCreate, db: AsyncSession = Depends(get_async_session)
# ):
#     # 1. PRE-CHECK: Look for duplicate Name or Code
#     # We use 'or_' because both must be unique
#     from sqlalchemy import or_

#     stmt = select(schema.Route).where(
#         or_(
#             schema.Route.name == data.name.strip(),
#             schema.Route.code == data.code.strip(),
#         )
#     )
#     result = await db.execute(stmt)
#     existing_route = result.scalar_one_or_none()

#     if existing_route:
#         # Determine which one caused the conflict for a better error message
#         conflict_field = "Name" if existing_route.name == data.name.strip() else "Code"
#         raise HTTPException(
#             status_code=400,
#             detail={
#                 "error": "duplicate_route",
#                 "message": f"A route with this {conflict_field} already exists. Please use a unique value.",
#             },
#         )

#     # 2. Create the Route Entry
#     new_route = schema.Route(name=data.name.strip(), code=data.code.strip())
#     db.add(new_route)

#     # We flush here to get the new_route.id for the RouteStops
#     await db.flush()

#     # 3. Map the sequence in RouteStop
#     route_stops = []
#     for index, stop_data in enumerate(data.stops):
#         # Logic: If it's the first stop, force 0. Otherwise, use provided time.
#         time_diff = 0 if index == 0 else stop_data.assume_time_diff_minutes

#         rs = schema.RouteStop(
#             route_id=new_route.id,
#             stop_id=stop_data.stop_id,
#             sequence_no=index + 1,
#             boarding_allowed=stop_data.boarding_allowed,
#             deboarding_allowed=stop_data.deboarding_allowed,
#             assume_time_diff_minutes=time_diff,
#         )
#         route_stops.append(rs)

#     db.add_all(route_stops)

#     try:
#         await db.commit()
#     except Exception:
#         await db.rollback()
#         raise HTTPException(status_code=500, detail="Failed to save route stops.")

#     return {
#         "status": "success",
#         "route_id": new_route.id,
#         "stops_count": len(data.stops),
#     }


# @router.post("/routes/create")
# async def create_route(
#     data: RouteCreate, db: AsyncSession = Depends(get_async_session)
# ):
#     # 1. Duplicate Check (Name/Code)
#     from sqlalchemy import or_

#     stmt = select(schema.Route).where(
#         or_(
#             schema.Route.name == data.name.strip(),
#             schema.Route.code == data.code.strip(),
#         )
#     )
#     result = await db.execute(stmt)
#     if result.scalar_one_or_none():
#         raise HTTPException(
#             status_code=400, detail="Route Name or Code already exists."
#         )

#     # 2. Basic Validation: A route needs at least 2 stops
#     if len(data.stops) < 2:
#         raise HTTPException(
#             status_code=400, detail="A route must have at least 2 stops."
#         )

#     # 3. Create the Route Entry
#     new_route = schema.Route(name=data.name.strip(), code=data.code.strip())
#     db.add(new_route)
#     await db.flush()  # Get the new_route.id

#     # 4. Map the Bulk List to the Sequence
#     route_stops = []

#     # Python's enumerate gives us the order perfectly
#     for index, stop_data in enumerate(data.stops):
#         # The first stop (index 0) is the START, so time_diff is always 0
#         actual_time_diff = 0 if index == 0 else stop_data.assume_time_diff_minutes

#         rs = schema.RouteStop(
#             route_id=new_route.id,
#             stop_id=stop_data.stop_id,
#             sequence_no=index + 1,  # 1, 2, 3...
#             boarding_allowed=stop_data.boarding_allowed,
#             deboarding_allowed=stop_data.deboarding_allowed,
#             assume_time_diff_minutes=actual_time_diff,
#         )
#         route_stops.append(rs)

#     db.add_all(route_stops)

#     try:
#         await db.commit()
#     except Exception:
#         await db.rollback()
#         raise HTTPException(
#             status_code=500, detail="Database error while saving stops."
#         )

#     return {
#         "status": "success",
#         "route_id": new_route.id,
#         "total_stops_added": len(route_stops),
#     }


# ----------------- specific routes create -------------------------


@router.post("/routes/create")
async def create_route_identity(
    data: RouteCreate, db: AsyncSession = Depends(get_async_session)
):
    try:
        new_route = schema.Route(name=data.name.strip(), code=data.code.strip().upper())
        db.add(new_route)
        await db.commit()
        await db.refresh(new_route)

        return {
            "status": "success",
            "message": "Route identity created. Now add stops.",
            "data": {
                "route_id": new_route.id,
                "name": new_route.name,
                "code": new_route.code,
            },
        }

    except IntegrityError as e:
        await db.rollback()
        # This catches the 500 and explains WHY (Duplicate Name/Code)
        error_info = str(e.orig)
        detail_msg = "Route Name or Code already exists."
        if "code" in error_info.lower():
            detail_msg = f"The route code '{data.code}' is already in use."
        elif "name" in error_info.lower():
            detail_msg = f"The route name '{data.name}' is already in use."

        raise HTTPException(
            status_code=400, detail={"error": "duplicate_entry", "message": detail_msg}
        )


# @router.post("/routes/{route_id}/stops")
# async def add_bulk_stops(
#     route_id: str,
#     data: BulkStopAddRequest,
#     db: AsyncSession = Depends(get_async_session),
# ) -> dict:
#     # 1. Check if the route exists
#     route = await db.get(schema.Route, route_id)
#     if not route:
#         raise HTTPException(status_code=404, detail="Route ID not found.")

#     # 2. Find where the current sequence ends
#     seq_stmt = select(func.max(schema.RouteStop.sequence_no)).where(
#         schema.RouteStop.route_id == route_id
#     )
#     result = await db.execute(seq_stmt)
#     last_seq = result.scalar() or 0

#     # 3. Create stop entries in order
#     new_entries = []
#     for i, stop_info in enumerate(data.stops):
#         current_seq = last_seq + i + 1

#         # If this is the absolute beginning of a route, force time to 0
#         time_gap = 0 if current_seq == 1 else stop_info.assume_time_diff_minutes

#         rs = schema.RouteStop(
#             route_id=route_id,
#             stop_id=stop_info.stop_id,
#             sequence_no=current_seq,
#             boarding_allowed=stop_info.boarding_allowed,
#             deboarding_allowed=stop_info.deboarding_allowed,
#             assume_time_diff_minutes=time_gap,
#         )
#         new_entries.append(rs)

#     db.add_all(new_entries)

#     try:
#         await db.commit()
#     except Exception as e:
#         await db.rollback()
#         raise HTTPException(
#             status_code=500,
#             detail={
#                 "error": "db_error",
#                 "message": "Failed to save stops.",
#                 "debug": str(e),
#             },
#         )

#     return {
#         "status": "success",
#         "route_id": route_id,
#         "added_count": len(new_entries),
#         "total_sequence": last_seq + len(new_entries),
#     }


@router.post("/routes/{route_id}/stops")
async def add_bulk_stops(
    route_id: str,
    data: BulkStopAddRequest,
    db: AsyncSession = Depends(get_async_session),
) -> dict:
    # 1. Check if the route exists
    route = await db.get(schema.Route, route_id)
    if not route:
        raise HTTPException(status_code=404, detail="Route ID not found.")

    # --- NEW CHECK: DUPLICATES IN REQUEST ---
    input_stop_ids = [s.stop_id for s in data.stops]
    if len(input_stop_ids) != len(set(input_stop_ids)):
        raise HTTPException(
            status_code=400, detail="Duplicate Stop IDs found in the request."
        )

    # --- NEW CHECK: DUPLICATES IN DATABASE ---
    existing_stops_stmt = select(schema.RouteStop.stop_id).where(
        schema.RouteStop.route_id == route_id
    )
    existing_result = await db.execute(existing_stops_stmt)
    existing_stop_ids = set(existing_result.scalars().all())

    for stop_id in input_stop_ids:
        if stop_id in existing_stop_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Stop ID {stop_id} is already added to this route.",
            )

    # 2. Find where the current sequence ends
    seq_stmt = select(func.max(schema.RouteStop.sequence_no)).where(
        schema.RouteStop.route_id == route_id
    )
    result = await db.execute(seq_stmt)
    last_seq = result.scalar() or 0

    # 3. Create stop entries in order
    new_entries = []
    for i, stop_info in enumerate(data.stops):
        current_seq = last_seq + i + 1

        # If this is the absolute beginning of a route, force time to 0
        time_gap = 0 if current_seq == 1 else stop_info.assume_time_diff_minutes

        rs = schema.RouteStop(
            route_id=route_id,
            stop_id=stop_info.stop_id,
            sequence_no=current_seq,
            boarding_allowed=stop_info.boarding_allowed,
            deboarding_allowed=stop_info.deboarding_allowed,
            assume_time_diff_minutes=time_gap,
        )
        new_entries.append(rs)

    db.add_all(new_entries)

    # --- NEW CHECK: MINIMUM 2 STOPS LOGIC ---
    total_sequence = last_seq + len(new_entries)

    # Update isActive based on total stop count
    if total_sequence < 2:
        route.is_active = False
    else:
        # Optional: auto-enable if it meets the criteria
        route.is_active = True

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail={
                "error": "db_error",
                "message": "Failed to save stops.",
                "debug": str(e),
            },
        )

    # Returning exact same names for frontend compatibility
    return {
        "status": "success",
        "route_id": route_id,
        "added_count": len(new_entries),
        "total_sequence": total_sequence,
    }


@router.get("/routes/all")
async def get_all_routes(db: AsyncSession = Depends(get_async_session)):
    # We use joinedload to count stops without extra queries
    stmt = (
        select(schema.Route)
        .options(joinedload(schema.Route.route_stops))
        .order_by(schema.Route.created_at.desc())
    )
    result = await db.execute(stmt)
    routes = result.unique().scalars().all()

    return [
        {
            "route_id": r.id,
            "name": r.name,
            "code": r.code,
            "is_active": r.is_active,
            "total_stops": len(r.route_stops),
            "created_at": r.created_at,
        }
        for r in routes
    ]


@router.get("/routes/{route_id}")
async def get_route_details(
    route_id: str, db: AsyncSession = Depends(get_async_session)
):
    # Fetch route and join stops in the correct sequence order
    stmt = (
        select(schema.Route)
        .options(joinedload(schema.Route.route_stops).joinedload(schema.RouteStop.stop))
        .where(schema.Route.id == route_id)
    )

    result = await db.execute(stmt)
    route = result.unique().scalar_one_or_none()

    if not route:
        raise HTTPException(status_code=404, detail="Route not found")

    # Format the stops into a clean list
    ordered_stops = [
        {
            "sequence_no": rs.sequence_no,
            "stop_id": rs.stop.id,
            "stop_name": rs.stop.name,
            "latitude": float(rs.stop.lat),
            "longitude": float(rs.stop.lng),
            "boarding": rs.boarding_allowed,
            "deboarding": rs.deboarding_allowed,
            "time_diff_min": rs.assume_time_diff_minutes,
        }
        for rs in route.route_stops
    ]

    return {
        "route_id": route.id,
        "name": route.name,
        "code": route.code,
        "is_active": route.is_active,
        "path": ordered_stops,
    }


@router.patch("/routes/{route_id}/toggle")
async def toggle_route(
    route_id: str,
    data: RouteStatusUpdate,
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    route = await service.toggle_route_status(route_id, data.is_active)

    if not route:
        raise HTTPException(status_code=404, detail="Route not found")

    status_text = "Activated" if route.is_active else "Deactivated"
    return {
        "status": "success",
        "message": f"Route '{route.name}' has been {status_text}.",
        "route_id": route.id,
        "current_status": route.is_active,
    }


# ------------------------  routs fares routes --------------------------
@router.post("/routes/fares/bulk-set")
async def set_route_fares(
    data: RouteFareCreate, db: AsyncSession = Depends(get_async_session)
):
    # 1. Verify the route exists
    route_stmt = select(schema.Route).where(schema.Route.id == data.route_id)
    route_res = await db.execute(route_stmt)
    if not route_res.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Route not found")

    new_fares_count = 0
    updated_fares_count = 0

    for entry in data.fares:
        # 2. Check if a fare rule already exists for this specific path
        stmt = select(schema.RouteFare).where(
            schema.RouteFare.route_id == data.route_id,
            schema.RouteFare.pickup_stop_id == entry.pickup_stop_id,
            schema.RouteFare.dropoff_stop_id == entry.dropoff_stop_id,
        )
        result = await db.execute(stmt)
        existing_fare = result.scalar_one_or_none()

        if existing_fare:
            existing_fare.amount = entry.amount
            updated_fares_count += 1
        else:
            new_fare = schema.RouteFare(
                route_id=data.route_id,
                pickup_stop_id=entry.pickup_stop_id,
                dropoff_stop_id=entry.dropoff_stop_id,
                amount=entry.amount,
            )
            db.add(new_fare)
            new_fares_count += 1

    await db.commit()

    return {
        "status": "success",
        "route_id": data.route_id,
        "new_rules": new_fares_count,
        "updated_rules": updated_fares_count,
    }


@router.get("/routes/{route_id}/fares")
async def get_route_fares(route_id: str, db: AsyncSession = Depends(get_async_session)):
    # Fetch fares with stop names for the Admin to read easily
    stmt = (
        select(schema.RouteFare)
        .options(
            joinedload(schema.RouteFare.pickup_stop),
            joinedload(schema.RouteFare.dropoff_stop),
        )
        .where(schema.RouteFare.route_id == route_id)
    )
    result = await db.execute(stmt)
    fares = result.scalars().all()

    return [
        {
            "fare_id": f.id,
            "pickup_stop_id": f.pickup_stop_id,
            "dropoff_stop_id": f.dropoff_stop_id,
            "from": f.pickup_stop.name,
            "to": f.dropoff_stop.name,
            "amount": float(f.amount),
            "is_active": f.is_active,
        }
        for f in fares
    ]


@router.get("/routes/{route_id}/full-report")
async def get_route_and_trip_details(
    route_id: str, db: AsyncSession = Depends(get_async_session)
):
    # Notice the change from .stops to .route_stops
    # and .trips to .scheduled_trips
    stmt = (
        select(schema.Route)
        .options(
            joinedload(schema.Route.route_stops).joinedload(schema.RouteStop.stop),
            joinedload(schema.Route.scheduled_trips)
            .joinedload(schema.ScheduledTrip.driver)  # Load the User
            .joinedload(schema.User.driver_profile),  # Load the Profile from User
            joinedload(schema.Route.scheduled_trips).joinedload(
                schema.ScheduledTrip.vehicle
            ),
        )
        .where(schema.Route.id == route_id)
    )

    result = await db.execute(stmt)
    route = result.unique().scalar_one_or_none()

    if not route:
        raise HTTPException(status_code=404, detail="Route not found")

    return {
        "route_info": {
            "name": route.name,
            "code": route.code,
            "stops": [
                {
                    "seq": rs.sequence_no,
                    "name": rs.stop.name,
                    "lat": float(rs.stop.lat),
                    "lng": float(rs.stop.lng),
                    "time_offset": rs.assume_time_diff_minutes,
                }
                for rs in route.route_stops  # route_stops is already sorted by sequence_no in your schema
            ],
        },
        "trips": [
            {
                "trip_id": trip.id,
                "status": trip.status,
                "scheduled_start": trip.planned_start_at,  # Changed from scheduled_start_time to match your schema
                "driver": {
                    # Accessing via trip.driver.driver_profile
                    "name": trip.driver.driver_profile.full_name
                    if (trip.driver and trip.driver.driver_profile)
                    else "Unassigned",
                    "phone": trip.driver.driver_profile.phone
                    if (trip.driver and trip.driver.driver_profile)
                    else "N/A",
                },
                "vehicle": {
                    "reg_no": trip.vehicle.registration_number
                    if trip.vehicle
                    else "Unassigned",
                    "model": trip.vehicle.vehicle_model if trip.vehicle else "N/A",
                    "capacity": trip.vehicle.seat_count if trip.vehicle else 0,
                },
            }
            for trip in route.scheduled_trips
        ],
    }


# ----------------------  trips routes -------------------


@router.get("/trips/monitor")
async def monitor_all_trips(
    status: Optional[schema.ScheduledTripStatus] = Query(None),
    db: AsyncSession = Depends(get_async_session),
):
    # Initialize the combined AdminService
    service = AdminService(db)
    trips = await service.get_all_trips(status=status)

    return [
        {
            "trip_id": t.id if t else "N/A",
            "route_name": t.route.name if t else "N/A",
            "route_code": t.route.code if t else "N/A",
            # "driver": t.driver.driver_profile.full_name
            # if t.driver and t.driver.driver_profile
            # else "No Driver Assigned",
            # Assuming 'full_name' exists in User model
            # In your router list comprehension:
            "driver": t.driver.driver_profile.full_name
            if t.driver and t.driver.driver_profile
            else "No Driver Assigned",
            "vehicle": f"{t.vehicle.registration_number}" if t else "N/A",
            "planned_start": t.planned_start_at if t else "N/A",
            "status": t.status if t else "N/A",
            "bookings_count": len(t.bookings) if t else "N/A",
            "cancellation_reason": t.cancellation_reason
            if t.cancellation_reason
            else None,
            "premature_end_reason": t.premature_end_reason
            if t.premature_end_reason
            else None,
            "admin_note": t.admin_note if t.admin_note else None,
        }
        for t in trips
    ]


@router.patch("/trips/{trip_id}/cancel")
async def cancel_trip_by_id(
    trip_id: str,
    reason: str = Body(..., embed=True),
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    result = await service.cancel_trip(trip_id, reason)

    if not result["success"]:
        # If it's a timing error, use 400. If it's a missing trip, use 404.
        status_code = 404 if "not found" in result["error"] else 400
        raise HTTPException(status_code=status_code, detail=result["error"])

    return {
        "status": "success",
        "message": f"Trip {trip_id} has been cancelled successfully.",
    }


# @router.patch("/trips/{trip_id}/cancel")
# async def cancel_trip_by_id(
#     trip_id: str,
#     reason: str = Body(..., embed=True),
#     db: AsyncSession = Depends(get_async_session),
# ):
#     service = AdminService(db)
#     success = await service.cancel_trip(trip_id, reason)

#     if not success:
#         raise HTTPException(status_code=404, detail="Trip not found in database.")

#     return {"status": "success", "message": f"Trip {trip_id} has been cancelled."}


# @router.get("/trips/{trip_id}")
# async def get_specific_trip_status(
#     trip_id: str, db: AsyncSession = Depends(get_async_session)
# ):
#     service = AdminService(db)
#     trip = await service.get_trip_by_id(trip_id)

#     if not trip:
#         raise HTTPException(status_code=404, detail="Trip not found")

#     return {
#         "trip_id": trip.id,
#         "status": trip.status,  # ACTIVE, COMPLETED, or CANCELLED
#         "route": {"name": trip.route.name, "code": trip.route.code},
#         "assignment": {
#             # "driver": trip.driver.full_name,
#             "driver": trip.driver.driver_profile.full_name
#             if trip.driver and trip.driver.driver_profile
#             else "No Driver Assigned",
#             "vehicle": trip.vehicle.registration_number,
#         },
#         "timing": {
#             "planned_start": trip.planned_start_at,
#             "actual_start": trip.actual_start_at,
#             "planned_end": trip.planned_end_at,
#             "actual_end": trip.actual_end_at,
#         },
#         "occupancy": {
#     "total_bookings": len(trip.bookings),
#     "passengers": [
#         {
#             # Access passenger_profile instead of passenger directly
#             "name": b.passenger.passenger_profile.full_name
#             if b.passenger and b.passenger.passenger_profile
#             else "Unknown Passenger",
#             "status": b.booking_status
#         }
#         for b in trip.bookings
#     ],
# },
#         "admin_note": trip.admin_note,
#     }


@router.get("/trips/{trip_id}")
async def get_specific_trip_status(
    trip_id: str, db: AsyncSession = Depends(get_async_session)
):
    service = AdminService(db)
    trip = await service.get_trip_by_id(trip_id)

    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    return {
        "trip_id": trip.id,
        "status": trip.status,
        "route": {"name": trip.route.name, "code": trip.route.code},
        "assignment": {
            # FIX: Access .driver_profile.full_name
            "driver": trip.driver.driver_profile.full_name
            if trip.driver and trip.driver.driver_profile
            else "No Driver Assigned",
            "vehicle": trip.vehicle.registration_number,
        },
        "timing": {
            "planned_start": trip.planned_start_at,
            "actual_start": trip.actual_start_at,
            "planned_end": trip.planned_end_at,
            "actual_end": trip.actual_end_at,
        },
        "cancelation": {
            "cancellation_reason": trip.cancellation_reason if trip else "N/A",
            "premature_end_reason": trip.premature_end_reason if trip else "N/A",
        },
        "occupancy": {
            "total_bookings": len(trip.bookings),
            "passengers": [
                {
                    # FIX: Access .passenger_profile.full_name
                    "name": b.passenger.passenger_profile.full_name
                    if b.passenger and b.passenger.passenger_profile
                    else "Unknown Passenger",
                    "status": b.booking_status,
                }
                for b in trip.bookings
            ],
        },
        "admin_note": trip.admin_note,
    }


# ----------------------   check all bookings ------------------------------
@router.get("/trips/{trip_id}/bookings")
async def get_all_bookings_for_trip(
    trip_id: str, db: AsyncSession = Depends(get_async_session)
):
    service = AdminService(db)
    bookings = await service.get_trip_bookings(trip_id)

    if not bookings:
        return []  # Return empty list if no one has booked yet

    return bookings


@router.patch("/bookings/{booking_id}/noshow")
async def record_passenger_no_show(
    booking_id: str, db: AsyncSession = Depends(get_async_session)
):
    service = AdminService(db)
    success = await service.mark_no_show(booking_id)

    if not success:
        raise HTTPException(status_code=404, detail="Booking record not found.")

    return {"status": "success", "message": f"Booking {booking_id} marked as No-Show."}


# app/admin/router.py


@router.post("/drivers/{driver_id}/setup-payout-account")
async def setup_payout_account(
    driver_id: str, db: AsyncSession = Depends(get_async_session)
):
    service = AdminService(db)
    account_id = await service.create_driver_linked_account(driver_id)
    return {"status": "success", "razorpay_account_id": account_id}


@router.post("/payouts/batch-process")
async def batch_process_driver_payouts(
    driver_id: str,
    month: int = Query(..., ge=1, le=12),
    year: int = Query(..., ge=2024),
    db: AsyncSession = Depends(get_async_session),
):
    payout_service = RoutePayoutService(db)
    summary = await payout_service.process_monthly_payouts_for_driver(
        driver_id, month, year
    )
    return {"status": "batch_completed", "details": summary}


# ---------------- drivers ratings -------------------------
@router.get("/driver-ratings")
async def view_driver_ratings(db: AsyncSession = Depends(get_async_session)):
    service = AdminService(db)
    report = await service.get_driver_ratings_report()
    return [
        {
            "driver_id": r.id,
            "driver_name": r.full_name if r else "N/A",
            "email": r.email if r else "N/A",
            "avg_rating": round(float(r.avg_driver_rating), 2),
            "trip_quality": round(float(r.avg_trip_rating), 2),
            "review_count": r.total_reviews if r else "N/A",
        }
        for r in report
    ]


@router.get("/incidents")
async def view_all_incidents(db: AsyncSession = Depends(get_async_session)):
    service = AdminService(db)
    data = await service.get_flagged_incidents()

    return {
        "failed_trips": [
            {
                "id": t.id,
                "status": t.status,
                "reason": t.premature_end_reason or t.cancellation_reason,
                "driver": t.driver.driver_profile.full_name
                if t.driver.driver_profile
                else t.driver.email,
                "admin_note": t.admin_note,
            }
            for t in data["trips"]
        ],
        "passenger_complaints": [
            {
                "booking_id": r.booking_id,
                "rating": r.driver_rating,
                "comment": r.review_text,
                "passenger": r.passenger.email,
                "driver": r.driver.driver_profile.full_name
                if r.driver.driver_profile
                else r.driver.email,
            }
            for r in data["bad_reviews"]
        ],
    }


@router.post("/resolve-trip/{trip_id}")
async def resolve_trip_issue(
    trip_id: str,
    note: str = Body(..., embed=True),
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    updated_trip = await service.update_admin_resolution(trip_id, note)
    if not updated_trip:
        raise HTTPException(status_code=404, detail="Trip not found")
    return {"message": "Incident resolved", "admin_note": updated_trip.admin_note}


# -------------------- support sections ---------------------------
@router.get("/tickets")
async def list_tickets(
    status: Optional[schema.SupportStatus] = None,
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    tickets = await service.get_all_support_tickets(status)
    return [
        {
            "id": t.id,
            "user": t.user.email,
            "subject": t.subject,
            "description": t.description,
            "status": t.status,
            "path": t.attachment_path,
            "created_at": t.created_at,
            "resolved_at_by_admin": t.resolved_at if t else "N/A",
            "rejection_reason_by_admin": t.rejection_reason if t else "N/A",
        }
        for t in tickets
    ]


@router.post("/tickets/{ticket_id}/action")
async def handle_ticket(
    ticket_id: str,
    action: str,  # 'resolve' or 'reject'
    note: str = Body(..., embed=True),
    current_admin: schema.User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    ticket = await service.resolve_ticket(ticket_id, current_admin.id, note, action)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return {"message": f"Ticket {action} successfully"}


# app/users/endpoints/router.py
@router.post("/support/create")
async def create_ticket(
    subject: str = Body(...),
    description: str = Body(...),
    current_user: schema.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    new_ticket = schema.SupportTicket(
        user_id=current_user.id,
        subject=subject,
        description=description,
        status=schema.SupportStatus.PENDING,
    )
    db.add(new_ticket)
    await db.commit()
    return {"message": "Ticket created. Support will contact you soon."}


@router.get("/reviews")
async def view_all_reviews(
    low_only: bool = False, db: AsyncSession = Depends(get_async_session)
):
    service = AdminService(db)
    # If low_only is true, only show 1 and 2 star reviews
    reviews = await service.get_all_reviews(min_rating=2 if low_only else None)

    return [
        {
            "id": r.id,
            "ratings": {"trip": r.trip_rating, "driver": r.driver_rating},
            "feedback": r.review_text,
            "passenger": r.passenger.passenger_profile.full_name
            if r.passenger.passenger_profile
            else r.passenger.email,
            "driver": r.driver.driver_profile.full_name
            if r.driver.driver_profile
            else r.driver.email,
            "trip_details": {
                "route": r.scheduled_trip.route.name,
                "date": r.scheduled_trip.planned_start_at,
            },
        }
        for r in reviews
    ]


@router.get("/{user_id}/full_details")
async def get_complete_user_audit(
    user_id: str = Path(..., description="The UUID of the user"),
    db: AsyncSession = Depends(get_async_session),
):
    # 1. Fetch User with every possible related record
    stmt = (
        select(schema.User)
        .options(
            # Profiles
            joinedload(schema.User.passenger_profile),
            joinedload(schema.User.driver_profile),
            # Driver Assets & Money
            joinedload(schema.User.vehicle),
            joinedload(schema.User.payout_details),
            # Recent Activity (Passenger side)
            joinedload(schema.User.passenger_bookings).options(
                joinedload(schema.TripBooking.payments),
                joinedload(schema.TripBooking.rating),
            ),
            # FIX: Removed .limit(10) from here
            joinedload(schema.User.driven_trips),
            # Ratings received as driver
            joinedload(schema.User.ratings_received_as_driver),
        )
        .where(schema.User.id == user_id)
    )

    result = await db.execute(stmt)
    user = result.unique().scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # 2. Calculate Driver Rating Average
    avg_rating = 0.0
    if user.ratings_received_as_driver:
        avg_rating = sum(
            r.driver_rating for r in user.ratings_received_as_driver
        ) / len(user.ratings_received_as_driver)

    # 3. Construct the response (Handle slicing in Python)
    return {
        "identity": {
            "id": user.id,
            "email": user.email,
            "role": user.role,
            "status": "ACTIVE" if user.is_active else "BANNED",
            "joined_on": user.created_at,
        },
        "financial_summary": {
            "payout_status": user.payout_details.linked_account_status
            if user.payout_details
            else "N/A",
            "razorpay_account": user.payout_details.razorpay_linked_account_id
            if user.payout_details
            else None,
            "is_eligible_for_funds": user.payout_details.is_payout_eligible
            if user.payout_details
            else False,
        },
        "performance_metrics": {
            "driver_avg_rating": round(avg_rating, 2),
            "total_trips_driven": len(user.driven_trips),
            "total_bookings_made": len(user.passenger_bookings),
        },
        "recent_bookings": [
            {
                "booking_id": b.id,
                "status": b.booking_status,
                "amount": b.fare_amount,
                "payment_status": b.payments[0].status
                if b.payments
                else "NO_PAYMENT_INITIATED",
                "booked_at": b.created_at,
            }
            for b in user.passenger_bookings[:5]
        ],
        # Added this to show the driven trips you were trying to limit
        "recent_driven_trips": [
            {"trip_id": t.id, "status": t.status, "started_at": t.actual_start_at}
            for t in user.driven_trips[:10]  # Sliced in Python
        ],
        "compliance_check": {
            "aadhaar": user.driver_profile.aadhaar_number
            if user.driver_profile
            else None,
            "license": user.driver_profile.driving_license_number
            if user.driver_profile
            else None,
            "vehicle_reg": user.vehicle.registration_number if user.vehicle else None,
            "verification": user.driver_profile.verification_status
            if user.driver_profile
            else "N/A",
        },
    }


@router.get("/{user_id}/transaction_history")
async def get_user_transaction_history(
    user_id: str = Path(..., description="User UUID"),
    db: AsyncSession = Depends(get_async_session),
):
    # 1. Get Payments (User as Passenger)
    # We join TripBooking to ensure the payment belongs to this user
    payment_stmt = (
        select(schema.BookingPayment)
        .join(schema.TripBooking)
        .where(schema.TripBooking.passenger_user_id == user_id)
        .options(joinedload(schema.BookingPayment.booking))
        .order_by(schema.BookingPayment.created_at.desc())
    )

    # 2. Get Payouts (User as Driver)
    transfer_stmt = (
        select(schema.BookingTransfer)
        .where(schema.BookingTransfer.driver_user_id == user_id)
        .options(joinedload(schema.BookingTransfer.booking))
        .order_by(schema.BookingTransfer.created_at.desc())
    )

    payments_res = await db.execute(payment_stmt)
    transfers_res = await db.execute(transfer_stmt)

    payments = payments_res.scalars().all()
    transfers = transfers_res.scalars().all()

    return {
        "user_id": user_id,
        "summary": {
            "total_spent": sum(p.amount for p in payments if p.status == "paid"),
            "total_earned": sum(t.amount for t in transfers if t.status == "processed"),
        },
        "outbound_payments": [
            {
                "transaction_id": p.razorpay_payment_id,
                "order_id": p.razorpay_order_id,
                "amount": p.amount,
                "status": p.status,
                "date": p.created_at,
                "booking_id": p.booking_id,
            }
            for p in payments
        ],
        "inbound_payouts": [
            {
                "transfer_id": t.razorpay_transfer_id,
                "amount": t.amount,
                "status": t.status,
                "date": t.processed_at or t.created_at,
                "failure_reason": t.failure_reason,
                "booking_id": t.booking_id,
            }
            for t in transfers
        ],
    }


@router.get("/bookings/{booking_id}/rating")
async def get_booking_rating(
    booking_id: str, db: AsyncSession = Depends(get_async_session)
):
    stmt = (
        select(schema.BookingRating)
        .options(
            joinedload(schema.BookingRating.passenger),
            joinedload(schema.BookingRating.driver),
        )
        .where(schema.BookingRating.booking_id == booking_id)
    )
    result = await db.execute(stmt)
    rating = result.scalar_one_or_none()

    if not rating:
        raise HTTPException(status_code=404, detail="No rating found for this booking")

    return {
        "trip_rating": rating.trip_rating,
        "driver_rating": rating.driver_rating,
        "review": rating.review_text,
        "passenger_name": rating.passenger.email,  # Or profile name
        "created_at": rating.created_at,
    }


@router.get("/transactions/all")
async def get_all_transactions(
    skip: int = 0,
    limit: int = 50,
    status: schema.BookingStatus = None,
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    bookings = await service.fetch_detailed_transactions(skip, limit, status)

    report = []
    for b in bookings:
        # Payout consistency check
        audit_payout = b.fare_amount - b.commission_amount
        is_payout_correct = audit_payout == b.driver_payout_amount

        report.append(
            {
                "booking_id": b.id,
                "timestamp": b.created_at,
                "status": b.booking_status,
                "passenger": {
                    "name": b.passenger.passenger_profile.full_name
                    if b.passenger.passenger_profile
                    else "N/A",
                    "email": b.passenger.email,
                },
                "trip_details": {
                    "route_name": b.route.name if b.route else "N/A",
                    "pickup": {
                        "id": b.pickup_stop_id,
                        "name": b.pickup_stop.name if b.pickup_stop else "N/A",
                    },
                    "dropoff": {
                        "id": b.dropoff_stop_id,
                        "name": b.dropoff_stop.name if b.dropoff_stop else "N/A",
                    },
                    "driver_name": b.scheduled_trip.driver.driver_profile.full_name
                    if (b.scheduled_trip and b.scheduled_trip.driver.driver_profile)
                    else "Unknown",
                },
                "financials": {
                    "total_fare": float(b.fare_amount),
                    "commission_percent": float(b.commission_percent_snapshot),
                    "admin_earned": float(b.commission_amount),
                    "driver_payout": float(b.driver_payout_amount),
                    "audit_passed": is_payout_correct,
                },
                # NEW: Refund/Cancellation Audit Logic
                "refund_info": {
                    "is_refunded": b.booking_status in ["cancelled", "refunded"],
                    "reason": b.cancellation_reason
                    if hasattr(b, "cancellation_reason")
                    else "No reason provided",
                    "cancelled_by": b.cancelled_by
                    if hasattr(b, "cancelled_by")
                    else "N/A",
                    "cancelled_at": b.cancelled_at,
                }
                if b.booking_status in ["cancelled", "refunded"]
                else None,
                "payment_gateway": [
                    {
                        "razorpay_order_id": p.razorpay_order_id,
                        "razorpay_payment_id": p.razorpay_payment_id,
                        "payment_status": p.status,
                    }
                    for p in b.payments
                ],
                "security_scans": [
                    {
                        "type": s.scan_type,
                        "time": s.created_at,
                        "at_correct_stop": s.within_radius,
                    }
                    for s in b.scan_events
                ],
            }
        )

    return {"total_count": len(report), "data": report}


# ============================================================
# Admin payout management by Anubhab Dey
# ============================================================


@router.get("/payouts/settings")
async def get_payout_settings(
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    return await service.get_payout_settings()


@router.patch("/payouts/settings")
async def patch_payout_settings(
    payload: PayoutSettingsUpdate,
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    return await service.update_payout_settings(payload.commission_percent)


@router.get("/payouts/drivers")
async def list_driver_payout_profiles(
    linked_account_status: Optional[schema.LinkedAccountStatus] = Query(default=None),
    is_payout_eligible: Optional[bool] = Query(default=None),
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    return await service.list_driver_payout_profiles(
        linked_account_status=linked_account_status,
        is_payout_eligible=is_payout_eligible,
    )


@router.get("/payouts/drivers/{driver_user_id}")
async def get_driver_payout_profile(
    driver_user_id: str,
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    return await service.get_driver_payout_profile(driver_user_id)


@router.put("/payouts/drivers/{driver_user_id}/details")
async def upsert_driver_payout_details(
    driver_user_id: str,
    payload: DriverPayoutDetailsUpsert,
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    return await service.upsert_driver_payout_details(driver_user_id, payload)


@router.patch("/payouts/drivers/{driver_user_id}/linked-account")
async def patch_driver_linked_account(
    driver_user_id: str,
    payload: DriverLinkedAccountUpdate,
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    return await service.update_driver_linked_account(driver_user_id, payload)


@router.patch("/payouts/drivers/{driver_user_id}/eligibility")
async def patch_driver_payout_eligibility(
    driver_user_id: str,
    payload: DriverPayoutEligibilityUpdate,
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    return await service.update_driver_payout_eligibility(driver_user_id, payload)


@router.get("/payouts/bookings")
async def list_payout_bookings(
    driver_user_id: Optional[str] = Query(default=None),
    passenger_user_id: Optional[str] = Query(default=None),
    booking_status: Optional[schema.BookingStatus] = Query(default=None),
    transfer_status: Optional[schema.TransferStatus] = Query(default=None),
    month: Optional[int] = Query(default=None, ge=1, le=12),
    year: Optional[int] = Query(default=None, ge=2000, le=2100),
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    return await service.list_payout_bookings(
        driver_user_id=driver_user_id,
        passenger_user_id=passenger_user_id,
        booking_status=booking_status,
        transfer_status=transfer_status,
        month=month,
        year=year,
    )


@router.get("/payouts/bookings/{booking_id}")
async def get_payout_booking_detail(
    booking_id: str,
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    return await service.get_payout_booking_detail(booking_id)


@router.post("/payouts/bookings/{booking_id}/trigger")
async def trigger_booking_payout(
    booking_id: str,
    payload: TriggerBookingPayoutRequest,
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    return await service.trigger_booking_payout(
        booking_id=booking_id,
        linked_account_id=payload.linked_account_id,
        require_completed=payload.require_completed,
    )


@router.post("/payouts/drivers/{driver_user_id}/trigger-monthly")
async def trigger_driver_monthly_payouts(
    driver_user_id: str,
    payload: TriggerDriverMonthlyPayoutRequest,
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    return await service.trigger_driver_monthly_payouts(
        driver_user_id=driver_user_id,
        month=payload.month,
        year=payload.year,
        linked_account_id=payload.linked_account_id,
    )


@router.post("/payouts/bulk-trigger")
async def trigger_bulk_payouts(
    payload: BulkPayoutTriggerRequest,
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    return await service.trigger_bulk_payouts(payload)


@router.get("/payouts/transfers")
async def list_booking_transfers(
    driver_user_id: Optional[str] = Query(default=None),
    status: Optional[schema.BookingTransferStatus] = Query(default=None),
    month: Optional[int] = Query(default=None, ge=1, le=12),
    year: Optional[int] = Query(default=None, ge=2000, le=2100),
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    return await service.list_booking_transfers(
        driver_user_id=driver_user_id,
        status=status,
        month=month,
        year=year,
    )


@router.get("/payouts/transfers/{transfer_id}")
async def get_booking_transfer_detail(
    transfer_id: str,
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    return await service.get_booking_transfer_detail(transfer_id)


@router.get("/payouts/refunds")
async def list_refund_queue(
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    return await service.list_refund_queue()


@router.post("/payouts/refunds/{booking_id}/reconcile")
async def reconcile_cancelled_booking_refund(
    booking_id: str,
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    return await service.reconcile_cancelled_booking_refund(booking_id)


@router.get("/payouts/dashboard")
async def get_payout_dashboard(
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    return await service.get_payout_dashboard()


@router.post("/payouts/drivers/{driver_user_id}/create-linked-account")
async def create_and_save_driver_linked_account(
    driver_user_id: str,
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    return await service.create_and_save_driver_linked_account(driver_user_id)


@router.post("/payouts/drivers/{driver_user_id}/sync-linked-account")
async def sync_driver_linked_account(
    driver_user_id: str,
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    return await service.sync_driver_linked_account(driver_user_id)


@router.get("/payouts/drivers/{driver_user_id}/linked-account/provider")
async def get_driver_linked_account_provider_detail(
    driver_user_id: str,
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminService(db)
    return await service.get_driver_linked_account_provider_detail(driver_user_id)


# ----------------- transactions details ---------------------
# @router.get("/transactions/all")
# async def get_all_transactions(
#     skip: int = 0,
#     limit: int = 50,
#     status: schema.BookingStatus = None,
#     db: AsyncSession = Depends(get_async_session),
#     # current_admin: schema.User = Depends(get_admin_user) # Logic to verify Admin role
# ):
#     """
#     Fetches every detail of a transaction:
#     Passenger, Driver, Route, Payment IDs, and QR Scans.
#     """

#     # 1. Build the Query with deep joins
#     stmt = (
#         select(schema.TripBooking)
#         .options(
#             joinedload(schema.TripBooking.passenger).joinedload(
#                 schema.User.passenger_profile
#             ),
#             joinedload(schema.TripBooking.scheduled_trip)
#             .joinedload(schema.ScheduledTrip.driver)
#             .joinedload(schema.User.driver_profile),
#             joinedload(schema.TripBooking.route),
#             joinedload(schema.TripBooking.pickup_stop),
#             joinedload(schema.TripBooking.dropoff_stop),
#             joinedload(schema.TripBooking.payments),  # Razorpay Order/Payment IDs
#             joinedload(schema.TripBooking.scan_events),  # Board/Drop QR history
#         )
#         .order_by(schema.TripBooking.created_at.desc())
#         .offset(skip)
#         .limit(limit)
#     )

#     if status:
#         stmt = stmt.where(schema.TripBooking.booking_status == status)

#     result = await db.execute(stmt)
#     bookings = result.unique().scalars().all()

#     # 2. Format the response for the Admin UI
#     report = []
#     for b in bookings:
#         # Calculate expected payout vs actual for audit
#         audit_payout = b.fare_amount - b.commission_amount
#         is_payout_correct = audit_payout == b.driver_payout_amount

#         report.append(
#             {
#                 "booking_id": b.id,
#                 "timestamp": b.created_at,
#                 "status": b.booking_status,
#                 "passenger": {
#                     "name": b.passenger.passenger_profile.full_name
#                     if b.passenger.passenger_profile
#                     else "N/A",
#                     "email": b.passenger.email,
#                 },
#                 "trip_details": {
#                     "route_name": b.route.name,
#                     "pickup": b.pickup_stop.name,
#                     "dropoff": b.dropoff_stop.name,
#                     "driver_name": b.scheduled_trip.driver.driver_profile.full_name
#                     if b.scheduled_trip.driver.driver_profile
#                     else "Unknown",
#                 },
#                 "financials": {
#                     "total_fare": float(b.fare_amount),
#                     "commission_percent": float(b.commission_percent_snapshot),
#                     "admin_earned": float(b.commission_amount),
#                     "driver_payout": float(b.driver_payout_amount),
#                     "audit_passed": is_payout_correct,
#                 },
#                 "payment_gateway": [
#                     {
#                         "razorpay_order_id": p.razorpay_order_id,
#                         "razorpay_payment_id": p.razorpay_payment_id,
#                         "payment_status": p.status,
#                     }
#                     for p in b.payments
#                 ],
#                 "security_scans": [
#                     {
#                         "type": s.scan_type,
#                         "time": s.created_at,
#                         "at_correct_stop": s.within_radius,
#                     }
#                     for s in b.scan_events
#                 ],
#             }
#         )

#     return {"total_count": len(report), "data": report}
