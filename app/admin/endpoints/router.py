from fastapi import (
	APIRouter,
	Body,
	Depends,
	File,
	HTTPException,
	Path,
	Query,
	Request,
	UploadFile,
)
from sqlalchemy import func, not_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, joinedload
from app.admin.logic.service import AdminService
from app.admin.rfid_service import AdminRFIDService
from app.admin.rfid_schemas import (
	RFIDPayoutOperationsSummaryResponse,
	RFIDRideMoneyDetailResponse,
	RFIDPayoutTransferDetailResponse,
	RFIDPayoutTransferReversalListResponse,
	RFIDPayoutTransferReversalRequest,
	RFIDRideDeductionReversalRequest,
	RFIDGenericMutationResponse,
	RFIDPayoutTransferReconcileCreatedRequest,
	RFIDPayoutTransferRefreshWithheldRequest,
	RFIDPayoutTransferBulkTriggerRequest,
	RFIDTripRideListResponse,
	RFIDPayoutTransferListResponse,
	RFIDCardAssignRequest,
	RFIDCardBulkRegisterRequest,
	RFIDCardBulkRegisterResponse,
	RFIDCardDetailResponse,
	RFIDCardListResponse,
	RFIDCardMutationResponse,
	RFIDCardRegisterRequest,
	RFIDCardUnassignRequest,
	RFIDDeviceCreateRequest,
	RFIDDeviceListResponse,
	RFIDDeviceMutationResponse,
	RFIDDeviceUpdateRequest,
	RFIDRechargeCreateRequest,
	RFIDRechargeMutationResponse,
	RFIDLedgerEntryListResponse,
	RFIDRechargeListResponse,
	RFIDCardBlockRequest,
	RFIDCardDecommissionRequest,
	RFIDCardReturnRequest,
	AdminRFIDSeatPolicyResponse,
    AdminRFIDSeatPolicyUpdateRequest,
	RFIDDeviceVehicleOptionListResponse,
	RFIDCardOptionListResponse,
)
from app.admin.structs.dto import (
	BookingFullDetailsResponsee,
	BulkPayoutTriggerRequest,
	BulkStopAddRequest,
	CommercialRuleCreateRequest,
	CommercialRuleStatusUpdateRequest,
	CommercialRuleUpdateRequest,
	DriverLinkedAccountUpdate,
	DriverPayoutDetailsUpsert,
	DriverPayoutEligibilityUpdate,
	PayoutAdjustmentCreateRequest,
	PayoutAdjustmentDecisionRequest,
	PayoutDashboardResponse,
	PayoutSettingsUpdate,
	RouteCreate,
	RouteFareCreate,
	RouteStatusUpdate,
	StopCreate,
	TriggerBookingPayoutRequest,
	TriggerDriverMonthlyPayoutRequest,
	TripManifestResponse,
	VehicleInspectionUpdate,
	VehicleVerificationUpdate,
	VerificationUpdate,
	AdminVehicleInspectionStatusListResponse,
)
from app.auth.schemas import (
    AdminDeviceUserListResponse,
    AdminUserDeviceListResponse,
    DriverDeviceSettingsResponse,
    DriverDeviceSettingsUpdateRequest,
    MessageResponse,
)
from app.auth.service import AuthService
from app.auth.exceptions import AuthError
from app.auth.dependencies import (
	get_auth_service,
	get_current_active_user,
	get_current_admin,
	get_current_user,
	to_http_exception,
)
from app.db import schema
from app.db.database import get_async_session
from app.notifications import hub
from app.notifications.hub import WSHub
from app.notifications.schemas import NotificationDataPayload
from app.notifications.service import NotificationService
from app.payments.service import RoutePayoutService

# Create ONE router for all admin tasks
router = APIRouter(
	prefix="/admin",
	tags=["Admin Management"],
	dependencies=[Depends(get_current_admin)],  # Protects all routes
)

###----Notifications --------


def get_ws_hub(request: Request) -> WSHub:
	return request.app.state.ws_hub

# -----------------------------
# Admin: Devices / Current Logins
# -----------------------------
@router.get("/devices", response_model=AdminDeviceUserListResponse)
async def list_admin_device_users(
	role: schema.UserRole | None = Query(default=None),
	page: int = Query(default=1, ge=1),
	page_size: int = Query(default=25, ge=1, le=100),
	auth_service: AuthService = Depends(get_auth_service),
) -> AdminDeviceUserListResponse:
	try:
		return await auth_service.list_admin_device_users(
			role=role,
			page=page,
			page_size=page_size,
		)
	except AuthError as exc:
		raise to_http_exception(exc) from exc


@router.get("/users/{user_id}/devices", response_model=AdminUserDeviceListResponse)
async def list_admin_user_devices(
	user_id: str = Path(...),
	auth_service: AuthService = Depends(get_auth_service),
) -> AdminUserDeviceListResponse:
	try:
		return await auth_service.list_admin_user_devices(user_id=user_id)
	except AuthError as exc:
		raise to_http_exception(exc) from exc


@router.delete("/users/{user_id}/devices/{session_id}", response_model=MessageResponse)
async def remove_admin_user_device(
	user_id: str = Path(...),
	session_id: str = Path(...),
	auth_service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
	try:
		return await auth_service.remove_admin_user_device(
			user_id=user_id,
			session_id=session_id,
		)
	except AuthError as exc:
		raise to_http_exception(exc) from exc


@router.delete("/users/{user_id}/devices", response_model=MessageResponse)
async def remove_all_admin_user_devices(
	user_id: str = Path(...),
	auth_service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
	try:
		return await auth_service.remove_all_admin_user_devices(user_id=user_id)
	except AuthError as exc:
		raise to_http_exception(exc) from exc

@router.get("/device-settings", response_model=DriverDeviceSettingsResponse)
async def get_driver_device_settings(
	auth_service: AuthService = Depends(get_auth_service),
) -> DriverDeviceSettingsResponse:
	try:
		return await auth_service.get_driver_device_settings()
	except AuthError as exc:
		raise to_http_exception(exc) from exc


@router.patch("/device-settings", response_model=DriverDeviceSettingsResponse)
async def update_driver_device_settings(
	payload: DriverDeviceSettingsUpdateRequest,
	auth_service: AuthService = Depends(get_auth_service),
) -> DriverDeviceSettingsResponse:
	try:
		return await auth_service.update_driver_device_settings(payload)
	except AuthError as exc:
		raise to_http_exception(exc) from exc

def _serialize_admin_rfid_device(device: schema.RFIDDevice) -> dict:
	return {
		"id": device.id,
		"serial_number": device.serial_number,
		"vehicle_id": device.vehicle_id,
		"is_active": device.is_active,
		"decommissioned_at": device.decommissioned_at,
		"last_seen_at": device.last_seen_at,
		"last_seen_lat": device.last_seen_lat,
		"last_seen_lng": device.last_seen_lng,
		"notes": device.notes,
		"created_at": device.created_at,
		"updated_at": device.updated_at,
	}


def _serialize_admin_rfid_ride(
	ride: schema.RFIDTripRide,
	passenger: schema.User | None,
	passenger_profile: schema.PassengerProfile | None,
	card: schema.RFIDCard | None,
	pickup_stop: schema.Stop | None,
	dropoff_stop: schema.Stop | None,
) -> dict:
	return {
		"rfid_ride_id": ride.id,
		"card_id": ride.card_id,
		"card_uid_masked": card.card_uid_masked if card else None,
		"passenger_user_id": ride.passenger_user_id,
		"passenger_name": passenger_profile.full_name if passenger_profile else None,
		"passenger_email": passenger.email if passenger else None,
		"status": ride.status,
		"pickup_stop": {
			"id": ride.pickup_stop_id,
			"name": pickup_stop.name if pickup_stop else None,
			"sequence": ride.pickup_sequence_no,
		},
		"dropoff_stop": {
			"id": ride.dropoff_stop_id,
			"name": dropoff_stop.name if dropoff_stop else None,
			"sequence": ride.dropoff_sequence_no,
		}
		if ride.dropoff_stop_id
		else None,
		"boarded_at": ride.boarded_at,
		"dropped_at": ride.dropped_at,
		"fare_amount": float(ride.fare_amount or 0),
		"fare_reversed_amount": float(ride.fare_reversed_amount or 0),
		"fare_net_amount": float(
			(ride.fare_amount or 0) - (ride.fare_reversed_amount or 0)
		),

		"commission_percent_snapshot": float(
			ride.commission_percent_snapshot or 0
		),
		"commission_amount": float(ride.commission_amount or 0),

		"driver_payout_amount": float(ride.driver_payout_amount or 0),
		"driver_payout_reversed_amount": float(
			ride.driver_payout_reversed_amount or 0
		),
		"driver_payout_net_amount": float(
			(ride.driver_payout_amount or 0)
			- (ride.driver_payout_reversed_amount or 0)
		),

		"platform_amount": float(ride.platform_amount or 0),
		"platform_amount_reversed": float(ride.platform_amount_reversed or 0),
		"platform_net_amount": float(
			(ride.platform_amount or 0)
			- (ride.platform_amount_reversed or 0)
		),

		"transfer_status": ride.transfer_status,
	}

@router.post("/send-notification/{user_id}", tags=["Admin Notifications"])
async def send_admin_notification(
	user_id: str,
	payload: NotificationDataPayload,
	request: Request,
	db: AsyncSession = Depends(get_async_session),
	current_user: schema.User = Depends(get_current_admin),
):
	hub = get_ws_hub(request)
	service = AdminService(db, ws_hub=hub)

	await service.send_user_notification(
		user_id=user_id,
		title=payload.title,
		message=payload.message,
		data=payload.data,
	)

	return {"status": "success", "message": f"Notification sent to {user_id}"}


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
					"profile_verification_req_date": p.verification_requested_at
					if p
					else None,
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
					"vehicle_id": v.id if v else None,
					"reg_no": v.registration_number if v else None,
					"reg_valid_till": v.registration_valid_till if v else None,
					"color": v.color if v else None,
					"model": v.vehicle_model if v else None,
					"capacity": v.seat_count if v else 0,
					"ac": v.has_ac if v else False,
					"status": v.verification_status if v else "Pending",
					# FIXED: Checking v.rc_file_path instead of d.rc_file_path
					"rc_file_path": v.rc_file_path
					if (v and v.rc_file_path)
					else "NA",
					"rear_photo_file_path": v.rear_photo_file_path
					if (v and v.rear_photo_file_path)
					else "NA",
					"vechical_verification_req_date": v.verification_requested_at
					if v
					else None,
					"vehical_reviewed_at": v.reviewed_at if v else None,
					"vehical_owner_ship_type": v.ownership_type if v else None,
					"vechical_auth_file_path": v.authentication_file_path
					if v
					else None,
					"front_photo_file_path": v.front_photo_file_path
					if v
					else None,
					"interior_photo_file_path": v.interior_photo_file_path
					if v
					else None,
					"left_side_file_path": v.left_side_file_path
					if v
					else None,
					"right_side_file_path": v.right_side_file_path
					if v
					else None,
					"insurance_document": v.insurance_document if v else None,
					"pollution_document": v.pollution_document if v else None,
					"owner_aadhaar_card": v.owner_aadhaar_card if v else None,
					"owner_name": v.owner_name if v else None,
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
async def get_all_passengers_info(
	db: AsyncSession = Depends(get_async_session),
):
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
				else "Not Set"
			},
			"total_trips_booked": len(p.passenger_bookings),
		}
		for p in passengers
	]


