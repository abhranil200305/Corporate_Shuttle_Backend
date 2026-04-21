from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_active_user
from app.db.database import get_async_session
from app.db.schema import BookingPaymentStatus, BookingStatus, User
from app.notifications.hub import WSHub
from app.passenger.schemas import (
    BookingCreateResponse,
    BookingDetailResponse,
    BookingListResponse,
    BookingMutationResponse,
    BookingQRResponse,
    BookingRatingMutationResponse,
    BookingRatingResponse,
    CreateBookingRatingRequest,
    CreateBookingRequest,
    CurrentTripBookingListResponse,
    CurrentTripLiveLocationResponse,
    CurrentTripStatusResponse,
    FarePreviewRequest,
    FarePreviewResponse,
    LegAvailableSeatsRequest,
    LegAvailableSeatsResponse,
    PassengerInvoiceResponse,
    PassengerProfileMutationResponse,
    PassengerProfileResponse,
    PassengerProfileUpsertRequest,
    PassengerTransactionHistoryResponse,
    RouteListResponse,
    RouteResponse,
    ScheduledTripDriverVehicleInfoResponse,
    ScheduledTripListResponse,
    ScheduledTripResponse,
    SupportTicketCreateResponse,
    SupportTicketListResponse,
    SupportTicketResponse,
    VerifyBookingPaymentRequest,
)

from app.passenger.service import PassengerService

router = APIRouter(prefix="/passenger", tags=["passenger"])


def get_ws_hub(request: Request) -> WSHub:
    return request.app.state.ws_hub

async def get_passenger_service(
    request: Request,
    db: AsyncSession = Depends(get_async_session),
) -> PassengerService:
    return PassengerService(
        db,
        ws_hub=get_ws_hub(request),
    )

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

@router.post("/profile/picture", response_model=PassengerProfileMutationResponse)
async def upsert_profile_picture(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> PassengerProfileMutationResponse:
    return await service.upsert_profile_picture(current_user, file)

@router.get("/routes", response_model=RouteListResponse)
async def list_routes(
    active_only: bool = Query(default=True),
    has_ac: bool | None = Query(default=None),
    service: PassengerService = Depends(get_passenger_service),
) -> RouteListResponse:
    return await service.list_routes(
        active_only=active_only,
        has_ac=has_ac,
    )

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

@router.get(
    "/scheduled-trips/{trip_id}/driver-vehicle-info",
    response_model=ScheduledTripDriverVehicleInfoResponse,
)
async def get_scheduled_trip_driver_vehicle_info(
    trip_id: str,
    service: PassengerService = Depends(get_passenger_service),
) -> ScheduledTripDriverVehicleInfoResponse:
    return await service.get_scheduled_trip_driver_vehicle_info(trip_id)

@router.post("/fare/preview", response_model=FarePreviewResponse)
async def preview_fare(
    payload: FarePreviewRequest,
    service: PassengerService = Depends(get_passenger_service),
) -> FarePreviewResponse:
    return await service.preview_fare(payload)

@router.post(
    "/scheduled-trips/{trip_id}/available-seats",
    response_model=LegAvailableSeatsResponse,
)
async def get_leg_available_seats(
    trip_id: str,
    payload: LegAvailableSeatsRequest,
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> LegAvailableSeatsResponse:
    return await service.get_leg_available_seats(current_user, trip_id, payload)

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


@router.get("/bookings/current", response_model=CurrentTripBookingListResponse)
async def list_current_bookings(
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> CurrentTripBookingListResponse:
    return await service.list_current_bookings(current_user)


@router.get("/history", response_model=BookingListResponse)
async def list_history(
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> BookingListResponse:
    return await service.list_history(current_user)

@router.get("/transactions", response_model=PassengerTransactionHistoryResponse)
async def list_transaction_history(
    status: BookingPaymentStatus | None = Query(default=None),
    month: int | None = Query(default=None, ge=1, le=12),
    year: int | None = Query(default=None, ge=2000, le=2100),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> PassengerTransactionHistoryResponse:
    return await service.list_transaction_history(
        current_user,
        status=status,
        month=month,
        year=year,
        limit=limit,
        offset=offset,
    )


@router.get("/bookings/{booking_id}", response_model=BookingDetailResponse)
async def get_booking_detail(
    booking_id: str,
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> BookingDetailResponse:
    return await service.get_booking_detail(current_user, booking_id)


@router.get("/bookings/{booking_id}/invoice", response_model=PassengerInvoiceResponse)
async def get_booking_invoice(
    booking_id: str,
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> PassengerInvoiceResponse:
    return await service.get_booking_invoice(current_user, booking_id)


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

@router.get("/bookings/{booking_id}/current-status", response_model=CurrentTripStatusResponse)
async def get_current_trip_status(
    booking_id: str,
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> CurrentTripStatusResponse:
    return await service.get_current_trip_status(current_user, booking_id)

@router.get(
    "/bookings/{booking_id}/live-location",
    response_model=CurrentTripLiveLocationResponse,
)
async def get_booking_live_location(
    booking_id: str,
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> CurrentTripLiveLocationResponse:
    return await service.get_booking_live_location(current_user, booking_id)

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

@router.post("/support", response_model=SupportTicketCreateResponse)
async def create_support_ticket(
    subject: str = Form(...),
    description: str = Form(...),
    file: UploadFile | None = File(default=None),
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> SupportTicketCreateResponse:
    return await service.create_support_ticket(
        current_user,
        subject=subject,
        description=description,
        file=file,
    )

@router.get("/support", response_model=SupportTicketListResponse)
async def list_support_tickets(
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> SupportTicketListResponse:
    return await service.list_support_tickets(current_user)


@router.get("/support/{ticket_id}", response_model=SupportTicketResponse)
async def get_support_ticket(
    ticket_id: str,
    current_user: User = Depends(get_current_active_user),
    service: PassengerService = Depends(get_passenger_service),
) -> SupportTicketResponse:
    return await service.get_support_ticket(current_user, ticket_id)