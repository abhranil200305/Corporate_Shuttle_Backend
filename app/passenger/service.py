from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.schema import (
    BookingPayment,
    BookingPaymentStatus,
    BookingRating,
    BookingStatus,
    PassengerProfile,
    Route,
    RouteFare,
    RouteStop,
    ScheduledTrip,
    ScheduledTripStatus,
    Stop,
    TripBooking,
    User,
    UserRole,
)
from app.passenger.schemas import (
    CreateBookingRatingRequest,
    CreateBookingRequest,
    FarePreviewRequest,
    PassengerProfileUpsertRequest,
    VerifyBookingPaymentRequest,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PassengerService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # role guard
    # ------------------------------------------------------------------
    @staticmethod
    def ensure_passenger(user: User) -> None:
        if user.role != UserRole.PASSENGER:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "passenger_only",
                    "message": "This endpoint is only available to passengers.",
                },
            )

    # ------------------------------------------------------------------
    # generic helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_name(value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_full_name",
                    "message": "Full name cannot be empty.",
                },
            )
        return cleaned
    
    @staticmethod
    def _get_profile_picture_upload_dir() -> Path:
        upload_dir = Path("/uploads/passenger/profilepictures")
        upload_dir.mkdir(parents=True, exist_ok=True)
        return upload_dir

    @staticmethod
    def _guess_profile_picture_extension(
        filename: str | None,
        content_type: str | None,
    ) -> str:
        suffix = Path(filename or "").suffix.lower().strip()
        if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            return ".jpg" if suffix == ".jpeg" else suffix

        mime_to_ext = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }
        return mime_to_ext.get((content_type or "").lower(), ".jpg")


    @staticmethod
    def _quantize_money(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @classmethod
    def _to_subunits(cls, amount: Decimal) -> int:
        return int((cls._quantize_money(amount) * 100).to_integral_value(rounding=ROUND_HALF_UP))

    async def _get_profile_obj(self, user_id: str) -> PassengerProfile | None:
        stmt = select(PassengerProfile).where(PassengerProfile.user_id == user_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_route_obj(self, route_id: str) -> Route:
        stmt = (
            select(Route)
            .where(Route.id == route_id)
            .options(
                selectinload(Route.route_stops).selectinload(RouteStop.stop),
            )
        )
        result = await self.db.execute(stmt)
        route = result.scalar_one_or_none()
        if route is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "route_not_found", "message": "Route not found."},
            )
        return route

    async def _get_trip_obj(self, trip_id: str) -> ScheduledTrip:
        stmt = (
            select(ScheduledTrip)
            .where(ScheduledTrip.id == trip_id)
            .options(
                selectinload(ScheduledTrip.route)
                .selectinload(Route.route_stops)
                .selectinload(RouteStop.stop),
                selectinload(ScheduledTrip.vehicle),
                selectinload(ScheduledTrip.driver),
            )
        )
        result = await self.db.execute(stmt)
        trip = result.scalar_one_or_none()
        if trip is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "scheduled_trip_not_found", "message": "Scheduled trip not found."},
            )
        return trip

    async def _get_booking_obj(
        self,
        *,
        booking_id: str,
        passenger_user_id: str,
    ) -> TripBooking:
        stmt = (
            select(TripBooking)
            .where(
                TripBooking.id == booking_id,
                TripBooking.passenger_user_id == passenger_user_id,
            )
            .options(
                selectinload(TripBooking.pickup_stop),
                selectinload(TripBooking.dropoff_stop),
                selectinload(TripBooking.payments),
                selectinload(TripBooking.rating),
                selectinload(TripBooking.scheduled_trip)
                .selectinload(ScheduledTrip.route)
                .selectinload(Route.route_stops)
                .selectinload(RouteStop.stop),
                selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.vehicle),
                selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.driver),
            )
        )
        result = await self.db.execute(stmt)
        booking = result.scalar_one_or_none()
        if booking is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "booking_not_found", "message": "Booking not found."},
            )
        return booking

    async def _get_route_sequence_map(self, route_id: str) -> dict[str, RouteStop]:
        stmt = select(RouteStop).where(RouteStop.route_id == route_id)
        result = await self.db.execute(stmt)
        rows = result.scalars().all()
        return {row.stop_id: row for row in rows}

    async def _resolve_fare(
        self,
        *,
        route_id: str,
        pickup_stop_id: str,
        dropoff_stop_id: str,
    ) -> tuple[RouteFare, RouteStop, RouteStop]:
        sequence_map = await self._get_route_sequence_map(route_id)
        pickup_route_stop = sequence_map.get(pickup_stop_id)
        dropoff_route_stop = sequence_map.get(dropoff_stop_id)

        if pickup_route_stop is None or dropoff_route_stop is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "stops_not_on_route",
                    "message": "Pickup and dropoff stops must both belong to the route.",
                },
            )

        if pickup_stop_id == dropoff_stop_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "same_pickup_dropoff",
                    "message": "Pickup and dropoff stops must be different.",
                },
            )

        if pickup_route_stop.sequence_no >= dropoff_route_stop.sequence_no:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_stop_order",
                    "message": "Pickup stop must come before dropoff stop on the route.",
                },
            )

        if not pickup_route_stop.boarding_allowed:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "boarding_not_allowed",
                    "message": "Boarding is not allowed at the selected pickup stop.",
                },
            )

        if not dropoff_route_stop.deboarding_allowed:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "deboarding_not_allowed",
                    "message": "Deboarding is not allowed at the selected dropoff stop.",
                },
            )

        fare_stmt = select(RouteFare).where(
            RouteFare.route_id == route_id,
            RouteFare.pickup_stop_id == pickup_stop_id,
            RouteFare.dropoff_stop_id == dropoff_stop_id,
            RouteFare.is_active.is_(True),
        )
        fare_result = await self.db.execute(fare_stmt)
        fare = fare_result.scalar_one_or_none()

        if fare is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "fare_not_found",
                    "message": "No active fare exists for the selected route segment.",
                },
            )

        return fare, pickup_route_stop, dropoff_route_stop

    async def _count_active_trip_bookings(self, scheduled_trip_id: str) -> int:
        active_statuses = (
            BookingStatus.PENDING_PAYMENT,
            BookingStatus.BOOKED,
            BookingStatus.BOARDED,
        )
        stmt = select(func.count(TripBooking.id)).where(
            TripBooking.scheduled_trip_id == scheduled_trip_id,
            TripBooking.booking_status.in_(active_statuses),
        )
        result = await self.db.execute(stmt)
        return int(result.scalar_one() or 0)

    def _serialize_stop_brief(self, stop: Stop) -> dict[str, Any]:
        return {
            "id": stop.id,
            "name": stop.name,
            "lat": stop.lat,
            "lng": stop.lng,
            "radius_meters": stop.radius_meters,
            "is_active": stop.is_active,
        }

    def _serialize_route(self, route: Route) -> dict[str, Any]:
        return {
            "id": route.id,
            "name": route.name,
            "code": route.code,
            "is_active": route.is_active,
            "stops": [
                {
                    "route_stop_id": route_stop.id,
                    "sequence_no": route_stop.sequence_no,
                    "boarding_allowed": route_stop.boarding_allowed,
                    "deboarding_allowed": route_stop.deboarding_allowed,
                    "stop": self._serialize_stop_brief(route_stop.stop),
                }
                for route_stop in sorted(route.route_stops, key=lambda item: item.sequence_no)
            ],
        }

    async def _serialize_trip(self, trip: ScheduledTrip) -> dict[str, Any]:
        seat_count = trip.vehicle.seat_count if trip.vehicle is not None else 0
        booked_count = await self._count_active_trip_bookings(trip.id)
        available_seats = max(seat_count - booked_count, 0)

        return {
            "id": trip.id,
            "route_id": trip.route_id,
            "driver_user_id": trip.driver_user_id,
            "vehicle_id": trip.vehicle_id,
            "planned_start_at": trip.planned_start_at,
            "planned_end_at": trip.planned_end_at,
            "actual_start_at": trip.actual_start_at,
            "actual_end_at": trip.actual_end_at,
            "status": trip.status,
            "admin_note": trip.admin_note,
            "available_seats": available_seats,
            "route": self._serialize_route(trip.route),
            "vehicle": None if trip.vehicle is None else {
                "id": trip.vehicle.id,
                "registration_number": trip.vehicle.registration_number,
                "vehicle_name": trip.vehicle.vehicle_name,
                "vehicle_model": trip.vehicle.vehicle_model,
                "color": trip.vehicle.color,
                "seat_count": trip.vehicle.seat_count,
                "has_ac": trip.vehicle.has_ac,
            },
            "driver": {
                "id": trip.driver.id,
                "email": trip.driver.email,
            } if trip.driver is not None else None,
        }

    def _serialize_payment(self, payment: BookingPayment) -> dict[str, Any]:
        return {
            "id": payment.id,
            "booking_id": payment.booking_id,
            "razorpay_order_id": payment.razorpay_order_id,
            "razorpay_payment_id": payment.razorpay_payment_id,
            "status": payment.status,
            "amount": payment.amount,
            "created_at": payment.created_at,
            "updated_at": payment.updated_at,
        }

    def _serialize_rating(self, rating: BookingRating | None) -> dict[str, Any] | None:
        if rating is None:
            return None
        return {
            "id": rating.id,
            "booking_id": rating.booking_id,
            "trip_rating": rating.trip_rating,
            "driver_rating": rating.driver_rating,
            "review_text": rating.review_text,
            "created_at": rating.created_at,
            "updated_at": rating.updated_at,
        }

    def _serialize_booking(self, booking: TripBooking) -> dict[str, Any]:
        return {
            "id": booking.id,
            "passenger_user_id": booking.passenger_user_id,
            "scheduled_trip_id": booking.scheduled_trip_id,
            "route_id": booking.route_id,
            "pickup_stop_id": booking.pickup_stop_id,
            "dropoff_stop_id": booking.dropoff_stop_id,
            "booking_status": booking.booking_status,
            "fare_amount": booking.fare_amount,
            "commission_percent_snapshot": booking.commission_percent_snapshot,
            "commission_amount": booking.commission_amount,
            "driver_payout_amount": booking.driver_payout_amount,
            "transfer_status": booking.transfer_status,
            "transfer_ready_at": booking.transfer_ready_at,
            "transfer_processed_at": booking.transfer_processed_at,
            "boarded_at": booking.boarded_at,
            "completed_at": booking.completed_at,
            "cancelled_at": booking.cancelled_at,
            "pickup_stop": self._serialize_stop_brief(booking.pickup_stop),
            "dropoff_stop": self._serialize_stop_brief(booking.dropoff_stop),
            "payments": [self._serialize_payment(payment) for payment in booking.payments],
            "rating": self._serialize_rating(booking.rating),
            "created_at": booking.created_at,
            "updated_at": booking.updated_at,
        }

    # ------------------------------------------------------------------
    # profile
    # ------------------------------------------------------------------
    async def get_profile(self, current_user: User) -> dict[str, Any]:
        self.ensure_passenger(current_user)
        profile = await self._get_profile_obj(current_user.id)
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "profile_not_found", "message": "Passenger profile not found."},
            )

        return {
            "id": profile.id,
            "user_id": profile.user_id,
            "full_name": profile.full_name,
            "profile_picture_path": profile.profile_picture_path,
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
        }

    async def create_profile(
        self,
        current_user: User,
        payload: PassengerProfileUpsertRequest,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        existing = await self._get_profile_obj(current_user.id)
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "profile_already_exists",
                    "message": "Passenger profile already exists.",
                },
            )

        profile = PassengerProfile(
            user_id=current_user.id,
            full_name=self._clean_name(payload.full_name),
            profile_picture_path=payload.profile_picture_path,
        )
        self.db.add(profile)
        await self.db.commit()
        await self.db.refresh(profile)

        return {
            "message": "Passenger profile created successfully.",
            "profile": {
                "id": profile.id,
                "user_id": profile.user_id,
                "full_name": profile.full_name,
                "profile_picture_path": profile.profile_picture_path,
                "created_at": profile.created_at,
                "updated_at": profile.updated_at,
            },
        }
    
    async def upsert_profile_picture(
        self,
        current_user: User,
        file: UploadFile,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        profile = await self._get_profile_obj(current_user.id)
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "profile_not_found",
                    "message": "Passenger profile not found. Create profile first.",
                },
            )

        if file is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "file_required",
                    "message": "Profile picture file is required.",
                },
            )

        content_type = (file.content_type or "").lower().strip()
        allowed_content_types = {
            "image/jpeg",
            "image/png",
            "image/webp",
            "image/gif",
        }
        if content_type not in allowed_content_types:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_file_type",
                    "message": "Only JPG, PNG, WEBP, and GIF images are allowed.",
                },
            )

        try:
            content = await file.read()
        finally:
            await file.close()

        if not content:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "empty_file",
                    "message": "Uploaded file is empty.",
                },
            )

        upload_dir = self._get_profile_picture_upload_dir()
        extension = self._guess_profile_picture_extension(
            file.filename,
            file.content_type,
        )
        filename = f"passenger_profile_{current_user.id}_{uuid4().hex}{extension}"
        destination = upload_dir / filename
        destination.write_bytes(content)

        old_path = (profile.profile_picture_path or "").strip()
        if old_path and old_path != str(destination):
            try:
                old_file = Path(old_path)
                if old_file.is_file():
                    old_file.unlink()
            except Exception:
                pass

        profile.profile_picture_path = str(destination)
        self.db.add(profile)
        await self.db.commit()
        await self.db.refresh(profile)

        return {
            "message": "Passenger profile picture uploaded successfully.",
            "profile": {
                "id": profile.id,
                "user_id": profile.user_id,
                "full_name": profile.full_name,
                "profile_picture_path": profile.profile_picture_path,
                "created_at": profile.created_at,
                "updated_at": profile.updated_at,
            },
        }

    async def patch_profile(
        self,
        current_user: User,
        payload: PassengerProfileUpsertRequest,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)
        profile = await self._get_profile_obj(current_user.id)
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "profile_not_found", "message": "Passenger profile not found."},
            )

        profile.full_name = self._clean_name(payload.full_name)
        profile.profile_picture_path = payload.profile_picture_path

        self.db.add(profile)
        await self.db.commit()
        await self.db.refresh(profile)

        return {
            "message": "Passenger profile updated successfully.",
            "profile": {
                "id": profile.id,
                "user_id": profile.user_id,
                "full_name": profile.full_name,
                "profile_picture_path": profile.profile_picture_path,
                "created_at": profile.created_at,
                "updated_at": profile.updated_at,
            },
        }

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------
    async def list_routes(self, *, active_only: bool = True) -> dict[str, Any]:
        stmt = (
            select(Route)
            .options(selectinload(Route.route_stops).selectinload(RouteStop.stop))
            .order_by(Route.name.asc())
        )
        if active_only:
            stmt = stmt.where(Route.is_active.is_(True))

        result = await self.db.execute(stmt)
        routes = result.scalars().unique().all()

        return {
            "items": [self._serialize_route(route) for route in routes],
            "count": len(routes),
        }

    async def get_route_detail(self, route_id: str) -> dict[str, Any]:
        route = await self._get_route_obj(route_id)
        return self._serialize_route(route)

    async def list_scheduled_trips(
        self,
        *,
        route_id: str | None = None,
        only_future: bool = True,
    ) -> dict[str, Any]:
        stmt = (
            select(ScheduledTrip)
            .options(
                selectinload(ScheduledTrip.route)
                .selectinload(Route.route_stops)
                .selectinload(RouteStop.stop),
                selectinload(ScheduledTrip.vehicle),
                selectinload(ScheduledTrip.driver),
            )
            .order_by(ScheduledTrip.planned_start_at.asc())
        )

        if route_id:
            stmt = stmt.where(ScheduledTrip.route_id == route_id)

        if only_future:
            stmt = stmt.where(
                ScheduledTrip.planned_start_at >= utcnow(),
                ScheduledTrip.status == ScheduledTripStatus.SCHEDULED,
            )

        result = await self.db.execute(stmt)
        trips = result.scalars().unique().all()

        items = []
        for trip in trips:
            items.append(await self._serialize_trip(trip))

        return {"items": items, "count": len(items)}

    async def get_scheduled_trip_detail(self, trip_id: str) -> dict[str, Any]:
        trip = await self._get_trip_obj(trip_id)
        return await self._serialize_trip(trip)

    async def preview_fare(self, payload: FarePreviewRequest) -> dict[str, Any]:
        route = await self._get_route_obj(payload.route_id)
        fare, pickup_route_stop, dropoff_route_stop = await self._resolve_fare(
            route_id=payload.route_id,
            pickup_stop_id=payload.pickup_stop_id,
            dropoff_stop_id=payload.dropoff_stop_id,
        )

        pickup_stop = next(
            rs.stop for rs in route.route_stops if rs.stop_id == payload.pickup_stop_id
        )
        dropoff_stop = next(
            rs.stop for rs in route.route_stops if rs.stop_id == payload.dropoff_stop_id
        )

        return {
            "route_id": route.id,
            "route_name": route.name,
            "route_code": route.code,
            "pickup_stop": self._serialize_stop_brief(pickup_stop),
            "dropoff_stop": self._serialize_stop_brief(dropoff_stop),
            "pickup_sequence_no": pickup_route_stop.sequence_no,
            "dropoff_sequence_no": dropoff_route_stop.sequence_no,
            "amount": fare.amount,
        }

    # ------------------------------------------------------------------
    # razorpay helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _get_razorpay_key_id() -> str:
        value = os.getenv("RAZORPAY_KEY_ID", "").strip()
        if not value:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "missing_razorpay_key_id",
                    "message": "RAZORPAY_KEY_ID is not configured.",
                },
            )
        return value

    @staticmethod
    def _get_razorpay_key_secret() -> str:
        value = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
        if not value:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "missing_razorpay_key_secret",
                    "message": "RAZORPAY_KEY_SECRET is not configured.",
                },
            )
        return value

    @staticmethod
    def _get_razorpay_base_url() -> str:
        return os.getenv("RAZORPAY_BASE_URL", "https://api.razorpay.com/v1").strip().rstrip("/")

    async def _razorpay_request(
        self,
        *,
        method: str,
        path: str,
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        key_id = self._get_razorpay_key_id()
        key_secret = self._get_razorpay_key_secret()
        base_url = self._get_razorpay_base_url()

        url = f"{base_url}{path}"

        async with httpx.AsyncClient(auth=(key_id, key_secret), timeout=20.0) as client:
            response = await client.request(method=method, url=url, json=json_payload)

        if response.status_code >= 400:
            try:
                error_payload = response.json()
            except Exception:
                error_payload = {"raw": response.text}

            raise HTTPException(
                status_code=502,
                detail={
                    "error": "razorpay_request_failed",
                    "message": "Razorpay request failed.",
                    "provider_status_code": response.status_code,
                    "provider_response": error_payload,
                },
            )

        return response.json()

    async def _create_razorpay_order(
        self,
        *,
        booking: TripBooking,
        amount: Decimal,
    ) -> dict[str, Any]:
        amount_subunits = self._to_subunits(amount)
        receipt = f"booking_{booking.id.replace('-', '')[:24]}"

        payload = {
            "amount": amount_subunits,
            "currency": "INR",
            "receipt": receipt,
            "notes": {
                "booking_id": booking.id,
                "scheduled_trip_id": booking.scheduled_trip_id,
                "passenger_user_id": booking.passenger_user_id,
            },
        }
        return await self._razorpay_request(method="POST", path="/orders", json_payload=payload)

    async def _fetch_razorpay_payment(self, payment_id: str) -> dict[str, Any]:
        return await self._razorpay_request(method="GET", path=f"/payments/{payment_id}")

    async def _capture_razorpay_payment(self, payment_id: str, amount_subunits: int) -> dict[str, Any]:
        payload = {
            "amount": amount_subunits,
            "currency": "INR",
        }
        return await self._razorpay_request(
            method="POST",
            path=f"/payments/{payment_id}/capture",
            json_payload=payload,
        )

    def _verify_razorpay_signature(
        self,
        *,
        order_id: str,
        payment_id: str,
        received_signature: str,
    ) -> None:
        secret = self._get_razorpay_key_secret()
        message = f"{order_id}|{payment_id}".encode("utf-8")
        generated_signature = hmac.new(
            secret.encode("utf-8"),
            message,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(generated_signature, received_signature):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_payment_signature",
                    "message": "Razorpay payment signature verification failed.",
                },
            )

    # ------------------------------------------------------------------
    # bookings
    # ------------------------------------------------------------------
    async def create_booking(
        self,
        current_user: User,
        payload: CreateBookingRequest,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        profile = await self._get_profile_obj(current_user.id)
        if profile is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "profile_required",
                    "message": "Create passenger profile before booking a trip.",
                },
            )

        trip = await self._get_trip_obj(payload.scheduled_trip_id)

        if trip.status != ScheduledTripStatus.SCHEDULED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "trip_not_bookable",
                    "message": "This scheduled trip is not open for booking.",
                },
            )

        if trip.planned_start_at <= utcnow():
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "trip_already_started",
                    "message": "This scheduled trip can no longer be booked.",
                },
            )

        existing_stmt = select(TripBooking).where(
            TripBooking.passenger_user_id == current_user.id,
            TripBooking.scheduled_trip_id == trip.id,
        )
        existing_result = await self.db.execute(existing_stmt)
        existing_booking = existing_result.scalar_one_or_none()
        if existing_booking is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "duplicate_booking",
                    "message": "Passenger already has a booking for this scheduled trip.",
                },
            )

        active_booking_count = await self._count_active_trip_bookings(trip.id)
        seat_count = trip.vehicle.seat_count if trip.vehicle is not None else 0
        if active_booking_count >= seat_count:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "trip_full",
                    "message": "No seats are currently available for this scheduled trip.",
                },
            )

        fare, _, _ = await self._resolve_fare(
            route_id=trip.route_id,
            pickup_stop_id=payload.pickup_stop_id,
            dropoff_stop_id=payload.dropoff_stop_id,
        )

        booking = TripBooking(
            passenger_user_id=current_user.id,
            scheduled_trip_id=trip.id,
            route_id=trip.route_id,
            pickup_stop_id=payload.pickup_stop_id,
            dropoff_stop_id=payload.dropoff_stop_id,
            booking_status=BookingStatus.PENDING_PAYMENT,
            fare_amount=self._quantize_money(fare.amount),
        )
        self.db.add(booking)
        await self.db.flush()

        order_payload = await self._create_razorpay_order(
            booking=booking,
            amount=booking.fare_amount,
        )

        payment = BookingPayment(
            booking_id=booking.id,
            razorpay_order_id=order_payload["id"],
            amount=booking.fare_amount,
            status=BookingPaymentStatus.CREATED,
        )
        self.db.add(payment)

        await self.db.commit()

        booking = await self._get_booking_obj(
            booking_id=booking.id,
            passenger_user_id=current_user.id,
        )

        return {
            "message": "Booking created. Payment is pending.",
            "booking": self._serialize_booking(booking),
            "payment_order": {
                "provider": "razorpay",
                "razorpay_key_id": self._get_razorpay_key_id(),
                "razorpay_order_id": order_payload["id"],
                "amount": booking.fare_amount,
                "amount_subunits": self._to_subunits(booking.fare_amount),
                "currency": order_payload.get("currency", "INR"),
                "receipt": order_payload.get("receipt"),
            },
        }

    async def verify_booking_payment(
        self,
        current_user: User,
        booking_id: str,
        payload: VerifyBookingPaymentRequest,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        booking = await self._get_booking_obj(
            booking_id=booking_id,
            passenger_user_id=current_user.id,
        )

        if booking.booking_status != BookingStatus.PENDING_PAYMENT:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "booking_not_pending_payment",
                    "message": "This booking is not awaiting payment verification.",
                },
            )

        payment = next(
            (item for item in booking.payments if item.razorpay_order_id == payload.razorpay_order_id),
            None,
        )
        if payment is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "payment_order_not_found",
                    "message": "Payment order was not found for this booking.",
                },
            )

        self._verify_razorpay_signature(
            order_id=payload.razorpay_order_id,
            payment_id=payload.razorpay_payment_id,
            received_signature=payload.razorpay_signature,
        )

        fetched_payment = await self._fetch_razorpay_payment(payload.razorpay_payment_id)
        fetched_order_id = fetched_payment.get("order_id")
        fetched_status = str(fetched_payment.get("status", "")).lower()
        fetched_captured = bool(fetched_payment.get("captured", False))
        fetched_amount = int(fetched_payment.get("amount", 0))

        expected_amount_subunits = self._to_subunits(payment.amount)

        if fetched_order_id != payment.razorpay_order_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "payment_order_mismatch",
                    "message": "Fetched payment order does not match stored payment order.",
                },
            )

        if fetched_amount != expected_amount_subunits:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "payment_amount_mismatch",
                    "message": "Fetched payment amount does not match booking amount.",
                },
            )

        if fetched_status == "authorized" and not fetched_captured:
            captured_payment = await self._capture_razorpay_payment(
                payload.razorpay_payment_id,
                expected_amount_subunits,
            )
            fetched_status = str(captured_payment.get("status", "")).lower()
            fetched_captured = bool(captured_payment.get("captured", False))

        if fetched_status != "captured" or not fetched_captured:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "payment_not_captured",
                    "message": "Payment is not in captured state yet.",
                    "provider_status": fetched_status,
                },
            )

        payment.razorpay_payment_id = payload.razorpay_payment_id
        payment.razorpay_signature = payload.razorpay_signature
        payment.status = BookingPaymentStatus.PAID

        booking.booking_status = BookingStatus.BOOKED

        self.db.add(payment)
        self.db.add(booking)
        await self.db.commit()

        booking = await self._get_booking_obj(
            booking_id=booking.id,
            passenger_user_id=current_user.id,
        )

        return {
            "message": "Payment verified successfully.",
            "booking": self._serialize_booking(booking),
        }

    async def list_bookings(
        self,
        current_user: User,
        *,
        status: BookingStatus | None = None,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        stmt = (
            select(TripBooking)
            .where(TripBooking.passenger_user_id == current_user.id)
            .options(
                selectinload(TripBooking.pickup_stop),
                selectinload(TripBooking.dropoff_stop),
                selectinload(TripBooking.payments),
                selectinload(TripBooking.rating),
                selectinload(TripBooking.scheduled_trip)
                .selectinload(ScheduledTrip.route)
                .selectinload(Route.route_stops)
                .selectinload(RouteStop.stop),
                selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.vehicle),
                selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.driver),
            )
            .order_by(TripBooking.created_at.desc())
        )

        if status is not None:
            stmt = stmt.where(TripBooking.booking_status == status)

        result = await self.db.execute(stmt)
        bookings = result.scalars().unique().all()

        return {
            "items": [self._serialize_booking(booking) for booking in bookings],
            "count": len(bookings),
        }

    async def get_booking_detail(self, current_user: User, booking_id: str) -> dict[str, Any]:
        self.ensure_passenger(current_user)
        booking = await self._get_booking_obj(
            booking_id=booking_id,
            passenger_user_id=current_user.id,
        )
        return self._serialize_booking(booking)

    async def cancel_booking(self, current_user: User, booking_id: str) -> dict[str, Any]:
        self.ensure_passenger(current_user)
        booking = await self._get_booking_obj(
            booking_id=booking_id,
            passenger_user_id=current_user.id,
        )

        if booking.booking_status != BookingStatus.PENDING_PAYMENT:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "cancel_not_allowed",
                    "message": "Only pending-payment bookings can be cancelled in the current implementation.",
                },
            )

        booking.booking_status = BookingStatus.CANCELLED
        booking.cancelled_at = utcnow()

        self.db.add(booking)
        await self.db.commit()

        booking = await self._get_booking_obj(
            booking_id=booking.id,
            passenger_user_id=current_user.id,
        )

        return {
            "message": "Booking cancelled successfully.",
            "booking": self._serialize_booking(booking),
        }

    async def list_upcoming_bookings(self, current_user: User) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        stmt = (
            select(TripBooking)
            .join(ScheduledTrip, ScheduledTrip.id == TripBooking.scheduled_trip_id)
            .where(
                TripBooking.passenger_user_id == current_user.id,
                TripBooking.booking_status == BookingStatus.BOOKED,
                ScheduledTrip.planned_start_at > utcnow(),
            )
            .options(
                selectinload(TripBooking.pickup_stop),
                selectinload(TripBooking.dropoff_stop),
                selectinload(TripBooking.payments),
                selectinload(TripBooking.rating),
                selectinload(TripBooking.scheduled_trip)
                .selectinload(ScheduledTrip.route)
                .selectinload(Route.route_stops)
                .selectinload(RouteStop.stop),
                selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.vehicle),
                selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.driver),
            )
            .order_by(ScheduledTrip.planned_start_at.asc())
        )
        result = await self.db.execute(stmt)
        bookings = result.scalars().unique().all()

        return {
            "items": [self._serialize_booking(booking) for booking in bookings],
            "count": len(bookings),
        }

    async def list_current_bookings(self, current_user: User) -> dict[str, Any]:
        self.ensure_passenger(current_user)
        now = utcnow()

        stmt = (
            select(TripBooking)
            .join(ScheduledTrip, ScheduledTrip.id == TripBooking.scheduled_trip_id)
            .where(
                TripBooking.passenger_user_id == current_user.id,
                TripBooking.booking_status.in_((BookingStatus.BOOKED, BookingStatus.BOARDED)),
                ScheduledTrip.planned_start_at <= now,
                ScheduledTrip.planned_end_at >= now,
            )
            .options(
                selectinload(TripBooking.pickup_stop),
                selectinload(TripBooking.dropoff_stop),
                selectinload(TripBooking.payments),
                selectinload(TripBooking.rating),
                selectinload(TripBooking.scheduled_trip)
                .selectinload(ScheduledTrip.route)
                .selectinload(Route.route_stops)
                .selectinload(RouteStop.stop),
                selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.vehicle),
                selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.driver),
            )
            .order_by(ScheduledTrip.planned_start_at.asc())
        )
        result = await self.db.execute(stmt)
        bookings = result.scalars().unique().all()

        return {
            "items": [self._serialize_booking(booking) for booking in bookings],
            "count": len(bookings),
        }

    async def list_history(self, current_user: User) -> dict[str, Any]:
        self.ensure_passenger(current_user)

        history_statuses = (
            BookingStatus.COMPLETED,
            BookingStatus.CANCELLED,
            BookingStatus.MISSED,
        )

        stmt = (
            select(TripBooking)
            .where(
                TripBooking.passenger_user_id == current_user.id,
                TripBooking.booking_status.in_(history_statuses),
            )
            .options(
                selectinload(TripBooking.pickup_stop),
                selectinload(TripBooking.dropoff_stop),
                selectinload(TripBooking.payments),
                selectinload(TripBooking.rating),
                selectinload(TripBooking.scheduled_trip)
                .selectinload(ScheduledTrip.route)
                .selectinload(Route.route_stops)
                .selectinload(RouteStop.stop),
                selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.vehicle),
                selectinload(TripBooking.scheduled_trip).selectinload(ScheduledTrip.driver),
            )
            .order_by(TripBooking.updated_at.desc())
        )
        result = await self.db.execute(stmt)
        bookings = result.scalars().unique().all()

        return {
            "items": [self._serialize_booking(booking) for booking in bookings],
            "count": len(bookings),
        }

    # ------------------------------------------------------------------
    # QR
    # ------------------------------------------------------------------
    @staticmethod
    def _get_qr_secret() -> str:
        value = os.getenv("PASSENGER_QR_SECRET", "").strip()
        if not value:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "missing_passenger_qr_secret",
                    "message": "PASSENGER_QR_SECRET is not configured.",
                },
            )
        return value

    def _build_qr_token(self, booking: TripBooking) -> tuple[str, dict[str, Any]]:
        payload = {
            "booking_id": booking.id,
            "scheduled_trip_id": booking.scheduled_trip_id,
            "passenger_user_id": booking.passenger_user_id,
            "pickup_stop_id": booking.pickup_stop_id,
            "dropoff_stop_id": booking.dropoff_stop_id,
            "booking_status": booking.booking_status.value,
            "issued_at": int(utcnow().timestamp()),
            "expires_at": int((utcnow() + timedelta(hours=12)).timestamp()),
        }
        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        payload_bytes = payload_json.encode("utf-8")

        signature = hmac.new(
            self._get_qr_secret().encode("utf-8"),
            payload_bytes,
            hashlib.sha256,
        ).hexdigest()

        encoded_payload = base64.urlsafe_b64encode(payload_bytes).decode("utf-8").rstrip("=")
        token = f"{encoded_payload}.{signature}"
        return token, payload

    async def get_booking_qr(self, current_user: User, booking_id: str) -> dict[str, Any]:
        self.ensure_passenger(current_user)
        booking = await self._get_booking_obj(
            booking_id=booking_id,
            passenger_user_id=current_user.id,
        )

        if booking.booking_status not in (BookingStatus.BOOKED, BookingStatus.BOARDED):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "qr_not_available",
                    "message": "QR is only available for active booked rides.",
                },
            )

        token, payload = self._build_qr_token(booking)
        return {
            "booking_id": booking.id,
            "qr_token": token,
            "payload": payload,
        }

    # ------------------------------------------------------------------
    # rating
    # ------------------------------------------------------------------
    async def create_rating(
        self,
        current_user: User,
        booking_id: str,
        payload: CreateBookingRatingRequest,
    ) -> dict[str, Any]:
        self.ensure_passenger(current_user)
        booking = await self._get_booking_obj(
            booking_id=booking_id,
            passenger_user_id=current_user.id,
        )

        if booking.booking_status != BookingStatus.COMPLETED:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rating_not_allowed",
                    "message": "Rating is allowed only after trip completion.",
                },
            )

        if booking.rating is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "rating_already_exists",
                    "message": "Rating already exists for this booking.",
                },
            )

        rating = BookingRating(
            booking_id=booking.id,
            passenger_user_id=current_user.id,
            driver_user_id=booking.scheduled_trip.driver_user_id,
            scheduled_trip_id=booking.scheduled_trip_id,
            trip_rating=payload.trip_rating,
            driver_rating=payload.driver_rating,
            review_text=payload.review_text.strip() if payload.review_text else None,
        )
        self.db.add(rating)
        await self.db.commit()
        await self.db.refresh(rating)

        return {
            "message": "Rating submitted successfully.",
            "rating": self._serialize_rating(rating),
        }

    async def get_rating(self, current_user: User, booking_id: str) -> dict[str, Any]:
        self.ensure_passenger(current_user)
        booking = await self._get_booking_obj(
            booking_id=booking_id,
            passenger_user_id=current_user.id,
        )

        if booking.rating is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "rating_not_found",
                    "message": "Rating not found for this booking.",
                },
            )

        return self._serialize_rating(booking.rating)