# -----------------------------
# Admin: Specific Driver Details Using User_id
# -----------------------------
@router.get("/driver/{user_id}")
async def get_driver_details(
	user_id: str, db: AsyncSession = Depends(get_async_session)
):
	service = AdminService(db)
	d = await service.fetch_driver_by_id(user_id)

	if not d:
		return {"error": "Driver not found"}

	rfid_devices = []
	if d.vehicle:
		rfid_devices_result = await db.execute(
			select(schema.RFIDDevice)
			.where(schema.RFIDDevice.vehicle_id == d.vehicle.id)
			.order_by(
				schema.RFIDDevice.is_active.desc(),
				schema.RFIDDevice.created_at.desc(),
			)
		)
		rfid_devices = list(rfid_devices_result.scalars().all())

	return {
		"user_id": d.id,
		"email": d.email,
		"is_active": d.is_active,
		"profile": {
			"full_name": d.driver_profile.full_name
			if d.driver_profile
			else "Not Set",
			"phone": d.driver_profile.phone if d.driver_profile else None,
			"verification_status": d.driver_profile.verification_status
			if d.driver_profile
			else "draft",
			"profile_verification_req_date": d.driver_profile.verification_requested_at
			if d.driver_profile
			else None,
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
		"vehicle": {
			"reg_no": d.vehicle.registration_number if d.vehicle else None,
			"reg_valid_till": d.vehicle.registration_valid_till
			if d.vehicle
			else None,
			"color": d.vehicle.color if d.vehicle else None,
			"model": d.vehicle.vehicle_model if d.vehicle else None,
			"vehical_name": d.vehicle.vehicle_name if d.vehicle else None,
			"capacity": d.vehicle.seat_count if d.vehicle else 0,
			"has_ac": d.vehicle.has_ac if d.vehicle else False,
			"verification": d.vehicle.verification_status
			if d.vehicle
			else "N/A",
			"rc_file_path": d.vehicle.rc_file_path if d.vehicle else "NA",
			"rear_photo_file_path": d.vehicle.rear_photo_file_path
			if d.vehicle
			else "NA",
			"vechical_verification_req_date": d.vehicle.verification_requested_at
			if d.vehicle
			else None,
			"vehical_reviewed_at": d.vehicle.reviewed_at
			if d.vehicle
			else None,
			"vehical_owner_ship_type": d.vehicle.ownership_type
			if d.vehicle
			else None,
			"vechical_auth_file_path": d.vehicle.authentication_file_path
			if d.vehicle
			else "NA",
			"front_photo_file_path": d.vehicle.front_photo_file_path
			if d.vehicle
			else None,
			"interior_photo_file_path": d.vehicle.interior_photo_file_path
			if d.vehicle
			else None,
			"left_side_file_path": d.vehicle.left_side_file_path
			if d.vehicle
			else None,
			"right_side_file_path": d.vehicle.right_side_file_path
			if d.vehicle
			else None,
			"insurance_document": d.vehicle.insurance_document
			if d.vehicle
			else None,
			"pollution_document": d.vehicle.pollution_document
			if d.vehicle
			else None,
			"owner_aadhaar_card": d.vehicle.owner_aadhaar_card
			if d.vehicle
			else None,
			"owner_name": d.vehicle.owner_name if d.vehicle else None,
		},
		"vehical_physical_inspection": {
			"inspection_status": d.vehicle.inspection_status
			if d.vehicle
			else None,
			"inspection_reason": d.vehicle.inspection_reason
			if d.vehicle
			else None,
			# "inspection_created_at":d.vehicle.inspection_created_at if d.vehicle else None,
			"inspection_reviewed_at": d.vehicle.inspection_reviewed_at
			if d.vehicle
			else None,
		},
		"account_info": {
			"account_number": d.driver_profile.bank_account_number
			if d.driver_profile
			else None,
			"IFSC_code": d.driver_profile.ifsc_code
			if d.driver_profile
			else None,
			"passbook_url": d.driver_profile.passbook_file_path
			if d.driver_profile
			else None,
		},
		"rfid": {
			"vehicle_id": d.vehicle.id if d.vehicle else None,
			"default_reserved_seat_count": d.vehicle.default_rfid_reserved_seat_count
			if d.vehicle
			else 0,
			"device_count": len(rfid_devices),
			"active_device_count": sum(
				1
				for device in rfid_devices
				if device.is_active and device.decommissioned_at is None
			),
			"devices": [
				_serialize_admin_rfid_device(device)
				for device in rfid_devices
			],
		},
	}

@router.get(
	"/vehicles/inspection-statuses",
	response_model=AdminVehicleInspectionStatusListResponse,
)
async def list_vehicle_inspection_statuses(
	page: int = Query(default=1, ge=1),
	page_size: int = Query(default=25, ge=1, le=100),
	inspection_status: schema.VehicleInspectionStatus | None = Query(default=None),
	inspection_status_missing: bool | None = Query(
		default=None,
		description="true = only vehicles with no inspection status; false = only vehicles with any inspection status",
	),
	vehicle_verification_status: schema.VehicleVerificationStatus | None = Query(default=None),
	is_active: bool | None = Query(default=None),
	driver_user_id: str | None = Query(default=None, min_length=1, max_length=36),
	q: str | None = Query(
		default=None,
		min_length=1,
		max_length=80,
		description="Search registration number, vehicle name, model, driver email, or driver name.",
	),
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminService(db)
	return await service.fetch_vehicle_inspection_statuses(
		page=page,
		page_size=page_size,
		inspection_status=inspection_status,
		inspection_status_missing=inspection_status_missing,
		vehicle_verification_status=vehicle_verification_status,
		is_active=is_active,
		driver_user_id=driver_user_id,
		q=q,
	)


# -----------------------------
# Admin: Specific Vehical Details Using User_id
# -----------------------------
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
			"reg_valid_till": d.vehicle.registration_valid_till
			if d.vehicle
			else None,
			"model": d.vehicle.vehicle_model if d.vehicle else None,
			"capacity": d.vehicle.seat_count if d.vehicle else 0,
			"has_ac": d.vehicle.has_ac if d.vehicle else False,
			"verification": d.vehicle.verification_status
			if d.vehicle
			else "N/A",
			"rc_file_path": v.rc_file_path if (v and v.rc_file_path) else "NA",
			"rear_photo_file_path": v.rear_photo_file_path
			if (v and v.rear_photo_file_path)
			else "NA",
			"vechical_verification_req_date": v.verification_requested_at
			if v
			else None,
			"vehical_reviewed_at": v.reviewed_at if v else None,
			"vehical_owner_ship_type": v.ownership_type if v else None,
			"vechical_auth_file_path": v.authentication_file_path
			if v
			else "NA",
		},
	}


# -----------------------------
# Admin: Specific Passenger Details Using User_id
# -----------------------------
# @router.get("/passenger/{user_id}")
# async def get_passenger_details(
# 	user_id: str, db: AsyncSession = Depends(get_async_session)
# ):
# 	service = AdminService(db)
# 	p = await service.fetch_passenger_by_id(user_id)

# 	if not p:
# 		return {"error": "Passenger not found"}

# 	return {
# 		"user_id": p.id,
# 		"email": p.email,
# 		"joined_at": p.created_at,
# 		"is_active": p.is_active,
# 		"profile": {
# 			"full_name": p.passenger_profile.full_name
# 			if p.passenger_profile
# 			else "Not Set",
# 			"avatar": p.passenger_profile.profile_picture_path
# 			if p.passenger_profile
# 			else None,
# 		},
# 		"booking_history": {
# 			"total_count": len(p.passenger_bookings),
# 			"bookings": [
# 				{
# 					"booking_id": b.id,
# 					"status": b.booking_status,
# 					"fare": float(b.fare_amount),
# 					"created_at": b.created_at,
# 					"pickup_stop": {
# 						"id": b.pickup_stop.id,
# 						"name": b.pickup_stop.name,
# 						"sequence": b.pickup_sequence_no_snapshot,
# 					},
# 					"dropoff_stop": {
# 						"id": b.dropoff_stop.id,  # Ensure this says dropoff_stop
# 						"name": b.dropoff_stop.name,  # Ensure this says dropoff_stop
# 						"sequence": b.dropoff_sequence_no_snapshot,
# 					},
# 				}
# 				for b in p.passenger_bookings
# 			],
# 		},
# 	}


@router.get("/passenger/{user_id}")
async def get_passenger_details(
	user_id: str, db: AsyncSession = Depends(get_async_session)
):
	service = AdminService(db)
	p = await service.fetch_passenger_by_id(user_id)

	if not p:
		return {"error": "Passenger not found"}

	bookings_data = []
	for booking in p.passenger_bookings:
		# Find the drop scan event
		drop_scan = None
		for scan in booking.scan_events:
			if scan.scan_type == schema.ScanType.DROP:
				drop_scan = scan
				break

		booking_info = {
			"booking_id": booking.id,
			"status": booking.booking_status,
			"fare": float(booking.fare_amount),
			"created_at": booking.created_at,
			"pickup_stop": {
				"id": booking.pickup_stop.id,
				"name": booking.pickup_stop.name,
				"sequence": booking.pickup_sequence_no_snapshot,
			},
			"dropoff_stop": {
				"id": booking.dropoff_stop.id,
				"name": booking.dropoff_stop.name,
				"sequence": booking.dropoff_sequence_no_snapshot,
			},
			# NEW: Actual drop information (where they actually got off)
			"actual_drop_stop_id": drop_scan.matched_stop_id
			if drop_scan
			else None,
			"actual_drop_stop_name": drop_scan.matched_stop.name
			if drop_scan and drop_scan.matched_stop
			else None,
			"actual_dropped_at": drop_scan.created_at if drop_scan else None,
		}
		bookings_data.append(booking_info)

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
			"bookings": bookings_data,
		},
	}


# -----------------------------
# Admin: Specific Vehicals Details Using Vehicle_id
# -----------------------------
@router.get("/vehicle/details/{vehicle_id}")
async def get_vehicle_data(
	vehicle_id: str, db: AsyncSession = Depends(get_async_session)
):
	service = AdminService(db)
	vehicle = await service.fetch_vehicle_details_by_id(vehicle_id)

	if not vehicle:
		raise HTTPException(status_code=404, detail="Vehicle not found")

	return {
		"vehicle_id": vehicle.id,
		"physical_inspection": {
			"status": vehicle.inspection_status,
			"reviewed_at": vehicle.inspection_reviewed_at,
			"reason": vehicle.inspection_reason,
		},
		"is_active": vehicle.is_active,
	}


# -----------------------------
# Admin: Check the users who has been inactive from last 3 months
# -----------------------------


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


# -----------------------------
# Admin: Active,inactive for the driver and passenger Using User_id
# -----------------------------
@router.post("/driver/activate/{user_id}")
async def activate_driver(
	user_id: str, db: AsyncSession = Depends(get_async_session)
):
	service = AdminService(db)
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


# -----------------------------
# Admin:  driver profiles verification
# -----------------------------
@router.post("/driver/verify/{user_id}")
async def verify_driver(
	user_id: str,
	data: VerificationUpdate,
	request: Request,
	db: AsyncSession = Depends(get_async_session),
):
	hub = get_ws_hub(request)
	admin_service = AdminService(db)
	notif_service = NotificationService(db, ws_hub=hub)

	driver = await admin_service.fetch_driver_by_id(user_id)
	if not driver or not driver.driver_profile:
		raise HTTPException(status_code=404, detail="Driver profile not found")

	await admin_service.update_driver_verification(
		user_id=user_id,
		status=data.status,
		rejection_reason=data.rejection_reason,
	)

	# Use the instance (notif_service) not the Class (NotificationService)
	title = (
		"Profile Verified!"
		if data.status == "verified"
		else "Profile Action Required"
	)
	message = (
		"Your profile has been verified. You can now accept rides."
		if data.status == "verified"
		else f"Your profile was not approved. Reason: {data.rejection_reason}"
	)

	await notif_service.notify_user(
		user_id=user_id,
		title=title,
		message=message,
		data={"type": "verification_update", "status": data.status},
		# commit=False # We handle the commit below
	)

	await db.commit()

	return {
		"message": f"Driver verification status updated to {data.status}",
		"user_id": user_id,
	}

