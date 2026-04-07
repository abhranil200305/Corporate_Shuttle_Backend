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
        stmt = (
            select(schema.User)
            .options(
                joinedload(schema.User.passenger_profile),
                # This part is the key:
                joinedload(schema.User.passenger_bookings).options(
                    joinedload(schema.TripBooking.pickup_stop),
                    joinedload(schema.TripBooking.dropoff_stop),
                ),
            )
            .where(schema.User.id == user_id, schema.User.role == "passenger")
        )

        result = await self.db.execute(stmt)
        return result.unique().scalar_one_or_none()

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

    async def get_all_reviews(self, min_rating: Optional[int] = None):
        stmt = (
            select(schema.BookingRating)
            .options(
                # Load the passenger's name
                joinedload(schema.BookingRating.passenger).joinedload(
                    schema.User.passenger_profile
                ),
                # Load the driver's name
                joinedload(schema.BookingRating.driver).joinedload(
                    schema.User.driver_profile
                ),
                # Load trip details
                joinedload(schema.BookingRating.scheduled_trip).joinedload(
                    schema.ScheduledTrip.route
                ),
            )
            .order_by(schema.BookingRating.created_at.desc())
        )

        if min_rating:
            stmt = stmt.where(schema.BookingRating.driver_rating <= min_rating)

        result = await self.db.execute(stmt)
        return result.scalars().all()

        # app/services/admin_service.py

    # app/services/admin_service.py

    async def fetch_user_bookings_with_details(self, user_id: str):

        stmt = (
            select(schema.TripBooking)
            .options(
                joinedload(schema.TripBooking.pickup_stop),
                joinedload(schema.TripBooking.dropoff_stop),
                joinedload(schema.TripBooking.scheduled_trip)
                .joinedload(schema.ScheduledTrip.driver)
                .joinedload(schema.User.driver_profile),
                joinedload(schema.TripBooking.payments),
                joinedload(schema.TripBooking.route),
            )
            .where(schema.TripBooking.passenger_user_id == user_id)
            .order_by(schema.TripBooking.created_at.desc())
        )

        result = await self.db.execute(stmt)
        return result.unique().scalars().all()

    async def fetch_detailed_transactions(
        self, skip: int, limit: int, status: str = None
    ):

        stmt = (
            select(schema.TripBooking)
            .options(
                joinedload(schema.TripBooking.passenger).joinedload(
                    schema.User.passenger_profile
                ),
                joinedload(schema.TripBooking.scheduled_trip)
                .joinedload(schema.ScheduledTrip.driver)
                .joinedload(schema.User.driver_profile),
                joinedload(schema.TripBooking.route),
                joinedload(schema.TripBooking.pickup_stop),
                joinedload(schema.TripBooking.dropoff_stop),
                joinedload(schema.TripBooking.payments),
                joinedload(schema.TripBooking.scan_events),
            )
            .order_by(schema.TripBooking.created_at.desc())
            .offset(skip)
            .limit(limit)
        )

        if status:
            stmt = stmt.where(schema.TripBooking.booking_status == status)

            result = await self.db.execute(stmt)
        return result.unique().scalars().all()


        result = await self.db.execute(stmt)
        return result.unique().scalars().all()

    # app/admin/logic/service.py

    async def fetch_detailed_transactions(
        self, skip: int, limit: int, status: str = None
    ):
        stmt = (
            select(schema.TripBooking)
            .options(
                joinedload(schema.TripBooking.passenger).joinedload(
                    schema.User.passenger_profile
                ),
                joinedload(schema.TripBooking.scheduled_trip)
                .joinedload(schema.ScheduledTrip.driver)
                .joinedload(schema.User.driver_profile),
                joinedload(schema.TripBooking.route),
                joinedload(schema.TripBooking.pickup_stop),
                joinedload(schema.TripBooking.dropoff_stop),
                joinedload(schema.TripBooking.payments),
                joinedload(schema.TripBooking.scan_events),
            )
            .order_by(schema.TripBooking.created_at.desc())
            .offset(skip)
            .limit(limit)
        )

        # 1. Apply filter only if status is provided
        if status:
            stmt = stmt.where(schema.TripBooking.booking_status == status)

        # 2. MOVE THESE OUTSIDE THE IF BLOCK
        result = await self.db.execute(stmt)
        return result.unique().scalars().all()

    # ============================================================
    # payout management, by Anubhab Dey
    # ============================================================

    async def _get_default_platform_settings(self):
        stmt = (
            select(schema.PlatformSettings)
            .where(schema.PlatformSettings.settings_key == "default")
            .limit(1)
        )
        result = await self.db.execute(stmt)
        settings = result.scalar_one_or_none()
        return settings

    def _booking_has_paid_payment(self, booking) -> bool:
        return any(
            payment.status == schema.BookingPaymentStatus.PAID
            and payment.razorpay_payment_id
            for payment in booking.payments
        )

    def _serialize_driver_payout_profile(self, driver, aggregates: dict | None = None):
        profile = driver.driver_profile
        vehicle = driver.vehicle
        payout = driver.payout_details
        agg = aggregates or {}

        return {
            "user_id": driver.id,
            "email": driver.email,
            "is_active": driver.is_active,
            "profile": {
                "full_name": profile.full_name if profile else None,
                "phone": profile.phone if profile else None,
                "verification_status": profile.verification_status if profile else None,
                "lifecycle_status": profile.lifecycle_status if profile else None,
            },
            "vehicle": {
                "vehicle_id": vehicle.id if vehicle else None,
                "registration_number": vehicle.registration_number if vehicle else None,
                "vehicle_name": vehicle.vehicle_name if vehicle else None,
                "vehicle_model": vehicle.vehicle_model if vehicle else None,
                "seat_count": vehicle.seat_count if vehicle else None,
                "has_ac": vehicle.has_ac if vehicle else None,
                "verification_status": vehicle.verification_status if vehicle else None,
            },
            "payout_details": None
            if payout is None
            else {
                "id": payout.id,
                "driver_user_id": payout.driver_user_id,
                "account_holder_name": payout.account_holder_name,
                "bank_account_number": payout.bank_account_number,
                "ifsc_code": payout.ifsc_code,
                "phone_number": payout.phone_number,
                "razorpay_linked_account_id": payout.razorpay_linked_account_id,
                "linked_account_status": payout.linked_account_status,
                "is_payout_eligible": payout.is_payout_eligible,
                "created_at": payout.created_at,
                "updated_at": payout.updated_at,
            },
            "aggregates": {
                "ready_booking_count": agg.get("ready_booking_count", 0),
                "ready_total_amount": agg.get("ready_total_amount", 0),
                "transferred_booking_count": agg.get("transferred_booking_count", 0),
                "transferred_total_amount": agg.get("transferred_total_amount", 0),
                "failed_booking_count": agg.get("failed_booking_count", 0),
                "failed_total_amount": agg.get("failed_total_amount", 0),
                "reversed_booking_count": agg.get("reversed_booking_count", 0),
                "reversed_total_amount": agg.get("reversed_total_amount", 0),
                "refund_queue_count": agg.get("refund_queue_count", 0),
                "refund_queue_total_amount": agg.get("refund_queue_total_amount", 0),
            },
        }

    def _build_driver_payout_aggregates(self, bookings):
        agg = {
            "ready_booking_count": 0,
            "ready_total_amount": 0,
            "transferred_booking_count": 0,
            "transferred_total_amount": 0,
            "failed_booking_count": 0,
            "failed_total_amount": 0,
            "reversed_booking_count": 0,
            "reversed_total_amount": 0,
            "refund_queue_count": 0,
            "refund_queue_total_amount": 0,
        }

        for booking in bookings:
            payout_amount = booking.driver_payout_amount or 0
            fare_amount = booking.fare_amount or 0

            if booking.transfer_status == schema.TransferStatus.READY:
                agg["ready_booking_count"] += 1
                agg["ready_total_amount"] += payout_amount

            elif booking.transfer_status == schema.TransferStatus.TRANSFERRED:
                agg["transferred_booking_count"] += 1
                agg["transferred_total_amount"] += payout_amount

            elif booking.transfer_status == schema.TransferStatus.FAILED:
                agg["failed_booking_count"] += 1
                agg["failed_total_amount"] += payout_amount

            elif booking.transfer_status == schema.TransferStatus.REVERSED:
                agg["reversed_booking_count"] += 1
                agg["reversed_total_amount"] += payout_amount

            if (
                booking.booking_status == schema.BookingStatus.CANCELLED
                and self._booking_has_paid_payment(booking)
            ):
                agg["refund_queue_count"] += 1
                agg["refund_queue_total_amount"] += fare_amount

        return agg

    def _serialize_payout_booking(self, booking):
        scheduled_trip = booking.scheduled_trip
        driver = scheduled_trip.driver if scheduled_trip else None
        passenger = booking.passenger

        driver_profile = (
            driver.driver_profile if driver and driver.driver_profile else None
        )
        passenger_profile = (
            passenger.passenger_profile
            if passenger and passenger.passenger_profile
            else None
        )

        return {
            "booking_id": booking.id,
            "scheduled_trip_id": booking.scheduled_trip_id,
            "route_id": booking.route_id,
            "driver_user_id": scheduled_trip.driver_user_id if scheduled_trip else None,
            "driver_name": driver_profile.full_name if driver_profile else None,
            "passenger_user_id": booking.passenger_user_id,
            "passenger_name": passenger_profile.full_name
            if passenger_profile
            else None,
            "booking_status": booking.booking_status,
            "fare_amount": booking.fare_amount,
            "commission_percent_snapshot": booking.commission_percent_snapshot,
            "commission_amount": booking.commission_amount,
            "driver_payout_amount": booking.driver_payout_amount,
            "transfer_status": booking.transfer_status,
            "transfer_ready_at": booking.transfer_ready_at,
            "transfer_processed_at": booking.transfer_processed_at,
            "payment_hold_expires_at": booking.payment_hold_expires_at,
            "boarded_at": booking.boarded_at,
            "completed_at": booking.completed_at,
            "cancelled_at": booking.cancelled_at,
            "refund_retry_after": booking.refund_retry_after,
            "refund_attempt_count": booking.refund_attempt_count,
            "pickup_stop_id": booking.pickup_stop_id,
            "dropoff_stop_id": booking.dropoff_stop_id,
            "created_at": booking.created_at,
            "updated_at": booking.updated_at,
        }

    def _serialize_booking_transfer(self, transfer):
        booking = transfer.booking
        scheduled_trip = booking.scheduled_trip if booking else None

        return {
            "transfer_id": transfer.id,
            "booking_id": transfer.booking_id,
            "driver_user_id": transfer.driver_user_id,
            "scheduled_trip_id": booking.scheduled_trip_id if booking else None,
            "source_booking_payment_id": transfer.source_booking_payment_id,
            "linked_account_id": transfer.linked_account_id,
            "razorpay_transfer_id": transfer.razorpay_transfer_id,
            "amount": transfer.amount,
            "status": transfer.status,
            "failure_reason": transfer.failure_reason,
            "processed_at": transfer.processed_at,
            "reversed_at": transfer.reversed_at,
            "created_at": transfer.created_at,
            "updated_at": transfer.updated_at,
            "booking_transfer_status": booking.transfer_status if booking else None,
            "booking_status": booking.booking_status if booking else None,
            "completed_at": booking.completed_at if booking else None,
            "cancelled_at": booking.cancelled_at if booking else None,
            "trip_driver_user_id": scheduled_trip.driver_user_id
            if scheduled_trip
            else None,
        }

    async def get_payout_settings(self):
        settings = await self._get_default_platform_settings()
        if settings is None:
            return {
                "settings_key": "default",
                "commission_percent": 0,
                "created_at": None,
                "updated_at": None,
            }

        return {
            "settings_key": settings.settings_key,
            "commission_percent": settings.commission_percent,
            "created_at": settings.created_at,
            "updated_at": settings.updated_at,
        }

    async def update_payout_settings(self, commission_percent):
        settings = await self._get_default_platform_settings()

        if settings is None:
            settings = schema.PlatformSettings(
                settings_key="default",
                commission_percent=commission_percent,
            )
            self.db.add(settings)
        else:
            settings.commission_percent = commission_percent

        await self.db.commit()
        await self.db.refresh(settings)

        return {
            "message": "Payout settings updated successfully.",
            "settings": {
                "settings_key": settings.settings_key,
                "commission_percent": settings.commission_percent,
                "created_at": settings.created_at,
                "updated_at": settings.updated_at,
            },
        }

    async def list_driver_payout_profiles(
        self,
        linked_account_status: Optional[schema.LinkedAccountStatus] = None,
        is_payout_eligible: Optional[bool] = None,
    ):
        drivers = await self.fetch_detailed_drivers()

        filtered_drivers = []
        for driver in drivers:
            payout = driver.payout_details

            current_linked_status = (
                payout.linked_account_status
                if payout is not None
                else schema.LinkedAccountStatus.NOT_CREATED
            )
            current_eligible = (
                payout.is_payout_eligible if payout is not None else False
            )

            if (
                linked_account_status is not None
                and current_linked_status != linked_account_status
            ):
                continue

            if (
                is_payout_eligible is not None
                and current_eligible != is_payout_eligible
            ):
                continue

            filtered_drivers.append(driver)

        driver_ids = [driver.id for driver in filtered_drivers]
        booking_map = {driver_id: [] for driver_id in driver_ids}

        if driver_ids:
            stmt = (
                select(schema.TripBooking)
                .join(
                    schema.ScheduledTrip,
                    schema.ScheduledTrip.id == schema.TripBooking.scheduled_trip_id,
                )
                .where(schema.ScheduledTrip.driver_user_id.in_(driver_ids))
                .options(
                    joinedload(schema.TripBooking.scheduled_trip),
                    joinedload(schema.TripBooking.payments),
                )
            )
            result = await self.db.execute(stmt)
            bookings = result.unique().scalars().all()

            for booking in bookings:
                driver_user_id = booking.scheduled_trip.driver_user_id
                booking_map.setdefault(driver_user_id, []).append(booking)

        items = []
        for driver in filtered_drivers:
            aggregates = self._build_driver_payout_aggregates(
                booking_map.get(driver.id, [])
            )
            items.append(self._serialize_driver_payout_profile(driver, aggregates))

        return {
            "items": items,
            "count": len(items),
        }

    async def get_driver_payout_profile(self, driver_user_id: str):
        driver = await self.fetch_driver_by_id(driver_user_id)
        if not driver:
            raise HTTPException(status_code=404, detail="Driver not found")

        stmt = (
            select(schema.TripBooking)
            .join(
                schema.ScheduledTrip,
                schema.ScheduledTrip.id == schema.TripBooking.scheduled_trip_id,
            )
            .where(schema.ScheduledTrip.driver_user_id == driver_user_id)
            .options(
                joinedload(schema.TripBooking.scheduled_trip),
                joinedload(schema.TripBooking.payments),
            )
        )
        result = await self.db.execute(stmt)
        bookings = result.unique().scalars().all()

        return self._serialize_driver_payout_profile(
            driver,
            self._build_driver_payout_aggregates(bookings),
        )

    async def upsert_driver_payout_details(
        self,
        driver_user_id: str,
        payload,
    ):
        driver = await self.fetch_driver_by_id(driver_user_id)
        if not driver:
            raise HTTPException(status_code=404, detail="Driver not found")

        payout = driver.payout_details
        if payout is None:
            payout = schema.DriverPayoutDetails(
                driver_user_id=driver_user_id,
                account_holder_name=payload.account_holder_name.strip(),
                bank_account_number=payload.bank_account_number.strip(),
                ifsc_code=payload.ifsc_code.strip(),
                phone_number=payload.phone_number.strip(),
            )
            self.db.add(payout)
        else:
            payout.account_holder_name = payload.account_holder_name.strip()
            payout.bank_account_number = payload.bank_account_number.strip()
            payout.ifsc_code = payload.ifsc_code.strip()
            payout.phone_number = payload.phone_number.strip()

        await self.db.commit()
        await self.db.refresh(payout)

        refreshed_driver = await self.fetch_driver_by_id(driver_user_id)
        return {
            "message": "Driver payout details saved successfully.",
            "driver": self._serialize_driver_payout_profile(refreshed_driver),
        }

    async def update_driver_linked_account(
        self,
        driver_user_id: str,
        payload,
    ):
        driver = await self.fetch_driver_by_id(driver_user_id)
        if not driver:
            raise HTTPException(status_code=404, detail="Driver not found")

        payout = driver.payout_details
        if payout is None:
            raise HTTPException(
                status_code=404,
                detail="Driver payout details not found. Create payout details first.",
            )

        payout.razorpay_linked_account_id = payload.razorpay_linked_account_id
        payout.linked_account_status = payload.linked_account_status

        self.db.add(payout)
        await self.db.commit()
        await self.db.refresh(payout)

        refreshed_driver = await self.fetch_driver_by_id(driver_user_id)
        return {
            "message": "Driver linked account updated successfully.",
            "driver": self._serialize_driver_payout_profile(refreshed_driver),
        }

    async def update_driver_payout_eligibility(
        self,
        driver_user_id: str,
        payload,
    ):
        driver = await self.fetch_driver_by_id(driver_user_id)
        if not driver:
            raise HTTPException(status_code=404, detail="Driver not found")

        payout = driver.payout_details
        if payout is None:
            raise HTTPException(
                status_code=404,
                detail="Driver payout details not found. Create payout details first.",
            )

        payout.is_payout_eligible = payload.is_payout_eligible
        self.db.add(payout)

        await self.db.commit()
        await self.db.refresh(payout)

        refreshed_driver = await self.fetch_driver_by_id(driver_user_id)
        return {
            "message": "Driver payout eligibility updated successfully.",
            "driver": self._serialize_driver_payout_profile(refreshed_driver),
        }

    async def list_payout_bookings(
        self,
        driver_user_id: Optional[str] = None,
        passenger_user_id: Optional[str] = None,
        booking_status: Optional[schema.BookingStatus] = None,
        transfer_status: Optional[schema.TransferStatus] = None,
        month: Optional[int] = None,
        year: Optional[int] = None,
    ):
        stmt = (
            select(schema.TripBooking)
            .join(
                schema.ScheduledTrip,
                schema.ScheduledTrip.id == schema.TripBooking.scheduled_trip_id,
            )
            .options(
                joinedload(schema.TripBooking.transfer),
                joinedload(schema.TripBooking.payments),
                joinedload(schema.TripBooking.passenger).joinedload(
                    schema.User.passenger_profile
                ),
                joinedload(schema.TripBooking.scheduled_trip)
                .joinedload(schema.ScheduledTrip.driver)
                .joinedload(schema.User.driver_profile),
            )
            .order_by(schema.TripBooking.created_at.desc())
        )

        if driver_user_id:
            stmt = stmt.where(schema.ScheduledTrip.driver_user_id == driver_user_id)

        if passenger_user_id:
            stmt = stmt.where(schema.TripBooking.passenger_user_id == passenger_user_id)

        if booking_status is not None:
            stmt = stmt.where(schema.TripBooking.booking_status == booking_status)

        if transfer_status is not None:
            stmt = stmt.where(schema.TripBooking.transfer_status == transfer_status)

        if month is not None:
            stmt = stmt.where(
                func.extract("month", schema.TripBooking.completed_at) == month
            )

        if year is not None:
            stmt = stmt.where(
                func.extract("year", schema.TripBooking.completed_at) == year
            )

        result = await self.db.execute(stmt)
        bookings = result.unique().scalars().all()

        return {
            "items": [self._serialize_payout_booking(booking) for booking in bookings],
            "count": len(bookings),
        }

    async def get_payout_booking_detail(self, booking_id: str):
        stmt = (
            select(schema.TripBooking)
            .where(schema.TripBooking.id == booking_id)
            .options(
                joinedload(schema.TripBooking.transfer),
                joinedload(schema.TripBooking.payments),
                joinedload(schema.TripBooking.pickup_stop),
                joinedload(schema.TripBooking.dropoff_stop),
                joinedload(schema.TripBooking.passenger).joinedload(
                    schema.User.passenger_profile
                ),
                joinedload(schema.TripBooking.scheduled_trip)
                .joinedload(schema.ScheduledTrip.driver)
                .joinedload(schema.User.driver_profile),
                joinedload(schema.TripBooking.scheduled_trip).joinedload(
                    schema.ScheduledTrip.route
                ),
            )
        )
        result = await self.db.execute(stmt)
        booking = result.unique().scalar_one_or_none()

        if booking is None:
            raise HTTPException(status_code=404, detail="Booking not found")

        return {
            "booking": self._serialize_payout_booking(booking),
            "pickup_stop": None
            if booking.pickup_stop is None
            else {
                "id": booking.pickup_stop.id,
                "name": booking.pickup_stop.name,
                "lat": booking.pickup_stop.lat,
                "lng": booking.pickup_stop.lng,
                "radius_meters": booking.pickup_stop.radius_meters,
            },
            "dropoff_stop": None
            if booking.dropoff_stop is None
            else {
                "id": booking.dropoff_stop.id,
                "name": booking.dropoff_stop.name,
                "lat": booking.dropoff_stop.lat,
                "lng": booking.dropoff_stop.lng,
                "radius_meters": booking.dropoff_stop.radius_meters,
            },
            "payments": [
                {
                    "id": payment.id,
                    "booking_id": payment.booking_id,
                    "razorpay_order_id": payment.razorpay_order_id,
                    "razorpay_payment_id": payment.razorpay_payment_id,
                    "amount": payment.amount,
                    "status": payment.status,
                    "created_at": payment.created_at,
                    "updated_at": payment.updated_at,
                }
                for payment in booking.payments
            ],
            "transfer": None
            if booking.transfer is None
            else self._serialize_booking_transfer(booking.transfer),
        }

    async def list_booking_transfers(
        self,
        driver_user_id: Optional[str] = None,
        status: Optional[schema.BookingTransferStatus] = None,
        month: Optional[int] = None,
        year: Optional[int] = None,
    ):
        stmt = (
            select(schema.BookingTransfer)
            .options(
                joinedload(schema.BookingTransfer.booking).joinedload(
                    schema.TripBooking.scheduled_trip
                ),
                joinedload(schema.BookingTransfer.booking).joinedload(
                    schema.TripBooking.payments
                ),
            )
            .order_by(schema.BookingTransfer.created_at.desc())
        )

        if driver_user_id:
            stmt = stmt.where(schema.BookingTransfer.driver_user_id == driver_user_id)

        if status is not None:
            stmt = stmt.where(schema.BookingTransfer.status == status)

        if month is not None:
            stmt = stmt.where(
                func.extract("month", schema.BookingTransfer.created_at) == month
            )

        if year is not None:
            stmt = stmt.where(
                func.extract("year", schema.BookingTransfer.created_at) == year
            )

        result = await self.db.execute(stmt)
        transfers = result.unique().scalars().all()

        return {
            "items": [
                self._serialize_booking_transfer(transfer) for transfer in transfers
            ],
            "count": len(transfers),
        }

    async def get_booking_transfer_detail(self, transfer_id: str):
        stmt = (
            select(schema.BookingTransfer)
            .where(schema.BookingTransfer.id == transfer_id)
            .options(
                joinedload(schema.BookingTransfer.booking).joinedload(
                    schema.TripBooking.scheduled_trip
                ),
                joinedload(schema.BookingTransfer.booking).joinedload(
                    schema.TripBooking.payments
                ),
                joinedload(schema.BookingTransfer.source_booking_payment),
            )
        )
        result = await self.db.execute(stmt)
        transfer = result.unique().scalar_one_or_none()

        if transfer is None:
            raise HTTPException(status_code=404, detail="Transfer not found")

        return {
            "transfer": self._serialize_booking_transfer(transfer),
            "source_payment": None
            if transfer.source_booking_payment is None
            else {
                "id": transfer.source_booking_payment.id,
                "booking_id": transfer.source_booking_payment.booking_id,
                "razorpay_order_id": transfer.source_booking_payment.razorpay_order_id,
                "razorpay_payment_id": transfer.source_booking_payment.razorpay_payment_id,
                "amount": transfer.source_booking_payment.amount,
                "status": transfer.source_booking_payment.status,
                "created_at": transfer.source_booking_payment.created_at,
                "updated_at": transfer.source_booking_payment.updated_at,
            },
        }

    async def trigger_booking_payout(
        self,
        booking_id: str,
        linked_account_id: Optional[str] = None,
        require_completed: bool = True,
    ):
        payout_service = RoutePayoutService(self.db)
        result = await payout_service.trigger_transfer_for_booking(
            booking_id=booking_id,
            linked_account_id=linked_account_id,
            require_completed=require_completed,
        )
        return {
            "message": "Booking payout trigger completed.",
            "result": result,
        }

    async def trigger_driver_monthly_payouts(
        self,
        driver_user_id: str,
        month: int,
        year: int,
        linked_account_id: Optional[str] = None,
    ):
        driver = await self.fetch_driver_by_id(driver_user_id)
        if not driver:
            raise HTTPException(status_code=404, detail="Driver not found")

        stmt = (
            select(schema.TripBooking)
            .join(
                schema.ScheduledTrip,
                schema.ScheduledTrip.id == schema.TripBooking.scheduled_trip_id,
            )
            .where(
                schema.ScheduledTrip.driver_user_id == driver_user_id,
                schema.TripBooking.booking_status == schema.BookingStatus.COMPLETED,
                func.extract("month", schema.TripBooking.completed_at) == month,
                func.extract("year", schema.TripBooking.completed_at) == year,
                schema.TripBooking.transfer_status.in_(
                    [
                        schema.TransferStatus.NOT_READY,
                        schema.TransferStatus.READY,
                        schema.TransferStatus.FAILED,
                    ]
                ),
            )
            .order_by(schema.TripBooking.completed_at.asc())
        )
        result = await self.db.execute(stmt)
        bookings = result.scalars().all()

        payout_service = RoutePayoutService(self.db)

        results = []
        success_count = 0
        failure_count = 0

        for booking in bookings:
            try:
                payout_result = await payout_service.trigger_transfer_for_booking(
                    booking_id=booking.id,
                    linked_account_id=linked_account_id,
                    require_completed=True,
                )
                results.append(
                    {
                        "booking_id": booking.id,
                        "status": "success",
                        "result": payout_result,
                    }
                )
                success_count += 1
            except Exception as exc:
                results.append(
                    {
                        "booking_id": booking.id,
                        "status": "failed",
                        "error": str(exc),
                    }
                )
                failure_count += 1

        return {
            "message": "Driver monthly payout batch completed.",
            "total_selected": len(bookings),
            "success_count": success_count,
            "failure_count": failure_count,
            "results": results,
        }

    async def trigger_bulk_payouts(
        self,
        payload,
    ):
        selected_bookings = []

        if payload.booking_ids:
            stmt = (
                select(schema.TripBooking)
                .where(schema.TripBooking.id.in_(payload.booking_ids))
                .order_by(schema.TripBooking.created_at.asc())
            )
            result = await self.db.execute(stmt)
            selected_bookings = result.scalars().all()
        else:
            stmt = (
                select(schema.TripBooking)
                .join(
                    schema.ScheduledTrip,
                    schema.ScheduledTrip.id == schema.TripBooking.scheduled_trip_id,
                )
                .where(
                    schema.TripBooking.booking_status == schema.BookingStatus.COMPLETED,
                )
                .order_by(schema.TripBooking.completed_at.asc())
                .limit(payload.limit)
            )

            if payload.driver_user_id:
                stmt = stmt.where(
                    schema.ScheduledTrip.driver_user_id == payload.driver_user_id
                )

            if payload.month is not None:
                stmt = stmt.where(
                    func.extract("month", schema.TripBooking.completed_at)
                    == payload.month
                )

            if payload.year is not None:
                stmt = stmt.where(
                    func.extract("year", schema.TripBooking.completed_at)
                    == payload.year
                )

            if payload.only_ready:
                stmt = stmt.where(
                    schema.TripBooking.transfer_status == schema.TransferStatus.READY
                )
            else:
                stmt = stmt.where(
                    schema.TripBooking.transfer_status.in_(
                        [
                            schema.TransferStatus.NOT_READY,
                            schema.TransferStatus.READY,
                            schema.TransferStatus.FAILED,
                        ]
                    )
                )

            result = await self.db.execute(stmt)
            selected_bookings = result.scalars().all()

        payout_service = RoutePayoutService(self.db)

        results = []
        success_count = 0
        failure_count = 0

        for booking in selected_bookings:
            try:
                payout_result = await payout_service.trigger_transfer_for_booking(
                    booking_id=booking.id,
                    linked_account_id=payload.linked_account_id,
                    require_completed=payload.require_completed,
                )
                results.append(
                    {
                        "booking_id": booking.id,
                        "status": "success",
                        "result": payout_result,
                    }
                )
                success_count += 1
            except Exception as exc:
                results.append(
                    {
                        "booking_id": booking.id,
                        "status": "failed",
                        "error": str(exc),
                    }
                )
                failure_count += 1

        return {
            "message": "Bulk payout trigger completed.",
            "total_selected": len(selected_bookings),
            "success_count": success_count,
            "failure_count": failure_count,
            "results": results,
        }

    async def list_refund_queue(self):
        stmt = (
            select(schema.TripBooking)
            .where(schema.TripBooking.booking_status == schema.BookingStatus.CANCELLED)
            .options(
                joinedload(schema.TripBooking.payments),
                joinedload(schema.TripBooking.transfer),
                joinedload(schema.TripBooking.scheduled_trip),
            )
            .order_by(
                schema.TripBooking.refund_retry_after.asc(),
                schema.TripBooking.created_at.asc(),
            )
        )
        result = await self.db.execute(stmt)
        bookings = result.unique().scalars().all()

        queued = []
        for booking in bookings:
            if not self._booking_has_paid_payment(booking):
                continue

            queued.append(
                {
                    "booking_id": booking.id,
                    "scheduled_trip_id": booking.scheduled_trip_id,
                    "passenger_user_id": booking.passenger_user_id,
                    "driver_user_id": (
                        booking.scheduled_trip.driver_user_id
                        if booking.scheduled_trip
                        else None
                    ),
                    "fare_amount": booking.fare_amount,
                    "transfer_status": booking.transfer_status,
                    "refund_retry_after": booking.refund_retry_after,
                    "refund_attempt_count": booking.refund_attempt_count,
                    "cancelled_at": booking.cancelled_at,
                    "created_at": booking.created_at,
                    "updated_at": booking.updated_at,
                }
            )

        return {
            "items": queued,
            "count": len(queued),
        }

    async def reconcile_cancelled_booking_refund(self, booking_id: str):
        stmt = (
            select(schema.TripBooking)
            .where(schema.TripBooking.id == booking_id)
            .options(
                joinedload(schema.TripBooking.payments),
                joinedload(schema.TripBooking.transfer),
                joinedload(schema.TripBooking.scheduled_trip),
            )
        )
        result = await self.db.execute(stmt)
        booking = result.unique().scalar_one_or_none()

        if booking is None:
            raise HTTPException(status_code=404, detail="Booking not found")

        payout_service = RoutePayoutService(self.db)
        outcome = await payout_service.reconcile_cancelled_booking_refund(booking)
        await self.db.commit()

        refreshed = await self.get_payout_booking_detail(booking_id)
        return {
            "message": "Cancelled booking refund reconciliation completed.",
            "outcome": outcome,
            "booking": refreshed["booking"],
            "transfer": refreshed["transfer"],
            "payments": refreshed["payments"],
        }

    async def get_payout_dashboard(self):
        settings = await self._get_default_platform_settings()

        booking_stmt = (
            select(schema.TripBooking)
            .options(joinedload(schema.TripBooking.payments))
            .order_by(schema.TripBooking.created_at.desc())
        )
        booking_result = await self.db.execute(booking_stmt)
        bookings = booking_result.unique().scalars().all()

        ready_booking_count = 0
        ready_total_amount = 0

        transferred_booking_count = 0
        transferred_total_amount = 0

        failed_booking_count = 0
        failed_total_amount = 0

        reversed_booking_count = 0
        reversed_total_amount = 0

        refund_queue_count = 0
        refund_queue_total_amount = 0

        for booking in bookings:
            payout_amount = booking.driver_payout_amount or 0
            fare_amount = booking.fare_amount or 0

            if booking.transfer_status == schema.TransferStatus.READY:
                ready_booking_count += 1
                ready_total_amount += payout_amount

            elif booking.transfer_status == schema.TransferStatus.TRANSFERRED:
                transferred_booking_count += 1
                transferred_total_amount += payout_amount

            elif booking.transfer_status == schema.TransferStatus.FAILED:
                failed_booking_count += 1
                failed_total_amount += payout_amount

            elif booking.transfer_status == schema.TransferStatus.REVERSED:
                reversed_booking_count += 1
                reversed_total_amount += payout_amount

            if (
                booking.booking_status == schema.BookingStatus.CANCELLED
                and self._booking_has_paid_payment(booking)
            ):
                refund_queue_count += 1
                refund_queue_total_amount += fare_amount

        drivers = await self.fetch_detailed_drivers()
        drivers_missing_linked_account_count = 0
        drivers_not_eligible_count = 0

        for driver in drivers:
            payout = driver.payout_details

            if payout is None or not payout.razorpay_linked_account_id:
                drivers_missing_linked_account_count += 1

            if payout is None or not payout.is_payout_eligible:
                drivers_not_eligible_count += 1

        return {
            "commission_percent": settings.commission_percent if settings else 0,
            "ready_booking_count": ready_booking_count,
            "ready_total_amount": ready_total_amount,
            "transferred_booking_count": transferred_booking_count,
            "transferred_total_amount": transferred_total_amount,
            "failed_booking_count": failed_booking_count,
            "failed_total_amount": failed_total_amount,
            "reversed_booking_count": reversed_booking_count,
            "reversed_total_amount": reversed_total_amount,
            "refund_queue_count": refund_queue_count,
            "refund_queue_total_amount": refund_queue_total_amount,
            "drivers_missing_linked_account_count": drivers_missing_linked_account_count,
            "drivers_not_eligible_count": drivers_not_eligible_count,
        }

    def _map_provider_linked_account_status(
        self,
        provider_status: str | None,
    ) -> schema.LinkedAccountStatus:
        normalized = (provider_status or "").strip().lower()

        if normalized in {"active", "activated"}:
            return schema.LinkedAccountStatus.ACTIVE

        if normalized in {"blocked", "suspended"}:
            return schema.LinkedAccountStatus.BLOCKED

        if normalized in {"deleted", "closed"}:
            return schema.LinkedAccountStatus.DELETED

        return schema.LinkedAccountStatus.NOT_CREATED

    async def create_and_save_driver_linked_account(
        self,
        driver_user_id: str,
    ):
        driver = await self.fetch_driver_by_id(driver_user_id)
        if not driver or not driver.driver_profile:
            raise HTTPException(status_code=404, detail="Driver profile not found")

        profile = driver.driver_profile
        payout = driver.payout_details

        if payout is not None and payout.razorpay_linked_account_id:
            return {
                "message": "Linked account already exists for this driver.",
                "driver": self._serialize_driver_payout_profile(driver),
                "razorpay_account_id": payout.razorpay_linked_account_id,
            }

        if payout is None:
            missing_fields = []

            if not (profile.bank_account_number or "").strip():
                missing_fields.append("bank_account_number")

            if not (profile.ifsc_code or "").strip():
                missing_fields.append("ifsc_code")

            if missing_fields:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "driver_payout_details_required",
                        "message": "Driver payout details are missing. Save payout details first or ensure bank details exist on the driver profile.",
                        "missing_fields": missing_fields,
                    },
                )

            payout = schema.DriverPayoutDetails(
                driver_user_id=driver_user_id,
                account_holder_name=(profile.full_name or "").strip(),
                bank_account_number=(profile.bank_account_number or "").strip(),
                ifsc_code=(profile.ifsc_code or "").strip(),
                phone_number=(profile.phone or "").strip(),
                linked_account_status=schema.LinkedAccountStatus.NOT_CREATED,
                is_payout_eligible=False,
            )
            self.db.add(payout)
            await self.db.flush()

        payout_service = RoutePayoutService(self.db)
        provider_account = await payout_service.create_linked_account(
            email=driver.email,
            phone=profile.phone,
            full_name=profile.full_name,
        )

        provider_account_id = str(provider_account.get("id") or "").strip()
        if not provider_account_id:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "invalid_linked_account_response",
                    "message": "Provider account creation response did not contain an account id.",
                },
            )

        provider_status = str(provider_account.get("status") or "").strip()

        payout.razorpay_linked_account_id = provider_account_id
        payout.linked_account_status = self._map_provider_linked_account_status(
            provider_status
        )

        self.db.add(payout)
        await self.db.commit()
        await self.db.refresh(payout)

        refreshed_driver = await self.fetch_driver_by_id(driver_user_id)

        return {
            "message": "Driver linked account created and saved successfully.",
            "driver": self._serialize_driver_payout_profile(refreshed_driver),
            "provider_account": provider_account,
        }

    async def sync_driver_linked_account(
        self,
        driver_user_id: str,
    ):
        driver = await self.fetch_driver_by_id(driver_user_id)
        if not driver:
            raise HTTPException(status_code=404, detail="Driver not found")

        payout = driver.payout_details
        if payout is None or not (payout.razorpay_linked_account_id or "").strip():
            raise HTTPException(
                status_code=404,
                detail="Driver linked account is not available locally.",
            )

        payout_service = RoutePayoutService(self.db)
        provider_account = await payout_service.fetch_linked_account(
            payout.razorpay_linked_account_id
        )

        provider_status = str(provider_account.get("status") or "").strip()
        payout.linked_account_status = self._map_provider_linked_account_status(
            provider_status
        )

        self.db.add(payout)
        await self.db.commit()
        await self.db.refresh(payout)

        refreshed_driver = await self.fetch_driver_by_id(driver_user_id)

        return {
            "message": "Driver linked account synced successfully.",
            "driver": self._serialize_driver_payout_profile(refreshed_driver),
            "provider_account": provider_account,
        }

    async def get_driver_linked_account_provider_detail(
        self,
        driver_user_id: str,
    ):
        driver = await self.fetch_driver_by_id(driver_user_id)
        if not driver:
            raise HTTPException(status_code=404, detail="Driver not found")

        payout = driver.payout_details
        if payout is None or not (payout.razorpay_linked_account_id or "").strip():
            raise HTTPException(
                status_code=404,
                detail="Driver linked account is not available locally.",
            )

        payout_service = RoutePayoutService(self.db)
        provider_account = await payout_service.fetch_linked_account(
            payout.razorpay_linked_account_id
        )

        return {
            "driver_user_id": driver_user_id,
            "razorpay_linked_account_id": payout.razorpay_linked_account_id,
            "linked_account_status": payout.linked_account_status,
            "provider_account": provider_account,
        }
