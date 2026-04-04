import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import desc, func, select, update
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
        # 1. Fetch the trip to check timing
        trip_stmt = select(schema.ScheduledTrip).where(
            schema.ScheduledTrip.id == trip_id
        )
        res = await self.db.execute(trip_stmt)
        trip = res.scalar_one_or_none()

        if not trip:
            return {"success": False, "error": "Trip not found"}

        # 2. Enforce the One-Hour Rule
        now = datetime.now(timezone.utc)
        if now > (trip.planned_start_at - timedelta(hours=1)):
            return {
                "success": False,
                "error": "Cannot cancel. Must be done at least 1 hour before start.",
            }

        # 3. Update the Trip Status
        trip.status = schema.ScheduledTripStatus.CANCELLED
        trip.cancellation_reason = reason
        trip.admin_note = f"Admin Cancelled at {now}. Reason: {reason}"

        # 4. Cancel all Bookings under this trip ID
        # This performs a bulk update for efficiency
        booking_update_stmt = (
            update(schema.TripBooking)
            .where(schema.TripBooking.scheduled_trip_id == trip_id)
            .values(
                booking_status="CANCELLED",  # Adjust to your specific Enum if needed
                cancel_reason=f"Trip cancelled by admin: {reason}",
            )
        )
        await self.db.execute(booking_update_stmt)

        # 5. Commit both changes
        await self.db.commit()
        return {"success": True}

    # async def get_trip_by_id(self, trip_id: str):
    #     stmt = (
    #         select(schema.ScheduledTrip)
    #         .options(
    #             joinedload(schema.ScheduledTrip.route),
    #             joinedload(schema.ScheduledTrip.vehicle),
    #             # Combine driver + driver_profile into one chain
    #             joinedload(schema.ScheduledTrip.driver).joinedload(
    #                 schema.User.driver_profile
    #             ),
    #             # Combine bookings + passenger into one chain (PICK ONE: joinedload is fine here)
    #             joinedload(schema.ScheduledTrip.bookings).joinedload(
    #                 schema.TripBooking.passenger
    #             ),
    #         )
    #         .where(schema.ScheduledTrip.id == trip_id)
    #     )
    #     result = await self.db.execute(stmt)
    #     return result.unique().scalar_one_or_none()

    async def get_trip_by_id(self, trip_id: str):
        stmt = (
            select(schema.ScheduledTrip)
            .options(
                joinedload(schema.ScheduledTrip.route),
                joinedload(schema.ScheduledTrip.vehicle),
                joinedload(schema.ScheduledTrip.driver).joinedload(
                    schema.User.driver_profile
                ),
                # Update this chain to go from Booking -> Passenger -> PassengerProfile
                joinedload(schema.ScheduledTrip.bookings)
                .joinedload(schema.TripBooking.passenger)
                .joinedload(schema.User.passenger_profile),  # <--- Add this deep join
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
                joinedload(schema.TripBooking.dropoff_stop),
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
                        "city": "Kolkata",  # Based on your context
                        "state": "West Bengal",
                        "country": "IN",
                    }
                },
            },
            "legal_business_name": p.full_name,
            "business_type": "individual",
        }

        # 3. Call Razorpay API (using the helper in RoutePayoutService)
        # Note: You should instantiate RoutePayoutService here
        payout_service = RoutePayoutService(self.db)
        response = await payout_service._razorpay_request(
            method="POST", path="/accounts", json_payload=payload
        )

        # 4. Save the Account ID to DriverPayoutDetails
        account_id = response.get("id")
        # Logic to update DriverPayoutDetails table with account_id and status='active'
        return account_id

    # app/payments/service.py (Add to RoutePayoutService)

    async def get_driver_ratings_report(self):
        """Aggregates ratings for all drivers to find low performers."""
        stmt = (
            select(
                schema.DriverProfile.full_name,
                schema.User.email,
                func.avg(schema.BookingRating.driver_rating).label("avg_driver_rating"),
                func.avg(schema.BookingRating.trip_rating).label("avg_trip_rating"),
                func.count(schema.BookingRating.id).label("total_reviews"),
            )
            .join(schema.User, schema.User.id == schema.DriverProfile.user_id)
            .join(
                schema.BookingRating,
                schema.BookingRating.driver_user_id == schema.User.id,
            )
            .group_by(schema.DriverProfile.full_name, schema.User.email)
            .order_by(desc("avg_driver_rating"))
        )
        result = await self.db.execute(stmt)
        return result.all()

    async def get_flagged_incidents(self):
        """Finds trips that failed or have very low ratings (1-2 stars)."""
        # 1. Get Trips with issues
        stmt_trips = (
            select(schema.ScheduledTrip)
            .where(
                schema.ScheduledTrip.status.in_(
                    [
                        schema.ScheduledTripStatus.CANCELLED,
                        schema.ScheduledTripStatus.PREMATURE_END,
                    ]
                )
            )
            .options(
                joinedload(schema.ScheduledTrip.driver).joinedload(
                    schema.User.driver_profile
                )
            )
            .order_by(schema.ScheduledTrip.updated_at.desc())
        )

        # 2. Get Bad Reviews (1 or 2 stars)
        stmt_reviews = (
            select(schema.BookingRating)
            .where(schema.BookingRating.driver_rating <= 2)
            .options(
                joinedload(schema.BookingRating.passenger),
                joinedload(schema.BookingRating.driver).joinedload(
                    schema.User.driver_profile
                ),
            )
        )

        trips = (await self.db.execute(stmt_trips)).scalars().all()
        reviews = (await self.db.execute(stmt_reviews)).scalars().all()

        return {"trips": trips, "bad_reviews": reviews}

    async def update_admin_resolution(self, trip_id: str, note: str):
        """Allows Admin to 'work upon' an issue by adding a resolution note."""
        stmt = select(schema.ScheduledTrip).where(schema.ScheduledTrip.id == trip_id)
        result = await self.db.execute(stmt)
        trip = result.scalar_one_or_none()

        if trip:
            trip.admin_note = note
            await self.db.commit()
            return trip
        return None

    async def get_all_support_tickets(
        self, status: Optional[schema.SupportStatus] = None
    ):
        """Fetch all tickets with user details for the Admin."""
        stmt = (
            select(schema.SupportTicket)
            .options(joinedload(schema.SupportTicket.user))
            .order_by(schema.SupportTicket.created_at.desc())
        )
        if status:
            stmt = stmt.where(schema.SupportTicket.status == status)

        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def resolve_ticket(
        self, ticket_id: str, admin_id: str, resolution_note: str, action: str
    ):
        """Action can be 'resolved' or 'rejected'."""
        stmt = select(schema.SupportTicket).where(schema.SupportTicket.id == ticket_id)
        result = await self.db.execute(stmt)
        ticket = result.scalar_one_or_none()

        if not ticket:
            return None

        if action == "resolve":
            ticket.status = schema.SupportStatus.RESOLVED
            ticket.resolved_at = datetime.now()
            ticket.resolved_by_admin_id = admin_id
        else:
            ticket.status = schema.SupportStatus.REJECTED
            ticket.rejection_reason = resolution_note

        await self.db.commit()
        return ticket

    async def get_driver_leaderboard(self):
        """Aggregates ratings to show Admin how drivers are performing."""
        stmt = (
            select(
                schema.User.id,
                schema.DriverProfile.full_name,
                func.avg(schema.BookingRating.driver_rating).label("avg_rating"),
                func.count(schema.BookingRating.id).label("total_reviews"),
            )
            .join(schema.DriverProfile, schema.User.id == schema.DriverProfile.user_id)
            .join(
                schema.BookingRating,
                schema.User.id == schema.BookingRating.driver_user_id,
            )
            .group_by(schema.User.id, schema.DriverProfile.full_name)
            .order_by(desc("avg_rating"))
        )
        result = await self.db.execute(stmt)
        return result.all()