# -----------------------------
# Admin: RFID Devices
# -----------------------------

@router.get(
    "/rfid/device-vehicle-options",
    response_model=RFIDDeviceVehicleOptionListResponse,
    tags=["Admin RFID"],
)
async def list_rfid_device_vehicle_options(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    q: str | None = Query(
        default=None,
        min_length=1,
        max_length=80,
        description="Search by driver name, vehicle license plate, or driver id.",
    ),
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminRFIDService(db)

    items, count = await service.list_device_vehicle_options(
        page=page,
        page_size=page_size,
        q=q,
    )

    return {
        "items": items,
        "count": count,
    }

@router.get(
    "/rfid/card-options",
    response_model=RFIDCardOptionListResponse,
    tags=["Admin RFID"],
)
async def list_rfid_card_options(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    q: str | None = Query(
        default=None,
        min_length=1,
        max_length=80,
        description="Search by masked card UID, assigned passenger name, or passenger id.",
    ),
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminRFIDService(db)

    items, count = await service.list_card_options(
        page=page,
        page_size=page_size,
        q=q,
    )

    return {
        "items": items,
        "count": count,
    }

@router.post(
	"/rfid/devices",
	response_model=RFIDDeviceMutationResponse,
	tags=["Admin RFID"],
)
async def create_rfid_device(
	payload: RFIDDeviceCreateRequest,
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminRFIDService(db)
	device = await service.create_device(payload)

	await db.commit()
	await db.refresh(device)

	return {
		"message": "RFID device registered successfully.",
		"device": service.serialize_device(device),
	}

@router.get(
	"/rfid/devices",
	response_model=RFIDDeviceListResponse,
	tags=["Admin RFID"],
)
async def list_rfid_devices(
	page: int = Query(default=1, ge=1),
	page_size: int = Query(default=25, ge=1, le=100),
	vehicle_id: str | None = Query(default=None, min_length=1, max_length=36),
	is_active: bool | None = Query(default=None),
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminRFIDService(db)
	devices, count = await service.list_devices(
		page=page,
		page_size=page_size,
		vehicle_id=vehicle_id,
		is_active=is_active,
	)

	return {
		"items": [service.serialize_device(device) for device in devices],
		"count": count,
	}


@router.patch(
	"/rfid/devices/{device_id}",
	response_model=RFIDDeviceMutationResponse,
	tags=["Admin RFID"],
)
async def update_rfid_device(
	device_id: str,
	payload: RFIDDeviceUpdateRequest,
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminRFIDService(db)
	device = await service.update_device(
		device_id=device_id,
		payload=payload,
	)

	await db.commit()
	await db.refresh(device)

	return {
		"message": "RFID device updated successfully.",
		"device": service.serialize_device(device),
	}


@router.post(
	"/rfid/devices/{device_id}/activate",
	response_model=RFIDDeviceMutationResponse,
	tags=["Admin RFID"],
)
async def activate_rfid_device(
	device_id: str,
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminRFIDService(db)
	device = await service.set_device_active(
		device_id=device_id,
		is_active=True,
	)

	await db.commit()
	await db.refresh(device)

	return {
		"message": "RFID device activated successfully.",
		"device": service.serialize_device(device),
	}


@router.post(
	"/rfid/devices/{device_id}/deactivate",
	response_model=RFIDDeviceMutationResponse,
	tags=["Admin RFID"],
)
async def deactivate_rfid_device(
	device_id: str,
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminRFIDService(db)
	device = await service.set_device_active(
		device_id=device_id,
		is_active=False,
	)

	await db.commit()
	await db.refresh(device)

	return {
		"message": "RFID device deactivated successfully.",
		"device": service.serialize_device(device),
	}


@router.post(
	"/rfid/devices/{device_id}/decommission",
	response_model=RFIDDeviceMutationResponse,
	tags=["Admin RFID"],
)
async def decommission_rfid_device(
	device_id: str,
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminRFIDService(db)
	device = await service.decommission_device(device_id)

	await db.commit()
	await db.refresh(device)

	return {
		"message": "RFID device decommissioned successfully.",
		"device": service.serialize_device(device),
	}

# -----------------------------
# Admin: RFID Cards
# -----------------------------


@router.post(
	"/rfid/cards",
	response_model=RFIDCardMutationResponse,
	tags=["Admin RFID"],
)
async def register_rfid_card(
	payload: RFIDCardRegisterRequest,
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminRFIDService(db)
	card = await service.register_card(payload)

	await db.commit()
	await db.refresh(card)

	return {
		"message": "RFID card registered successfully.",
		"card": service.serialize_card(card),
	}


@router.post(
	"/rfid/cards/bulk",
	response_model=RFIDCardBulkRegisterResponse,
	tags=["Admin RFID"],
)
async def bulk_register_rfid_cards(
	payload: RFIDCardBulkRegisterRequest,
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminRFIDService(db)
	items, created_count, skipped_count = await service.bulk_register_cards(payload)

	await db.commit()

	return {
		"message": "RFID card bulk registration completed.",
		"created_count": created_count,
		"skipped_count": skipped_count,
		"items": items,
	}


@router.get(
	"/rfid/cards",
	response_model=RFIDCardListResponse,
	tags=["Admin RFID"],
)
async def list_rfid_cards(
	page: int = Query(default=1, ge=1),
	page_size: int = Query(default=25, ge=1, le=100),
	inventory_status: schema.RFIDCardInventoryStatus | None = Query(default=None),
	authorization_status: schema.RFIDCardAuthorizationStatus | None = Query(default=None),
	assigned_passenger_user_id: str | None = Query(
		default=None,
		min_length=1,
		max_length=36,
	),
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminRFIDService(db)
	cards, count = await service.list_cards(
		page=page,
		page_size=page_size,
		inventory_status=inventory_status,
		authorization_status=authorization_status,
		assigned_passenger_user_id=assigned_passenger_user_id,
	)

	return {
		"items": [service.serialize_card(card) for card in cards],
		"count": count,
	}


@router.get(
	"/rfid/cards/{card_id}",
	response_model=RFIDCardDetailResponse,
	tags=["Admin RFID"],
)
async def get_rfid_card_detail(
	card_id: str,
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminRFIDService(db)
	return await service.get_card_detail(card_id)


@router.post(
	"/rfid/cards/{card_id}/assign",
	response_model=RFIDCardMutationResponse,
	tags=["Admin RFID"],
)
async def assign_rfid_card(
	card_id: str,
	payload: RFIDCardAssignRequest,
	db: AsyncSession = Depends(get_async_session),
	current_user: schema.User = Depends(get_current_admin),
):
	service = AdminRFIDService(db)
	card = await service.assign_card(
		card_id=card_id,
		payload=payload,
		admin_user_id=current_user.id,
	)

	await db.commit()
	await db.refresh(card)

	return {
		"message": "RFID card assigned successfully.",
		"card": service.serialize_card(card),
	}


@router.post(
	"/rfid/cards/{card_id}/unassign",
	response_model=RFIDCardMutationResponse,
	tags=["Admin RFID"],
)
async def unassign_rfid_card(
	card_id: str,
	payload: RFIDCardUnassignRequest,
	db: AsyncSession = Depends(get_async_session),
	current_user: schema.User = Depends(get_current_admin),
):
	service = AdminRFIDService(db)
	card = await service.unassign_card(
		card_id=card_id,
		payload=payload,
		admin_user_id=current_user.id,
	)

	await db.commit()
	await db.refresh(card)

	return {
		"message": "RFID card unassigned successfully.",
		"card": service.serialize_card(card),
	}

# -----------------------------
# Admin: RFID Recharges
# -----------------------------


@router.post(
	"/rfid/recharges/manual",
	response_model=RFIDRechargeMutationResponse,
	tags=["Admin RFID"],
)
async def create_manual_rfid_recharge(
	payload: RFIDRechargeCreateRequest,
	db: AsyncSession = Depends(get_async_session),
	current_user: schema.User = Depends(get_current_admin),
):
	service = AdminRFIDService(db)
	recharge, account = await service.create_manual_recharge(
		payload=payload,
		admin_user_id=current_user.id,
	)

	await db.commit()
	await db.refresh(recharge)
	await db.refresh(account)

	return {
		"message": "RFID recharge recorded successfully.",
		"recharge": service.serialize_recharge(recharge),
		"account": service.serialize_account(account),
	}

@router.get(
	"/rfid/cards/{card_id}/ledger",
	response_model=RFIDLedgerEntryListResponse,
	tags=["Admin RFID"],
)
async def list_rfid_card_ledger_entries(
	card_id: str,
	page: int = Query(default=1, ge=1),
	page_size: int = Query(default=25, ge=1, le=100),
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminRFIDService(db)
	entries, count = await service.list_card_ledger_entries(
		card_id=card_id,
		page=page,
		page_size=page_size,
	)

	return {
		"items": [service.serialize_ledger_entry(entry) for entry in entries],
		"count": count,
	}


@router.get(
	"/rfid/cards/{card_id}/recharges",
	response_model=RFIDRechargeListResponse,
	tags=["Admin RFID"],
)
async def list_rfid_card_recharges(
	card_id: str,
	page: int = Query(default=1, ge=1),
	page_size: int = Query(default=25, ge=1, le=100),
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminRFIDService(db)
	recharges, count = await service.list_card_recharges(
		card_id=card_id,
		page=page,
		page_size=page_size,
	)

	return {
		"items": [service.serialize_recharge(recharge) for recharge in recharges],
		"count": count,
	}

@router.post(
	"/rfid/cards/{card_id}/block",
	response_model=RFIDCardMutationResponse,
	tags=["Admin RFID"],
)
async def block_rfid_card(
	card_id: str,
	payload: RFIDCardBlockRequest,
	db: AsyncSession = Depends(get_async_session),
	current_user: schema.User = Depends(get_current_admin),
):
	service = AdminRFIDService(db)
	card = await service.block_card(
		card_id=card_id,
		payload=payload,
		admin_user_id=current_user.id,
	)

	await db.commit()
	await db.refresh(card)

	return {
		"message": "RFID card blocked successfully.",
		"card": service.serialize_card(card),
	}


@router.post(
	"/rfid/cards/{card_id}/unblock",
	response_model=RFIDCardMutationResponse,
	tags=["Admin RFID"],
)
async def unblock_rfid_card(
	card_id: str,
	payload: RFIDCardBlockRequest,
	db: AsyncSession = Depends(get_async_session),
	current_user: schema.User = Depends(get_current_admin),
):
	service = AdminRFIDService(db)
	card = await service.unblock_card(
		card_id=card_id,
		payload=payload,
		admin_user_id=current_user.id,
	)

	await db.commit()
	await db.refresh(card)

	return {
		"message": "RFID card unblocked successfully.",
		"card": service.serialize_card(card),
	}

@router.post(
	"/rfid/cards/{card_id}/return",
	response_model=RFIDCardMutationResponse,
	tags=["Admin RFID"],
)
async def return_rfid_card(
	card_id: str,
	payload: RFIDCardReturnRequest,
	db: AsyncSession = Depends(get_async_session),
	current_user: schema.User = Depends(get_current_admin),
):
	service = AdminRFIDService(db)
	card = await service.return_card(
		card_id=card_id,
		payload=payload,
		admin_user_id=current_user.id,
	)

	await db.commit()
	await db.refresh(card)

	return {
		"message": "RFID card returned successfully.",
		"card": service.serialize_card(card),
	}


@router.post(
	"/rfid/cards/{card_id}/decommission",
	response_model=RFIDCardMutationResponse,
	tags=["Admin RFID"],
)
async def decommission_rfid_card(
	card_id: str,
	payload: RFIDCardDecommissionRequest,
	db: AsyncSession = Depends(get_async_session),
	current_user: schema.User = Depends(get_current_admin),
):
	service = AdminRFIDService(db)
	card = await service.decommission_card(
		card_id=card_id,
		payload=payload,
		admin_user_id=current_user.id,
	)

	await db.commit()
	await db.refresh(card)

	return {
		"message": "RFID card decommissioned successfully.",
		"card": service.serialize_card(card),
	}

@router.get(
	"/rfid/rides/payout-ready",
	response_model=RFIDPayoutTransferListResponse,
	tags=["Admin RFID"],
)
async def list_payout_ready_rfid_transfers(
	page: int = Query(default=1, ge=1),
	page_size: int = Query(default=25, ge=1, le=100),
	driver_user_id: str | None = Query(default=None, min_length=1, max_length=36),
	scheduled_trip_id: str | None = Query(default=None, min_length=1, max_length=36),
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminRFIDService(db)
	transfers, count = await service.list_payout_ready_rfid_transfers(
		page=page,
		page_size=page_size,
		driver_user_id=driver_user_id,
		scheduled_trip_id=scheduled_trip_id,
	)

	return {
		"items": [
			service.serialize_payout_transfer(transfer)
			for transfer in transfers
		 ],
		"count": count,
	}

@router.post(
	"/rfid/payout-transfers/{transfer_id}/trigger",
	tags=["Admin RFID"],
)
async def trigger_rfid_payout_transfer(
	transfer_id: str = Path(..., min_length=1, max_length=36),
	db: AsyncSession = Depends(get_async_session),
):
	service = RoutePayoutService(db)
	result = await service.trigger_rfid_payout_transfer(transfer_id)

	await db.commit()

	return result

@router.get(
	"/rfid/payout-transfers",
	response_model=RFIDPayoutTransferListResponse,
	tags=["Admin RFID"],
)
async def list_rfid_payout_transfers(
	page: int = Query(default=1, ge=1),
	page_size: int = Query(default=25, ge=1, le=100),
	status: schema.RFIDPayoutTransferStatus | None = Query(default=None),
	driver_user_id: str | None = Query(default=None, min_length=1, max_length=36),
	scheduled_trip_id: str | None = Query(default=None, min_length=1, max_length=36),
	rfid_ride_id: str | None = Query(default=None, min_length=1, max_length=36),
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminRFIDService(db)
	transfers, count = await service.list_rfid_payout_transfers(
		page=page,
		page_size=page_size,
		status=status,
		driver_user_id=driver_user_id,
		scheduled_trip_id=scheduled_trip_id,
		rfid_ride_id=rfid_ride_id,
	)

	return {
		"items": [
			service.serialize_payout_transfer(transfer)
			for transfer in transfers
		],
		"count": count,
	}

@router.post(
	"/rfid/payout-transfers/trigger-ready",
	tags=["Admin RFID"],
)
async def trigger_ready_rfid_payout_transfers(
	payload: RFIDPayoutTransferBulkTriggerRequest,
	db: AsyncSession = Depends(get_async_session),
):
	service = RoutePayoutService(db)
	result = await service.trigger_ready_rfid_payout_transfers(
		transfer_ids=payload.transfer_ids,
		driver_user_id=payload.driver_user_id,
		scheduled_trip_id=payload.scheduled_trip_id,
		limit=payload.limit,
	)

	return result

@router.post(
	"/rfid/payout-transfers/refresh-withheld",
	tags=["Admin RFID"],
)
async def refresh_withheld_rfid_payout_transfers(
	payload: RFIDPayoutTransferRefreshWithheldRequest,
	db: AsyncSession = Depends(get_async_session),
):
	service = RoutePayoutService(db)
	result = await service.refresh_withheld_rfid_payout_transfers(
		driver_user_id=payload.driver_user_id,
		scheduled_trip_id=payload.scheduled_trip_id,
		limit=payload.limit,
	)

	await db.commit()

	return result

@router.post(
	"/rfid/payout-transfers/reconcile-created",
	tags=["Admin RFID"],
)
async def reconcile_created_rfid_payout_transfers(
	payload: RFIDPayoutTransferReconcileCreatedRequest,
	db: AsyncSession = Depends(get_async_session),
):
	service = RoutePayoutService(db)
	result = await service.reconcile_created_rfid_payout_transfers(
		driver_user_id=payload.driver_user_id,
		scheduled_trip_id=payload.scheduled_trip_id,
		limit=payload.limit,
	)

	await db.commit()

	return result

@router.post(
	"/rfid/rides/{rfid_ride_id}/reverse-deduction",
	response_model=RFIDGenericMutationResponse,
	tags=["Admin RFID"],
)
async def reverse_rfid_ride_deduction(
	rfid_ride_id: str = Path(..., min_length=1, max_length=36),
	payload: RFIDRideDeductionReversalRequest = Body(...),
	db: AsyncSession = Depends(get_async_session),
	current_admin: schema.User = Depends(get_current_admin),
):
	service = AdminRFIDService(db)
	result = await service.reverse_rfid_ride_deduction(
		rfid_ride_id=rfid_ride_id,
		amount=payload.amount,
		reason=payload.reason,
		admin_user_id=current_admin.id,
		admin_note=payload.admin_note,
	)

	await db.commit()

	return {
		"message": result["message"],
		"data": result,
	}

@router.post(
	"/rfid/payout-transfers/{transfer_id}/reverse",
	response_model=RFIDGenericMutationResponse,
	tags=["Admin RFID"],
)
async def reverse_rfid_payout_transfer(
	transfer_id: str = Path(..., min_length=1, max_length=36),
	payload: RFIDPayoutTransferReversalRequest = Body(...),
	db: AsyncSession = Depends(get_async_session),
	current_admin: schema.User = Depends(get_current_admin),
):
	service = RoutePayoutService(db)
	result = await service.reverse_rfid_payout_transfer(
		transfer_id=transfer_id,
		amount=payload.amount,
		reason=payload.reason,
		admin_user_id=current_admin.id,
		admin_note=payload.admin_note,
	)

	await db.commit()

	return {
		"message": result["message"],
		"data": result,
	}

@router.get(
	"/rfid/payout-transfer-reversals",
	response_model=RFIDPayoutTransferReversalListResponse,
	tags=["Admin RFID"],
)
async def list_rfid_payout_transfer_reversals(
	page: int = Query(default=1, ge=1),
	page_size: int = Query(default=25, ge=1, le=100),
	status: schema.RFIDPayoutTransferReversalStatus | None = Query(default=None),
	rfid_payout_transfer_id: str | None = Query(default=None, min_length=1, max_length=36),
	rfid_ride_id: str | None = Query(default=None, min_length=1, max_length=36),
	driver_user_id: str | None = Query(default=None, min_length=1, max_length=36),
	scheduled_trip_id: str | None = Query(default=None, min_length=1, max_length=36),
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminRFIDService(db)
	reversals, count = await service.list_rfid_payout_transfer_reversals(
		page=page,
		page_size=page_size,
		status=status,
		rfid_payout_transfer_id=rfid_payout_transfer_id,
		rfid_ride_id=rfid_ride_id,
		driver_user_id=driver_user_id,
		scheduled_trip_id=scheduled_trip_id,
	)

	return {
		"items": [
			service.serialize_payout_transfer_reversal(reversal)
			for reversal in reversals
		],
		"count": count,
	}

@router.get(
	"/rfid/payout-transfers/{transfer_id}",
	response_model=RFIDPayoutTransferDetailResponse,
	tags=["Admin RFID"],
)
async def get_rfid_payout_transfer_detail(
	transfer_id: str = Path(..., min_length=1, max_length=36),
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminRFIDService(db)
	return await service.get_rfid_payout_transfer_detail(transfer_id)

@router.get(
	"/rfid/rides/{rfid_ride_id}/money-detail",
	response_model=RFIDRideMoneyDetailResponse,
	tags=["Admin RFID"],
)
async def get_rfid_ride_money_detail(
	rfid_ride_id: str = Path(..., min_length=1, max_length=36),
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminRFIDService(db)
	return await service.get_rfid_ride_money_detail(rfid_ride_id)

@router.get(
	"/rfid/payout-operations-summary",
	response_model=RFIDPayoutOperationsSummaryResponse,
	tags=["Admin RFID"],
)
async def get_rfid_payout_operations_summary(
	driver_user_id: str | None = Query(default=None, min_length=1, max_length=36),
	scheduled_trip_id: str | None = Query(default=None, min_length=1, max_length=36),
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminRFIDService(db)
	return await service.get_rfid_payout_operations_summary(
		driver_user_id=driver_user_id,
		scheduled_trip_id=scheduled_trip_id,
	)

@router.get(
    "/rfid/seat-policy",
    response_model=AdminRFIDSeatPolicyResponse,
    tags=["Admin RFID"],
)
async def get_rfid_seat_policy(
    db: AsyncSession = Depends(get_async_session),
):
    service = AdminRFIDService(db)
    return await service.get_rfid_seat_policy()

@router.patch(
    "/rfid/seat-policy",
    response_model=AdminRFIDSeatPolicyResponse,
    tags=["Admin RFID"],
)
async def update_rfid_seat_policy(
    payload: AdminRFIDSeatPolicyUpdateRequest,
    db: AsyncSession = Depends(get_async_session),
    current_admin: schema.User = Depends(get_current_admin),
):
    service = AdminRFIDService(db)
    result = await service.update_rfid_seat_policy(
        allow_driver_rfid_seat_reservation=(
            payload.allow_driver_rfid_seat_reservation
        ),
    )

    await db.commit()

    return result

# -----------------------------
# Admin:  driver vehical verification
# -----------------------------
@router.post("/vehicle/verify/{user_id}")
async def verify_vehicle(
	user_id: str,
	data: VehicleVerificationUpdate,
	request: Request,
	db: AsyncSession = Depends(get_async_session),
):
	hub = get_ws_hub(request)
	service = AdminService(db, ws_hub=hub)

	# 1. Instantiate the NotificationService correctly
	notif_service = NotificationService(db, ws_hub=hub)

	driver = await service.fetch_driver_by_id(user_id)
	if not driver or not driver.vehicle:
		raise HTTPException(
			status_code=404, detail="Vehicle record not found for this user"
		)

	# 3. Update the vehicle status
	await service.update_vehicle_verification(
		user_id=user_id,
		status=data.status,
		rejection_reason=data.rejection_reason,
	)

	# --- NOTIFICATION LOGIC ---
	status_msg = "approved" if data.status == "verified" else "rejected"

	try:
		# Call the method on 'notif_service' (the instance), not the class
		await notif_service.notify_user(
			user_id=user_id,
			title="Vehicle Verification Update",
			message=f"Your vehicle {driver.vehicle.registration_number} has been {status_msg}.",
			data={"type": "vehicle_update", "status": data.status},
			commit=False,
		)
	except Exception as e:
		# Log the error but don't stop the whole process
		print(f"Notification failed: {e}")

	# Final commit for both the DB update and the notification record
	await db.commit()

	return {
		"message": f"Vehicle verification status updated to {data.status}",
		"user_id": user_id,
		"registration_number": driver.vehicle.registration_number,
	}


# -----------------------------
# Admin:  Vechicals Physicals Inspections Using Vehicle Id
# -----------------------------
@router.post("/vehicle/inspect/{vehicle_id}")
async def resolve_vehicle_inspection(
	vehicle_id: str,
	data: VehicleInspectionUpdate,
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminService(db)

	# 1. Check if vehicle exists
	vehicle = await service.fetch_vehicle_by_id(vehicle_id)
	if not vehicle:
		raise HTTPException(status_code=404, detail="Vehicle record not found")

	# 2. Perform the update via service
	# This automatically sets the inspection_created_at to now
	await service.update_physical_inspection(
		vehicle_id=vehicle_id, status=data.status, reason=data.reason
	)

	# 3. Commit the transaction
	await db.commit()

	return {
		"status": "success",
		"message": f"Vehicle physical inspection has been {data.status}",
		"vehicle_id": vehicle_id,
	}


# -----------------------------
# Admin:  Check Which Drivers Data has been Verified by Admin
# -----------------------------


@router.get("/drivers/verified_data")
async def get_fully_verified_fleet(
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminService(db)
	drivers = await service.fetch_fully_verified_drivers()

	fleet_data = []
	for d in drivers:
		# Accessing nested data from DriverProfile and Vehicle objects
		profile = d.driver_profile
		vehicle = d.vehicle

		fleet_data.append(
			{
				"driver_id": d.id,
				"personal_details": {
					"full_name": profile.full_name,  # From DriverProfile
					"phone": profile.phone,  # From DriverProfile
					"email": d.email,  # From User
				},
				"verification_info": {
					"profile_status": profile.verification_status,
					"profile_verified_at": profile.reviewed_at,
					"vehicle_status": vehicle.verification_status,
					"vehicle_verified_at": vehicle.reviewed_at,
				},
				"vehicle_details": {
					"registration": vehicle.registration_number,
					"model": f"{vehicle.vehicle_name} {vehicle.vehicle_model}",
					"has_ac": vehicle.has_ac,
				},
			}
		)

	return {"status": "success", "count": len(fleet_data), "items": fleet_data}


# ------------------- add stops and routes -----------------------------
# -----------------------------
# Admin:  Upload stops data using JSONL File by admin
# -----------------------------


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
		select(schema.Stop)
		.where(schema.Stop.is_active)
		.order_by(schema.Stop.name)
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


# -----------------------------
# Admin:  Upload Single stops at a time
# -----------------------------
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


# -----------------------------
# Admin: Delete the Stops Using Stop_ID
# -----------------------------
@router.delete("/stops/{stop_id}")
async def delete_stop(
	stop_id: str, db: AsyncSession = Depends(get_async_session)
):
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


# ----------------- specific routes create -------------------------
# -----------------------------
# Admin: Create a new Routes
# -----------------------------


@router.post("/routes/create")
async def create_route_identity(
	data: RouteCreate, db: AsyncSession = Depends(get_async_session)
):
	try:
		new_route = schema.Route(
			name=data.name.strip(),
			code=data.code.strip().upper(),
			has_ac=data.has_ac,
		)
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
				"has_ac": new_route.has_ac,
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
			status_code=400,
			detail={"error": "duplicate_entry", "message": detail_msg},
		)


# -----------------------------
# Admin: Add Stops into routes using Route_ID
# -----------------------------
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
		time_gap = (
			0 if current_seq == 1 else stop_info.assume_time_diff_minutes
		)

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


# -----------------------------
# Admin: Check all Existings Routes
# -----------------------------
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
			"has_ac": r.has_ac,
			"is_active": r.is_active,
			"total_stops": len(r.route_stops),
			"created_at": r.created_at,
		}
		for r in routes
	]


# -----------------------------
# Admin: Check Routes Data using routes_id
# -----------------------------
@router.get("/routes/{route_id}")
async def get_route_details(
	route_id: str, db: AsyncSession = Depends(get_async_session)
):
	# Fetch route and join stops in the correct sequence order
	stmt = (
		select(schema.Route)
		.options(
			joinedload(schema.Route.route_stops).joinedload(
				schema.RouteStop.stop
			)
		)
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
		"has_ac": route.has_ac,
		"is_active": route.is_active,
		"path": ordered_stops,
	}


# -----------------------------
# Admin: Change the routes into Active to Deactive and Vice Versa
# -----------------------------
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


# -----------------------------
# Admin: Set Fares of each a specific routes
# -----------------------------
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


# -----------------------------
# Admin: Check Fares of a Specific routes using routes_id
# -----------------------------
@router.get("/routes/{route_id}/fares")
async def get_route_fares(
	route_id: str, db: AsyncSession = Depends(get_async_session)
):
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


# -----------------------------
# Admin: Check all details of a Specific Routes using route_id
# -----------------------------
@router.get("/routes/{route_id}/full-report")
async def get_route_and_trip_details(
	route_id: str, db: AsyncSession = Depends(get_async_session)
):
	# Notice the change from .stops to .route_stops
	# and .trips to .scheduled_trips
	stmt = (
		select(schema.Route)
		.options(
			joinedload(schema.Route.route_stops).joinedload(
				schema.RouteStop.stop
			),
			joinedload(schema.Route.scheduled_trips)
			.joinedload(schema.ScheduledTrip.driver)  # Load the User
			.joinedload(
				schema.User.driver_profile
			),  # Load the Profile from User
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
					"model": trip.vehicle.vehicle_model
					if trip.vehicle
					else "N/A",
					"capacity": trip.vehicle.seat_count if trip.vehicle else 0,
				},
			}
			for trip in route.scheduled_trips
		],
	}


# -----------------------------
# Admin: Get all trips using all details
# -----------------------------


@router.get("/trips/monitor")
async def monitor_all_trips(
	status: schema.ScheduledTripStatus | None = Query(None),
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


# -----------------------------
# Admin: Cancel a Specific Trip Using Trip_id
# -----------------------------
@router.patch("/trips/{trip_id}/cancel")
async def cancel_trip_by_id(
	trip_id: str,
	request: Request,
	reason: str = Body(..., embed=True),
	db: AsyncSession = Depends(get_async_session),
):
	hub = get_ws_hub(request)
	service = AdminService(db, ws_hub=hub)

	# 1. Create the instance of the NotificationService
	notif_service = NotificationService(db, ws_hub=hub)

	trip = await service.get_trip_by_id(trip_id)
	if not trip:
		raise HTTPException(status_code=404, detail="Trip not found")

	result = await service.cancel_trip(trip_id, reason)

	if not result["success"]:
		status_code = 404 if "not found" in result["error"] else 400
		raise HTTPException(status_code=status_code, detail=result["error"])

	# 2. Notify the Driver
	if trip.driver_user_id:
		try:
			# Call using the instance 'notif_service'
			await notif_service.notify_user(
				user_id=trip.driver_user_id,
				title="Trip Cancelled by Admin",
				message=f"Your trip on {trip.route.name} has been cancelled. Reason: {reason}",
				data={"type": "Trip_cancel_UPDATE"},
				commit=False,  # Don't commit yet
			)
		except Exception as e:
			print(f"Driver Notification failed: {e}")

	# 3. Notify all Passengers
	for booking in trip.bookings:
		try:
			# Call using the instance 'notif_service'
			await notif_service.notify_user(
				user_id=booking.passenger_id,
				title="Urgent: Trip Cancelled",
				message=f"Your ride for {trip.route.name} is cancelled. A refund has been initiated.",
				data={"type": "Trip_cancel_UPDATE"},
				commit=False,  # Don't commit yet
			)
		except Exception as e:
			print(
				f"Passenger Notification failed for {booking.passenger_id}: {e}"
			)

	# 4. Final single commit for the cancellation AND all notification records
	await db.commit()

	return {
		"status": "success",
		"message": "Trip cancelled and users notified.",
	}


# -----------------------------
# Admin: Cancel a Specific Trip Using Trip_id any time before trip ended
# -----------------------------
@router.post("/trips/{trip_id}/premature-end")
async def premature_end_trip(
	trip_id: str,
	request: Request,
	db: AsyncSession = Depends(get_async_session),
):
	hub = get_ws_hub(request)
	service = AdminService(db, ws_hub=hub)
	return await service.handle_premature_trip_end(trip_id)


# -----------------------------
# Admin: Get Trip Details using Trip_ID
# -----------------------------


# @router.get("/trips/{trip_id}")
# async def get_specific_trip_status(
# 	trip_id: str, db: AsyncSession = Depends(get_async_session)
# ):
# 	service = AdminService(db)
# 	trip = await service.get_trip_by_id(trip_id)

# 	if not trip:
# 		raise HTTPException(status_code=404, detail="Trip not found")


# 	return {
# 		"trip_id": trip.id,
# 		"status": trip.status,
# 		"route": {"name": trip.route.name, "code": trip.route.code},
# 		"assignment": {
# 			# FIX: Access .driver_profile.full_name
# 			"driver": trip.driver.driver_profile.full_name
# 			if trip.driver and trip.driver.driver_profile
# 			else "No Driver Assigned",
# 			"vehicle": trip.vehicle.registration_number,
# 		},
# 		"timing": {
# 			"planned_start": trip.planned_start_at,
# 			"actual_start": trip.actual_start_at,
# 			"planned_end": trip.planned_end_at,
# 			"actual_end": trip.actual_end_at,
# 		},
# 		"cancelation": {
# 			"cancellation_reason": trip.cancellation_reason if trip else "N/A",
# 			"premature_end_reason": trip.premature_end_reason
# 			if trip
# 			else "N/A",
# 		},
# 		"occupancy": {
# 			"total_bookings": len(trip.bookings),
# 			"passengers": [
# 				{
# 					# FIX: Access .passenger_profile.full_name
# 					"passenger_id": b.passenger.passenger_profile.user_id,
# 					"name": b.passenger.passenger_profile.full_name
# 					if b.passenger and b.passenger.passenger_profile
# 					else "Unknown Passenger",
# 					"status": b.booking_status,
# 					"pickup_stop_id": b.pickup_stop_id,
# 					"pickup_stop_name": b.pickup_stop.name
# 					if b.pickup_stop
# 					else None,
# 					"dropoff_stop_id": b.dropoff_stop_id,
# 					"dropoff_stop_name": b.dropoff_stop.name
# 					if b.dropoff_stop
# 					else None,
# 				}
# 				for b in trip.bookings
# 			],
# 		},
# 		"admin_note": trip.admin_note,
# 	}
@router.get("/trips/{trip_id}")
async def get_specific_trip_status(
	trip_id: str, db: AsyncSession = Depends(get_async_session)
):
	service = AdminService(db)
	trip = await service.get_trip_by_id(trip_id)

	if not trip:
		raise HTTPException(status_code=404, detail="Trip not found")

	passengers_data = []
	for booking in trip.bookings:
		# Safely access eagerly loaded relationships
		# These won't trigger lazy loading now
		pickup_stop_name = (
			booking.pickup_stop.name if booking.pickup_stop else None
		)
		dropoff_stop_name = (
			booking.dropoff_stop.name if booking.dropoff_stop else None
		)

		# Find drop scan event from eagerly loaded scan_events
		drop_scan = None

		# Scan events are already loaded via joinedload
		for scan in booking.scan_events:
			if scan.scan_type == schema.ScanType.DROP:
				drop_scan = scan
			elif scan.scan_type == schema.ScanType.BOARD:
				pass

		passenger_info = {
			"passenger_id": booking.passenger.passenger_profile.user_id,
			"name": booking.passenger.passenger_profile.full_name
			if booking.passenger and booking.passenger.passenger_profile
			else "Unknown Passenger",
			"status": booking.booking_status,
			# Booked locations
			"pickup_stop_id": booking.pickup_stop_id,
			"pickup_stop_name": pickup_stop_name,
			"dropoff_stop_id": booking.dropoff_stop_id,
			"dropoff_stop_name": dropoff_stop_name,
			# Actual drop location from scan event
			"actual_drop_stop_id": drop_scan.matched_stop_id
			if drop_scan
			else None,
			"actual_drop_stop_name": drop_scan.matched_stop.name
			if drop_scan and drop_scan.matched_stop
			else None,
			"actual_dropped_at": drop_scan.created_at if drop_scan else None,
		}
		passengers_data.append(passenger_info)

	PickupStop = aliased(schema.Stop)
	DropoffStop = aliased(schema.Stop)

	rfid_rides_result = await db.execute(
		select(
			schema.RFIDTripRide,
			schema.User,
			schema.PassengerProfile,
			schema.RFIDCard,
			PickupStop,
			DropoffStop,
		)
		.outerjoin(
			schema.User,
			schema.User.id == schema.RFIDTripRide.passenger_user_id,
		)
		.outerjoin(
			schema.PassengerProfile,
			schema.PassengerProfile.user_id == schema.RFIDTripRide.passenger_user_id,
		)
		.outerjoin(
			schema.RFIDCard,
			schema.RFIDCard.id == schema.RFIDTripRide.card_id,
		)
		.outerjoin(
			PickupStop,
			PickupStop.id == schema.RFIDTripRide.pickup_stop_id,
		)
		.outerjoin(
			DropoffStop,
			DropoffStop.id == schema.RFIDTripRide.dropoff_stop_id,
		)
		.where(schema.RFIDTripRide.scheduled_trip_id == trip.id)
		.order_by(schema.RFIDTripRide.boarded_at.asc())
	)

	rfid_passengers = [
		_serialize_admin_rfid_ride(
			ride=ride,
			passenger=passenger,
			passenger_profile=passenger_profile,
			card=card,
			pickup_stop=pickup_stop,
			dropoff_stop=dropoff_stop,
		)
		for (
			ride,
			passenger,
			passenger_profile,
			card,
			pickup_stop,
			dropoff_stop,
		) in rfid_rides_result.all()
	]

	return {
		"trip_id": trip.id,
		"status": trip.status,
		"route": {"name": trip.route.name, "code": trip.route.code},
		"assignment": {
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
			"premature_end_reason": trip.premature_end_reason
			if trip
			else "N/A",
		},
		"occupancy": {
			"total_bookings": len(trip.bookings),
			"passengers": passengers_data,
		},
		"rfid": {
			"reserved_seat_count": trip.rfid_reserved_seat_count,
			"total_rfid_rides": len(rfid_passengers),
			"passengers": rfid_passengers,
		},
		"admin_note": trip.admin_note,
	}


# -----------------------------
# Admin:Check All bookings Using Trip ID
# -----------------------------
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
		raise HTTPException(
			status_code=404, detail="Booking record not found."
		)

	return {
		"status": "success",
		"message": f"Booking {booking_id} marked as No-Show.",
	}


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
	await db.commit()
	return {"status": "batch_completed", "details": summary}


# ---------------- drivers ratings -------------------------
# -----------------------------
# Admin: Get all Drivers Ratings
# -----------------------------
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
	return {
		"message": "Incident resolved",
		"admin_note": updated_trip.admin_note,
	}


# -------------------- support sections ---------------------------
# -----------------------------
# Admin: Get All Support Queries
# -----------------------------
@router.get("/tickets")
async def list_tickets(
	status: schema.SupportStatus | None = None,
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


# -----------------------------
# Admin: Get All Support Queries
# -----------------------------
@router.post("/tickets/{ticket_id}/action")
async def handle_ticket(
	ticket_id: str,
	action: str,  # 'resolve' or 'reject'
	request: Request,
	note: str = Body(..., embed=True),
	current_admin: schema.User = Depends(get_current_admin),
	db: AsyncSession = Depends(get_async_session),
):
	hub = get_ws_hub(request)
	service = AdminService(db, ws_hub=hub)

	# 1. Create the instance of the NotificationService
	notif_service = NotificationService(db, ws_hub=hub)

	ticket = await service.resolve_ticket(
		ticket_id, current_admin.id, note, action
	)
	if not ticket:
		raise HTTPException(status_code=404, detail="Ticket not found")

	status_msg = "resolved" if action == "resolve" else "rejected"

	# 2. Use the instance 'notif_service' instead of the class 'NotificationService'
	try:
		await notif_service.notify_user(
			user_id=ticket.user_id,
			title=f"Support Ticket {status_msg.capitalize()}",
			message=f"Your ticket '{ticket.subject}' has been {status_msg}. Admin Note: {note}",
			data={"type": "TICKET_UPDATE"},
			commit=True,  # Keep it False so we commit everything once at the end
		)
	except Exception as e:
		# Log the error so the admin knows the DB updated but the alert failed
		print(f"Notification failed for ticket {ticket_id}: {e}")

	# 3. Final commit for both the ticket update and the notification record
	await db.commit()

	return {"message": f"Ticket {action}ed successfully"}


# app/users/endpoints/router.py
@router.post("/support/create")
async def create_ticket(
	subject: str = Body(...),
	description: str = Body(...),
	current_user: schema.User = Depends(get_current_user),
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminService(db, ws_hub=hub)
	new_ticket = schema.SupportTicket(
		user_id=current_user.id,
		subject=subject,
		description=description,
		status=schema.SupportStatus.PENDING,
	)
	db.add(new_ticket)

	await service.send_user_notification(
		db,
		current_user.id,
		"Ticket Received",
		f"Your ticket '{subject}' has been created and is pending review.",
		"TICKET_CREATED",
	)

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
			{
				"trip_id": t.id,
				"status": t.status,
				"started_at": t.actual_start_at,
			}
			for t in user.driven_trips[:10]  # Sliced in Python
		],
		"compliance_check": {
			"aadhaar": user.driver_profile.aadhaar_number
			if user.driver_profile
			else None,
			"license": user.driver_profile.driving_license_number
			if user.driver_profile
			else None,
			"vehicle_reg": user.vehicle.registration_number
			if user.vehicle
			else None,
			"verification": user.driver_profile.verification_status
			if user.driver_profile
			else "N/A",
		},
	}


# -------------------------  TRANSACTIONS DETAILS BY SPECIFIC USERS --------------------
# -----------------------------
# Admin: get Transaction history using user_id
# -----------------------------


@router.get("/{user_id}/transaction_history")
async def get_user_transaction_history(
	user_id: str,
	skip: int = 0,
	limit: int = 50,
	status: schema.BookingStatus = None,
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminService(db)
	bookings = await service.fetch_user_transaction_history(
		user_id, skip, limit, status
	)

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
					if (b.passenger and b.passenger.passenger_profile)
					else "N/A",
					"email": b.passenger.email if b.passenger else "N/A",
				},
				"trip_details": {
					"route_name": b.route.name if b.route else "N/A",
					"pickup": {
						"id": b.pickup_stop_id,
						"name": b.pickup_stop.name if b.pickup_stop else "N/A",
					},
					"dropoff": {
						"id": b.dropoff_stop_id,
						"name": b.dropoff_stop.name
						if b.dropoff_stop
						else "N/A",
					},
					"driver_name": b.scheduled_trip.driver.driver_profile.full_name
					if (
						b.scheduled_trip
						and b.scheduled_trip.driver
						and b.scheduled_trip.driver.driver_profile
					)
					else "Unknown",
				},
				"financials": {
					"total_fare": float(b.fare_amount),
					"commission_percent": float(b.commission_percent_snapshot),
					"admin_earned": float(b.commission_amount),
					"driver_payout": float(b.driver_payout_amount),
					"audit_passed": is_payout_correct,
				},
				"refund_info": {
					"is_refunded": b.booking_status
					in ["cancelled", "refunded"],
					"reason": getattr(
						b, "cancellation_reason", "No reason provided"
					),
					"cancelled_at": getattr(b, "cancelled_at", None),
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

	return {"user_id": user_id, "total_count": len(report), "data": report}


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
		raise HTTPException(
			status_code=404, detail="No rating found for this booking"
		)

	return {
		"trip_rating": rating.trip_rating,
		"driver_rating": rating.driver_rating,
		"review": rating.review_text,
		"passenger_name": rating.passenger.email,  # Or profile name
		"created_at": rating.created_at,
	}


# ------------------  GET BOOKINGS DETAILS BY USER ID ----------------------
# -----------------------------
# Admin: GET BOOKINGS DETAILS BY USER ID
# -----------------------------
@router.get("/user/{user_id}/bookings/detailed")
async def get_user_bookings_detailed(
	user_id: str, db: AsyncSession = Depends(get_async_session)
):
	service = AdminService(db)
	bookings = await service.fetch_user_bookings_with_details(user_id)

	if not bookings:
		return {"user_id": user_id, "total_bookings": 0, "history": []}

	history = []
	for b in bookings:
		history.append(
			{
				"booking_id": b.id,
				"date": b.created_at,
				"status": b.booking_status,
				"route": b.route.name if b.route else "Unknown Route",
				"stops": {
					"pickup": {
						"id": b.pickup_stop_id,
						"name": b.pickup_stop.name if b.pickup_stop else "N/A",
						"seq": b.pickup_sequence_no_snapshot,
					},
					"dropoff": {
						"id": b.dropoff_stop_id,
						"name": b.dropoff_stop.name
						if b.dropoff_stop
						else "N/A",
						"seq": b.dropoff_sequence_no_snapshot,
					},
				},
				"financials": {
					"fare": float(b.fare_amount),
					"payment_id": b.payments[0].razorpay_payment_id
					if b.payments
					else None,
					"payment_status": b.payments[0].status
					if b.payments
					else "unpaid",
				},
				# Refund & Cancellation Logic
				"audit_note": {
					"cancelled_by": b.cancelled_by
					if hasattr(b, "cancelled_by")
					else "N/A",
					"reason": b.cancellation_reason
					if hasattr(b, "cancellation_reason")
					else "N/A",
					"refund_status": b.transfer_status
					if b.booking_status == "cancelled"
					else "N/A",
				}
				if b.booking_status in ["cancelled", "refunded"]
				else None,
			}
		)

	return {
		"user_id": user_id,
		"total_bookings": len(history),
		"history": history,
	}


# ------------------------ ALL PASSENGERS DETAILS -------------------
# -----------------------------
# Admin: Get ALL Passengers Details
# -----------------------------
@router.get("/passengers")
async def get_all_passengers_full_detail(
	skip: int = 0,
	limit: int = 50,
	current_user: schema.User = Depends(get_current_active_user),
	db: AsyncSession = Depends(get_async_session),
):
	# 1. Authorization Check
	if current_user.role != schema.UserRole.ADMIN:
		raise HTTPException(status_code=403, detail="Unauthorized access")

	# 2. Fetch Data from Service
	service = AdminService(db)
	users = await service.fetch_complete_passenger_data(skip, limit)

	full_report = []

	for u in users:
		prof = u.passenger_profile
		# FIX: Using the correct relationship name from your schema.py
		bookings = u.passenger_bookings

		# Calculate metrics
		# Note: We filter for 'paid' status in the nested payments of each booking
		total_spent = sum(
			p.amount
			for b in bookings
			for p in b.payments
			if p.status == schema.BookingPaymentStatus.PAID
		)
		ride_count = len(bookings)

		# 3. Constructing the JSON Response
		# We handle 'None' for profile safely to prevent AttributeErrors
		full_report.append(
			{
				"account_info": {
					"user_id": u.id,
					"email": u.email,
					"is_active": u.is_active,
					"created_at": u.created_at,
				},
				"profile": {
					"name": prof.full_name if prof else "N/A",
					"profile_picture": prof.profile_picture_path
					if prof
					else None,
					# Note: These fields are not in your current DB schema for PassengerProfile
					"status": "Active" if u.is_active else "Inactive",
				},
				"usage_metrics": {
					"total_rides": ride_count,
					"total_spending": float(total_spent),
					"last_ride_date": bookings[0].created_at
					if ride_count > 0
					else None,
				},
				"recent_bookings": [
					{
						"booking_id": b.id,
						"status": b.booking_status,
						"route": b.route.name if b.route else "N/A",
						"pickup": b.pickup_stop.name
						if b.pickup_stop
						else "N/A",
						"dropoff": b.dropoff_stop.name
						if b.dropoff_stop
						else "N/A",
						"fare": float(b.fare_amount),
						"date": b.created_at,
					}
					for b in bookings[:5]  # Limit to 5 most recent
				],
			}
		)

	return {
		"status": "success",
		"count": len(full_report),
		"passengers": full_report,
	}


# -----------------------------------   AVAILABLE VEHICALS ---------------------
@router.get("/available_vehicles")
async def get_available_vehicles(
	current_user: schema.User = Depends(get_current_active_user),
	db: AsyncSession = Depends(get_async_session),
):
	"""
	Returns vehicles NOT currently in an active/scheduled trip,
	including the assigned driver's name.
	"""
	if current_user.role != schema.UserRole.ADMIN:
		raise HTTPException(status_code=403, detail="Unauthorized access")

	try:
		# 1. Subquery for vehicles currently busy
		# Using ScheduledTripStatus.IN_PROGRESS as defined in your schema
		busy_vehicle_ids = select(schema.ScheduledTrip.vehicle_id).where(
			schema.ScheduledTrip.status.in_(
				[
					schema.ScheduledTripStatus.SCHEDULED,
					schema.ScheduledTripStatus.IN_PROGRESS,
				]
			)
		)

		# 2. Query available vehicles with Driver and Profile loaded
		# We use joinedload to fetch the driver user and their profile in one go
		query = (
			select(schema.Vehicle)
			.options(
				joinedload(schema.Vehicle.driver).joinedload(
					schema.User.driver_profile
				)
			)
			.where(not_(schema.Vehicle.id.in_(busy_vehicle_ids)))
		)

		result = await db.execute(query)
		available_vehicles = result.scalars().all()

		# 3. Format Response
		return {
			"status": "success",
			"count": len(available_vehicles),
			"vehicles": [
				{
					"id": v.id,
					"registration_number": v.registration_number,
					"vehicle_name": v.vehicle_name,
					"vehicle_model": v.vehicle_model,
					"seat_count": v.seat_count,
					"has_ac": v.has_ac,
					"driver_info": {
						"user_id": v.driver_user_id,
						# Accessing nested relationship: Vehicle -> User -> DriverProfile
						"driver_name": v.driver.driver_profile.full_name
						if v.driver and v.driver.driver_profile
						else "No Profile Found",
						"driver_email": v.driver.email if v.driver else "N/A",
					},
					"is_active": v.is_active,
				}
				for v in available_vehicles
			],
		}

	except Exception as e:
		# It's better to log the full error 'e' internally
		raise HTTPException(
			status_code=500, detail=f"Database error: {str(e)}"
		)


# ---------------- RATING AND REVIEWS FOR DRIVERS ---------------------
@router.get("/reviews/drivers")
async def get_driver_reviews(
	driver_id: str | None = Query(
		None, description="Filter by specific Driver User ID"
	),
	min_rating: int = Query(1, ge=1, le=5),
	skip: int = 0,
	limit: int = 50,
	current_user: schema.User = Depends(get_current_active_user),
	db: AsyncSession = Depends(get_async_session),
):
	"""
	Fetches all ratings and reviews given by passengers to drivers.
	"""
	# 1. Authorization
	if current_user.role != schema.UserRole.ADMIN:
		raise HTTPException(status_code=403, detail="Admin access required")

	try:
		# 2. Build the Query
		# We load: Passenger (User -> Profile), Driver (User -> Profile), and the Trip/Route
		stmt = (
			select(schema.BookingRating)
			.options(
				joinedload(schema.BookingRating.passenger).joinedload(
					schema.User.passenger_profile
				),
				joinedload(schema.BookingRating.driver).joinedload(
					schema.User.driver_profile
				),
				joinedload(schema.BookingRating.scheduled_trip).joinedload(
					schema.ScheduledTrip.route
				),
			)
			.order_by(schema.BookingRating.created_at.desc())
		)

		# 3. Apply Filters
		if driver_id:
			stmt = stmt.where(schema.BookingRating.driver_user_id == driver_id)

		# Filtering by driver_rating (assuming field name from your schema logic)
		stmt = stmt.where(schema.BookingRating.driver_rating >= min_rating)

		# 4. Execute
		result = await db.execute(stmt.offset(skip).limit(limit))
		ratings = result.scalars().all()

		# 5. Format JSON Response
		review_list = []
		for r in ratings:
			review_list.append(
				{
					"rating_id": r.id,
					"trip_details": {
						"trip_id": r.scheduled_trip_id,
						"route_name": r.scheduled_trip.route.name
						if r.scheduled_trip and r.scheduled_trip.route
						else "N/A",
						"date": r.scheduled_trip.planned_start_at
						if r.scheduled_trip
						else None,
					},
					"passenger": {
						"name": r.passenger.passenger_profile.full_name
						if r.passenger and r.passenger.passenger_profile
						else "Anonymous",
						"email": r.passenger.email if r.passenger else "N/A",
					},
					"driver": {
						"user_id": r.driver_user_id,
						"name": r.driver.driver_profile.full_name
						if r.driver and r.driver.driver_profile
						else "Unknown Driver",
					},
					"feedback": {
						"rating": r.driver_rating,
						"comment": r.review_text,
						"created_at": r.created_at,
						"trip_ratings": r.trip_rating,
					},
				}
			)

		return {
			"status": "success",
			"count": len(review_list),
			"reviews": review_list,
		}

	except Exception as e:
		raise HTTPException(
			status_code=500, detail=f"Database error: {str(e)}"
		)


@router.get("/reviews/stats")
async def get_rating_summary(
	db: AsyncSession = Depends(get_async_session),
	current_user: schema.User = Depends(get_current_active_user),
):
	"""
	Returns quick stats like average rating across the platform.
	"""
	if current_user.role != schema.UserRole.ADMIN:
		raise HTTPException(status_code=403, detail="Unauthorized")

	# Calculate average rating
	stmt = select(func.avg(schema.BookingRating.driver_rating))
	result = await db.execute(stmt)
	avg_rating = result.scalar() or 0.0

	return {"average_platform_rating": round(float(avg_rating), 2)}


# -----------------------------
# Admin: Get all Transactions Details
# -----------------------------


@router.get("/transactions/all")
# async def get_all_transactions(
#     skip: int = 0,
#     limit: int = 50,
#     status: schema.BookingStatus = None,
#     db: AsyncSession = Depends(get_async_session),
# ):
#     service = AdminService(db)
#     bookings = await service.fetch_detailed_transactions(skip, limit, status)

#     report = []
#     for b in bookings:
#         # 1. Payout Consistency Calculation
#         audit_payout = b.fare_amount - b.commission_amount
#         is_payout_correct = audit_payout == b.driver_payout_amount
#         trip_status = b.scheduled_trip.status if b.scheduled_trip else "unknown"

#         report.append(
#             {
#                 "booking_id": b.id,
#                 "timestamp": b.created_at,
#                 "status": b.booking_status,
#                 "trip_overall_status": trip_status,
#                 "passenger": {
#                     "name": b.passenger.passenger_profile.full_name
#                     if (b.passenger and b.passenger.passenger_profile)
#                     else "N/A",
#                     "email": b.passenger.email if b.passenger else "N/A",
#                 },
#                 "trip_details": {
#                     "route_name": b.route.name if b.route else "N/A",
#                     "driver_name": b.scheduled_trip.driver.driver_profile.full_name
#                     if (b.scheduled_trip and b.scheduled_trip.driver)
#                     else "Unknown",
#                 },
#                 "financials": {
#                     "total_fare": float(b.fare_amount),
#                     "admin_earned": float(b.commission_amount),
#                     "driver_payout": float(b.driver_payout_amount),
#                     "audit_passed": is_payout_correct,
#                 },
#                 # NEW: Driver Transfer Data (from BookingTransfer table)
#                 "transfer_details": {
#                     "transfer_id": b.transfer.razorpay_transfer_id
#                     if b.transfer
#                     else None,
#                     "status": b.transfer.status if b.transfer else "not_initiated",
#                     "processed_at": b.transfer.processed_at if b.transfer else None,
#                     "failure_reason": b.transfer.failure_reason if b.transfer else None,
#                 },
#                 # NEW: Adjustments (from PayoutAdjustment table)
#                 "payout_adjustments": [
#                     {
#                         "type": adj.adjustment_type,
#                         "amount": float(adj.amount),
#                         "reason": adj.reason_text,
#                         "status": adj.decision_status,
#                     }
#                     for adj in b.originated_payout_adjustments
#                 ],
#                 "refund_info": {
#                     "is_refunded": b.booking_status in ["cancelled", "refunded"],
#                     "cancelled_at": b.cancelled_at,
#                     "reason": "Trip Ended Prematurely by Admin"
#                     if trip_status == "premature_end"
#                     else "Standard Cancellation",
#                 }
#                 if b.booking_status in ["cancelled", "refunded"]
#                 else None,
#                 "payment_gateway": [
#                     {
#                         "razorpay_order_id": p.razorpay_order_id,
#                         "razorpay_payment_id": p.razorpay_payment_id,
#                         "payment_status": p.status,
#                     }
#                     for p in b.payments
#                 ],
#             }
#         )

#     return {"total_count": len(report), "data": report}

# -----------------------------
# Admin: Get all Transactions details
# -----------------------------
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
					if (b.passenger and b.passenger.passenger_profile)
					else "N/A",
					"email": b.passenger.email if b.passenger else "N/A",
				},
				"trip_details": {
					"route_name": b.route.name if b.route else "N/A",
					"pickup": {
						"id": b.pickup_stop_id,
						"name": b.pickup_stop.name if b.pickup_stop else "N/A",
					},
					"dropoff": {
						"id": b.dropoff_stop_id,
						"name": b.dropoff_stop.name
						if b.dropoff_stop
						else "N/A",
					},
					"driver_name": b.scheduled_trip.driver.driver_profile.full_name
					if (
						b.scheduled_trip
						and b.scheduled_trip.driver
						and b.scheduled_trip.driver.driver_profile
					)
					else "Unknown",
				},
				"financials": {
					"total_fare": float(b.fare_amount),
					"commission_percent": float(b.commission_percent_snapshot),
					"admin_earned": float(b.commission_amount),
					"driver_payout": float(b.driver_payout_amount),
					"audit_passed": is_payout_correct,
				},
				"refund_info": {
					"is_refunded": b.booking_status
					in ["cancelled", "refunded"],
					"reason": getattr(
						b, "cancellation_reason", "No reason provided"
					),
					"cancelled_by": getattr(b, "cancelled_by", "N/A"),
					"cancelled_at": getattr(b, "cancelled_at", None),
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


# -----------------------------
# Admin: Get all Most Booked Routes
# -----------------------------
@router.get(
	"/analytics/most-booked-routes", response_model=list[dict], status_code=200
)
async def get_most_booked_routes(
	db: AsyncSession = Depends(get_async_session),
	current_admin: schema.User = Depends(get_current_admin),
):
	service = AdminService(db)
	try:
		# Call the service method
		stats = await service.get_top_booking_routes()

		return [
			{
				"route_id": row.route_id,
				"route_name": row.route_name,
				"total_bookings": row.total_bookings,
			}
			for row in stats
		]
	except Exception as e:
		print(f"Error fetching route analytics: {e}")
		raise HTTPException(
			status_code=500, detail="Failed to retrieve most booked routes"
		)


# -----------------------------
# Admin: Get all top Pick-up_Stops
# -----------------------------
@router.get(
	"/analytics/top-pickup-stops", response_model=list[dict], status_code=200
)
async def get_top_pickup_stops(
	db: AsyncSession = Depends(get_async_session),
	current_admin: schema.User = Depends(get_current_admin),
):
	service = AdminService(db)
	try:
		stats = await service.get_most_popular_pickup_stops()

		return [
			{
				# "route_id": row.route_id,
				# "route_name": row.route_name,
				"stop_id": row.stop_id,
				"stop_name": row.stop_name,
				"booking_count": row.booking_count,
			}
			for row in stats
		]
	except Exception as e:
		# Useful for catching those pesky attribute errors!
		print(f"Error fetching pickup analytics: {e}")
		raise HTTPException(
			status_code=500,
			detail="Failed to retrieve top pickup stops analytics",
		)


# -----------------------------
# Admin: Complete any trip when trip is currently in progress and all booked passengers are departure
# -----------------------------
@router.post("/trips/{trip_id}/complete-manually")
async def admin_complete_trip(
	trip_id: str,
	note: str = None,
	db: AsyncSession = Depends(get_async_session),
	# Ensure only admins can access this
	current_admin: schema.User = Depends(get_current_admin),
):
	service = AdminService(db)
	return await service.manually_complete_trip(
		trip_id, current_admin.id, note
	)


# -----------------------------
# Admin: Get all passengers details
# -----------------------------
@router.get("/{trip_id}/passengers", response_model=TripManifestResponse)
async def get_trip_passengers(
	trip_id: str,
	db: AsyncSession = Depends(get_async_session),
	# Add your admin authentication dependency here
	current_admin=Depends(get_current_admin),
):
	service = AdminService(db)

	# Check if trip exists first
	trip = await service.get_trip_by_id(trip_id)
	if not trip:
		raise HTTPException(status_code=404, detail="Trip not found")

	passengers = await service.get_trip_passenger_list(trip_id)

	return {
		"trip_id": trip_id,
		"total_bookings": len(passengers),
		"passengers": passengers,
	}


# -----------------------------
# Admin: Get all Details of bookings using booking_id
# -----------------------------
@router.get(
	"/booking/{booking_id}", response_model=BookingFullDetailsResponsee
)
async def get_specific_booking_details(
	booking_id: str,
	db: AsyncSession = Depends(get_async_session),
	current_admin=Depends(get_current_admin),
):
	service = AdminService(db)
	details = await service.get_booking_details(booking_id)

	if not details:
		raise HTTPException(status_code=404, detail="Booking record not found")

	return details


# -----------------------------
# Admin: Get in progress status of a trip using trip_id
# -----------------------------
@router.get("/trip/{trip_id}/status-only")
async def get_trip_status_only(
	trip_id: str,
	db: AsyncSession = Depends(get_async_session),
	current_admin=Depends(get_current_admin),
):
	"""
	Simple GET to check trip status without any updates
	"""
	stmt = (
		select(
			schema.ScheduledTrip,
			schema.Route.name.label("route_name"),
			schema.DriverProfile.full_name.label("driver_name"),
		)
		.join(
			schema.Route,
			schema.ScheduledTrip.route_id == schema.Route.id,
			isouter=True,
		)
		.join(
			schema.User,
			schema.ScheduledTrip.driver_user_id == schema.User.id,
			isouter=True,
		)
		.join(
			schema.DriverProfile,
			schema.User.id == schema.DriverProfile.user_id,
			isouter=True,
		)
		.where(schema.ScheduledTrip.id == trip_id)
	)

	result = await db.execute(stmt)
	row = result.first()

	if not row:
		raise HTTPException(status_code=404, detail="Trip not found")

	trip = row.ScheduledTrip

	return {
		"trip_id": trip.id,
		"status": trip.status.value,
		"is_in_progress": trip.status
		== schema.ScheduledTripStatus.IN_PROGRESS,
		"route_name": row.route_name,
		"driver_name": row.driver_name,
		"last_known_location": {
			"lat": float(trip.last_lat) if trip.last_lat else None,
			"lng": float(trip.last_lng) if trip.last_lng else None,
		},
		"planned_times": {
			"start": trip.planned_start_at,
			"end": trip.planned_end_at,
		},
		"actual_times": {
			"start": trip.actual_start_at,
			"end": trip.actual_end_at,
		},
		"last_updated": trip.updated_at,
	}


# ============================================================
# Admin payout management by Anubhab Dey
# ============================================================


@router.get("/payouts/settings")
async def get_payout_settings(db: AsyncSession = Depends(get_async_session)):
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
	linked_account_status: schema.LinkedAccountStatus | None = Query(
		default=None
	),
	is_payout_eligible: bool | None = Query(default=None),
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminService(db)
	return await service.list_driver_payout_profiles(
		linked_account_status=linked_account_status,
		is_payout_eligible=is_payout_eligible,
	)


@router.get("/payouts/drivers/{driver_user_id}")
async def get_driver_payout_profile(
	driver_user_id: str, db: AsyncSession = Depends(get_async_session)
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
	return await service.update_driver_payout_eligibility(
		driver_user_id, payload
	)


@router.get("/payouts/bookings")
async def list_payout_bookings(
	driver_user_id: str | None = Query(default=None),
	passenger_user_id: str | None = Query(default=None),
	booking_status: schema.BookingStatus | None = Query(default=None),
	transfer_status: schema.TransferStatus | None = Query(default=None),
	month: int | None = Query(default=None, ge=1, le=12),
	year: int | None = Query(default=None, ge=2000, le=2100),
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
	booking_id: str, db: AsyncSession = Depends(get_async_session)
):
	service = AdminService(db)
	return await service.get_payout_booking_detail(booking_id)


@router.post("/payouts/bookings/{booking_id}/adjustments")
async def create_payout_adjustment(
	booking_id: str,
	payload: PayoutAdjustmentCreateRequest,
	db: AsyncSession = Depends(get_async_session),
	current_admin: schema.User = Depends(get_current_admin),
):
	service = AdminService(db)
	return await service.create_payout_adjustment(
		booking_id=booking_id, admin_user_id=current_admin.id, payload=payload
	)


@router.get("/payouts/bookings/{booking_id}/adjustments")
async def list_booking_payout_adjustments(
	booking_id: str, db: AsyncSession = Depends(get_async_session)
):
	service = AdminService(db)
	return await service.list_booking_payout_adjustments(booking_id)


@router.get("/payouts/drivers/{driver_user_id}/open-adjustments")
async def list_driver_open_payout_adjustments(
	driver_user_id: str, db: AsyncSession = Depends(get_async_session)
):
	service = AdminService(db)
	return await service.list_driver_open_payout_adjustments(driver_user_id)


@router.patch("/payouts/adjustments/{adjustment_id}/decision")
async def update_payout_adjustment_decision(
	adjustment_id: str,
	payload: PayoutAdjustmentDecisionRequest,
	db: AsyncSession = Depends(get_async_session),
	current_admin: schema.User = Depends(get_current_admin),
):
	service = AdminService(db)
	return await service.update_payout_adjustment_decision(
		adjustment_id=adjustment_id,
		admin_user_id=current_admin.id,
		payload=payload,
	)


@router.post("/payouts/bookings/{booking_id}/trigger")
async def trigger_booking_payout(
	booking_id: str,
	payload: TriggerBookingPayoutRequest,
	db: AsyncSession = Depends(get_async_session),
	current_admin: schema.User = Depends(get_current_admin),
):
	service = AdminService(db)
	return await service.trigger_booking_payout(
		booking_id=booking_id,
		linked_account_id=payload.linked_account_id,
		require_completed=payload.require_completed,
		adjustments_to_apply=[
			{
				"adjustment_id": item.adjustment_id,
				"applied_amount": item.applied_amount,
			}
			for item in payload.adjustments_to_apply
		],
		applied_by_admin_id=current_admin.id,
	)


@router.post("/payouts/drivers/{driver_user_id}/trigger-monthly")
async def trigger_driver_monthly_payouts(
	driver_user_id: str,
	payload: TriggerDriverMonthlyPayoutRequest,
	db: AsyncSession = Depends(get_async_session),
	current_admin: schema.User = Depends(get_current_admin),
):
	service = AdminService(db)
	return await service.trigger_driver_monthly_payouts(
		driver_user_id=driver_user_id,
		month=payload.month,
		year=payload.year,
		linked_account_id=payload.linked_account_id,
		booking_items=payload.booking_items,
		applied_by_admin_id=current_admin.id,
	)


@router.post("/payouts/bulk-trigger")
async def trigger_bulk_payouts(
	payload: BulkPayoutTriggerRequest,
	db: AsyncSession = Depends(get_async_session),
	current_admin: schema.User = Depends(get_current_admin),
):
	service = AdminService(db)
	return await service.trigger_bulk_payouts(
		payload, applied_by_admin_id=current_admin.id
	)


@router.get("/payouts/transfers")
async def list_booking_transfers(
	driver_user_id: str | None = Query(default=None),
	status: schema.BookingTransferStatus | None = Query(default=None),
	month: int | None = Query(default=None, ge=1, le=12),
	year: int | None = Query(default=None, ge=2000, le=2100),
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminService(db)
	return await service.list_booking_transfers(
		driver_user_id=driver_user_id, status=status, month=month, year=year
	)


@router.get("/payouts/transfers/{transfer_id}")
async def get_booking_transfer_detail(
	transfer_id: str, db: AsyncSession = Depends(get_async_session)
):
	service = AdminService(db)
	return await service.get_booking_transfer_detail(transfer_id)


@router.get("/payouts/refunds")
async def list_refund_queue(db: AsyncSession = Depends(get_async_session)):
	service = AdminService(db)
	return await service.list_refund_queue()


@router.post("/payouts/refunds/{booking_id}/reconcile")
async def reconcile_cancelled_booking_refund(
	booking_id: str, db: AsyncSession = Depends(get_async_session)
):
	service = AdminService(db)
	return await service.reconcile_cancelled_booking_refund(booking_id)


@router.get("/payouts/dashboard", response_model=PayoutDashboardResponse)
async def get_payout_dashboard(db: AsyncSession = Depends(get_async_session)):
	service = AdminService(db)
	return await service.get_payout_dashboard()


@router.post("/payouts/drivers/{driver_user_id}/create-linked-account")
async def create_and_save_driver_linked_account(
	driver_user_id: str, db: AsyncSession = Depends(get_async_session)
):
	service = AdminService(db)
	return await service.create_and_save_driver_linked_account(driver_user_id)


@router.post("/payouts/drivers/{driver_user_id}/sync-linked-account")
async def sync_driver_linked_account(
	driver_user_id: str, db: AsyncSession = Depends(get_async_session)
):
	service = AdminService(db)
	return await service.sync_driver_linked_account(driver_user_id)


@router.get("/payouts/drivers/{driver_user_id}/linked-account/provider")
async def get_driver_linked_account_provider_detail(
	driver_user_id: str, db: AsyncSession = Depends(get_async_session)
):
	service = AdminService(db)
	return await service.get_driver_linked_account_provider_detail(
		driver_user_id
	)


@router.get("/commercial-rules")
async def list_commercial_rules(
	rule_type: str | None = Query(default=None),
	is_active: bool | None = Query(default=None),
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminService(db)
	return await service.list_commercial_rules(
		rule_type=rule_type, is_active=is_active
	)


@router.get("/commercial-rules/{rule_id}")
async def get_commercial_rule(
	rule_id: str, db: AsyncSession = Depends(get_async_session)
):
	service = AdminService(db)
	return await service.get_commercial_rule(rule_id)


@router.post("/commercial-rules")
async def create_commercial_rule(
	payload: CommercialRuleCreateRequest,
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminService(db)
	return await service.create_commercial_rule(payload)


@router.patch("/commercial-rules/{rule_id}")
async def update_commercial_rule(
	rule_id: str,
	payload: CommercialRuleUpdateRequest,
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminService(db)
	return await service.update_commercial_rule(rule_id, payload)


@router.delete("/commercial-rules/{rule_id}")
async def delete_commercial_rule(
	rule_id: str, db: AsyncSession = Depends(get_async_session)
):
	service = AdminService(db)
	return await service.delete_commercial_rule(rule_id)


@router.patch("/commercial-rules/{rule_id}/status")
async def set_commercial_rule_status(
	rule_id: str,
	payload: CommercialRuleStatusUpdateRequest,
	db: AsyncSession = Depends(get_async_session),
):
	service = AdminService(db)
	return await service.set_commercial_rule_active(rule_id, payload.is_active)
