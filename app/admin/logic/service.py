import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import joinedload

from app.db import schema
from app.payments.service import RoutePayoutService


class AdminService:
    def __init__(self, db):
        self.db = db

    async def fetch_detailed_drivers(self):
        stmt = (
            select(schema.User)
            .filter(schema.User.role == schema.UserRole.DRIVER)
            .options(
                joinedload(schema.User.driver_profile),
                joinedload(schema.User.vehicle),
                joinedload(schema.User.payout_details),
            )
        )
        result = await self.db.execute(stmt)
        return result.scalars().unique().all()

    async def fetch_detailed_passengers(self):
        """Fetches all passengers with profiles and bookings."""
        stmt = (
            select(schema.User)
            .filter(schema.User.role == schema.UserRole.PASSENGER)
            .options(
                joinedload(schema.User.passenger_profile),
                joinedload(schema.User.passenger_bookings),
            )
        )
        result = await self.db.execute(stmt)
        return result.scalars().unique().all()

    async def fetch_driver_by_id(self, user_id: str):
        """Fetches one driver by ID."""
        stmt = (
            select(schema.User)
            .filter(
                schema.User.id == user_id, schema.User.role == schema.UserRole.DRIVER
            )
            .options(
                joinedload(schema.User.driver_profile),
                joinedload(schema.User.vehicle),
                joinedload(schema.User.payout_details),
            )
        )
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def fetch_passenger_by_id(self, user_id: str):
        """Fetches one passenger by ID."""
        stmt = (
            select(schema.User)
            .filter(
                schema.User.id == user_id, schema.User.role == schema.UserRole.PASSENGER
            )
            .options(
                joinedload(schema.User.passenger_profile),
                joinedload(schema.User.passenger_bookings),
            )
        )
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def fetch_inactive_users(self, months: int = 3):
        threshold_date = datetime.now(timezone.utc) - timedelta(days=months * 30)

        stmt = (
            select(schema.User)
            .join(schema.UserSession)
            .options(
                joinedload(schema.User.passenger_profile),
                joinedload(schema.User.driver_profile),
            )
            .where(schema.UserSession.last_used_at < threshold_date)
            .distinct()
        )

        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def toggle_driver_status(self, user_id: str, active: bool):
        stmt = (
            update(schema.User)
            .where(
                schema.User.id == user_id, schema.User.role == schema.UserRole.DRIVER
            )
            .values(is_active=active)
        )

        await self.db.execute(stmt)
        await self.db.commit()
        return True

    async def update_driver_verification(
        self,
        user_id: str,
        status: schema.DriverVerificationStatus,
        rejection_reason: str = None,
    ):
        stmt = (
            update(schema.DriverProfile)
            .where(schema.DriverProfile.user_id == user_id)
            .values(
                verification_status=status,
                rejection_reason=rejection_reason,
                reviewed_at=datetime.now(timezone.utc),
            )
        )
        await self.db.execute(stmt)
        await self.db.commit()
        return True

    async def update_vehicle_verification(
        self,
        user_id: str,
        status: schema.VehicleVerificationStatus,
        rejection_reason: str = None,
    ):
        stmt = (
            update(schema.Vehicle)
            .where(schema.Vehicle.driver_user_id == user_id)
            .values(
                verification_status=status,
                rejection_reason=rejection_reason,
                reviewed_at=datetime.now(timezone.utc),
            )
        )
        await self.db.execute(stmt)
        await self.db.commit()
        return True

    async def upsert_stops_from_jsonl(self, file_content: str):
        lines = file_content.splitlines()
        count = 0
        for line in lines:
            if not line.strip():
                continue

            try:
                data = json.loads(line)
                location_name = data.get("location")
                lat = data.get("latitude")
                lng = data.get("longitude")

                # Check if this stop already exists in the database
                stmt = select(schema.Stop).where(schema.Stop.name == location_name)
                result = await self.db.execute(stmt)
                existing_stop = result.scalar_one_or_none()

                if existing_stop:
                    # Update coordinates if the building moved or was refined
                    existing_stop.lat = lat
                    existing_stop.lng = lng
                else:
                    # Create a new Stop in the 'Library'
                    new_stop = schema.Stop(
                        name=location_name,
                        lat=lat,
                        lng=lng,
                        radius_meters=150,  # Default geofence for Kolkata IT parks
                    )
                    self.db.add(new_stop)

                count += 1
            except json.JSONDecodeError:
                print(f"Skipping invalid JSON line: {line}")
                continue

        await self.db.commit()
        return count

    async def create_route_with_sequence(
        self, name: str, code: str, stop_ids: list[str]
    ):
        # 1. Create Route
        new_route = schema.Route(name=name, code=code)
        self.db.add(new_route)
        await self.db.flush()  # Get ID without committing

        # 2. Create Sequence (The Ordered Playlist)
        for index, s_id in enumerate(stop_ids):
            rs = schema.RouteStop(
                route_id=new_route.id, stop_id=s_id, sequence_order=index + 1
            )
            self.db.add(rs)

        await self.db.commit()
        return new_route

    async def toggle_route_status(self, route_id: str, is_active: bool):
        # 1. Fetch the specific route
        stmt = select(schema.Route).where(schema.Route.id == route_id)
        result = await self.db.execute(stmt)
        route = result.scalar_one_or_none()

        if not route:
            return None

        # 2. Update the status
        route.is_active = is_active
        await self.db.commit()
        return route

    async def get_all_trips(self, status=None):
        stmt = (
            select(schema.ScheduledTrip)
            .options(
                joinedload(schema.ScheduledTrip.route),
                joinedload(schema.ScheduledTrip.driver).joinedload(
                    schema.User.driver_profile
                ),  # Pre-load the profile too!
                joinedload(schema.ScheduledTrip.vehicle),
                joinedload(schema.ScheduledTrip.bookings),  # <--- ADD THIS LINE
            )
            .order_by(schema.ScheduledTrip.planned_start_at.desc())
        )
        if status:
            stmt = stmt.where(schema.ScheduledTrip.status == status)

        result = await self.db.execute(stmt)
        return result.unique().scalars().all()

    async def cancel_trip(self, trip_id: str, reason: str):
        trip_stmt = select(schema.ScheduledTrip).where(
            schema.ScheduledTrip.id == trip_id
        )
        res = await self.db.execute(trip_stmt)
        trip = res.scalar_one_or_none()

        if trip:
            trip.status = schema.ScheduledTripStatus.CANCELLED
            trip.admin_note = reason
            # Important: You would usually trigger a refund logic here for all bookings
            await self.db.commit()
            return True
        return False

    async def get_trip_by_id(self, trip_id: str):
        stmt = (
            select(schema.ScheduledTrip)
            .options(
                joinedload(schema.ScheduledTrip.route),
                joinedload(schema.ScheduledTrip.vehicle),
                # Combine driver + driver_profile into one chain
                joinedload(schema.ScheduledTrip.driver).joinedload(
                    schema.User.driver_profile
                ),
                # Combine bookings + passenger into one chain (PICK ONE: joinedload is fine here)
                joinedload(schema.ScheduledTrip.bookings).joinedload(
                    schema.TripBooking.passenger
                ),
            )
            .where(schema.ScheduledTrip.id == trip_id)
        )
        result = await self.db.execute(stmt)
        return result.unique().scalar_one_or_none()
    
    async def get_trip_bookings(self, trip_id: str):
        stmt = (
        select(schema.TripBooking)
        .options(
            joinedload(schema.TripBooking.passenger),
            joinedload(schema.TripBooking.pickup_stop),
            joinedload(schema.TripBooking.dropoff_stop)
        )
        .where(schema.TripBooking.scheduled_trip_id == trip_id)
    )
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def mark_no_show(self, booking_id: str):
      stmt = select(schema.TripBooking).where(schema.TripBooking.id == booking_id)
      res = await self.db.execute(stmt)
      booking = res.scalar_one_or_none()

      if booking:
        # Assuming NO_SHOW is a status in your Enum
        booking.booking_status = "NO_SHOW" 
        booking.updated_at = datetime.now(timezone.utc)
        await self.db.commit()
        return True
        return False

    # app/admin/logic/service.py (Add these methods)

async def create_driver_linked_account(self, driver_id: str):
    driver = await self.fetch_driver_by_id(driver_id)
    if not driver or not driver.driver_profile:
        raise HTTPException(status_code=404, detail="Driver profile not found")
    
    p = driver.driver_profile
    
    # 2. Razorpay Account Creation Payload
    payload = {
        "type": "route",
        "email": driver.email,
        "contact": p.phone,
        "profile": {
            "name": p.full_name,
            "addresses": {
                "permanent": {
                    "city": "Kolkata", # Based on your context
                    "state": "West Bengal",
                    "country": "IN"
                }
            }
        },
        "legal_business_name": p.full_name,
        "business_type": "individual"
    }
    
    # 3. Call Razorpay API (using the helper in RoutePayoutService)
    # Note: You should instantiate RoutePayoutService here
    payout_service = RoutePayoutService(self.db)
    response = await payout_service._razorpay_request(
        method="POST", 
        path="/accounts", 
        json_payload=payload
    )
    
    # 4. Save the Account ID to DriverPayoutDetails
    account_id = response.get("id")
    # Logic to update DriverPayoutDetails table with account_id and status='active'
    return account_id