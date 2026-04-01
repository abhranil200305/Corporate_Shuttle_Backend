from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_active_user
from app.db.database import get_async_session
from app.db.schema import BookingStatus, User
from app.passenger.schemas import (
    BookingCreateResponse,
    BookingListResponse,
    BookingMutationResponse,
    BookingQRResponse,
    BookingRatingMutationResponse,
    BookingRatingResponse,
    CreateBookingRatingRequest,
    CreateBookingRequest,
    FarePreviewRequest,
    FarePreviewResponse,
    PassengerProfileMutationResponse,
    PassengerProfileResponse,
    PassengerProfileUpsertRequest,
    RouteListResponse,
    RouteResponse,
    ScheduledTripListResponse,
    ScheduledTripResponse,
    VerifyBookingPaymentRequest,
    TripBookingResponse,
)
from app.passenger.service import PassengerService

router = APIRouter(prefix="/passenger", tags=["passenger"])


async def get_passenger_service(
    db: AsyncSession = Depends(get_async_session),
) -> PassengerService:
    return PassengerService(db)


@router.post("/profile", response_model=PassengerProfileMutationResponse)
async def create_profile(
    payload: PassengerProfileUpsertRequest,
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> PassengerProfileMutationResponse:
    return await service.create_profile(current_user, payload)


@router.get("/profile", response_model=PassengerProfileResponse)
async def get_profile(
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> PassengerProfileResponse:
    return await service.get_profile(current_user)


@router.patch("/profile", response_model=PassengerProfileMutationResponse)
async def patch_profile(
    payload: PassengerProfileUpsertRequest,
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> PassengerProfileMutationResponse:
    return await service.patch_profile(current_user, payload)


@router.get("/routes", response_model=RouteListResponse)
async def list_routes(
    active_only: bool = Query(default=True),
    service: PassengerService = Depends(get_passenger_service),
) -> RouteListResponse:
    return await service.list_routes(active_only=active_only)


@router.get("/routes/{route_id}", response_model=RouteResponse)
async def get_route_detail(
    route_id: str,
    service: PassengerService = Depends(get_passenger_service),
) -> RouteResponse:
    return await service.get_route_detail(route_id)


@router.get("/scheduled-trips", response_model=ScheduledTripListResponse)
async def list_scheduled_trips(
    route_id: str | None = Query(default=None),
    only_future: bool = Query(default=True),
    service: PassengerService = Depends(get_passenger_service),
) -> ScheduledTripListResponse:
    return await service.list_scheduled_trips(
        route_id=route_id,
        only_future=only_future,
    )


@router.get("/scheduled-trips/{trip_id}", response_model=ScheduledTripResponse)
async def get_scheduled_trip_detail(
    trip_id: str,
    service: PassengerService = Depends(get_passenger_service),
) -> ScheduledTripResponse:
    return await service.get_scheduled_trip_detail(trip_id)


@router.post("/fare/preview", response_model=FarePreviewResponse)
async def preview_fare(
    payload: FarePreviewRequest,
    service: PassengerService = Depends(get_passenger_service),
) -> FarePreviewResponse:
    return await service.preview_fare(payload)


@router.post("/bookings", response_model=BookingCreateResponse)
async def create_booking(
    payload: CreateBookingRequest,
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> BookingCreateResponse:
    return await service.create_booking(current_user, payload)


@router.post("/bookings/{booking_id}/verify-payment", response_model=BookingMutationResponse)
async def verify_booking_payment(
    booking_id: str,
    payload: VerifyBookingPaymentRequest,
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> BookingMutationResponse:
    return await service.verify_booking_payment(current_user, booking_id, payload)


@router.get("/bookings", response_model=BookingListResponse)
async def list_bookings(
    status: BookingStatus | None = Query(default=None),
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> BookingListResponse:
    return await service.list_bookings(current_user, status=status)


@router.get("/bookings/upcoming", response_model=BookingListResponse)
async def list_upcoming_bookings(
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> BookingListResponse:
    return await service.list_upcoming_bookings(current_user)


@router.get("/bookings/current", response_model=BookingListResponse)
async def list_current_bookings(
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> BookingListResponse:
    return await service.list_current_bookings(current_user)


@router.get("/history", response_model=BookingListResponse)
async def list_history(
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> BookingListResponse:
    return await service.list_history(current_user)


@router.get("/bookings/{booking_id}", response_model=TripBookingResponse)
async def get_booking_detail(
    booking_id: str,
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> TripBookingResponse:
    return await service.get_booking_detail(current_user, booking_id)


@router.post("/bookings/{booking_id}/cancel", response_model=BookingMutationResponse)
async def cancel_booking(
    booking_id: str,
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> BookingMutationResponse:
    return await service.cancel_booking(current_user, booking_id)


@router.get("/bookings/{booking_id}/qr", response_model=BookingQRResponse)
async def get_booking_qr(
    booking_id: str,
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> BookingQRResponse:
    return await service.get_booking_qr(current_user, booking_id)


@router.post("/bookings/{booking_id}/rating", response_model=BookingRatingMutationResponse)
async def create_rating(
    booking_id: str,
    payload: CreateBookingRatingRequest,
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> BookingRatingMutationResponse:
    return await service.create_rating(current_user, booking_id, payload)


@router.get("/bookings/{booking_id}/rating", response_model=BookingRatingResponse)
async def get_rating(
    booking_id: str,
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> BookingRatingResponse:
    return await service.get_rating(current_user, booking_id)