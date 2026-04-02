from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import select, update
from sqlalchemy.orm import joinedload

from app.db import schema


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
                        radius_meters=150  # Default geofence for Kolkata IT parks
                    )
                    self.db.add(new_stop)
                
                count += 1
            except json.JSONDecodeError:
                print(f"Skipping invalid JSON line: {line}")
                continue
        
        await self.db.commit()
        return count


    async def create_route_with_sequence(self, name: str, code: str, stop_ids: list[str]):
        # 1. Create Route
        new_route = schema.Route(name=name, code=code)
        self.db.add(new_route)
        await self.db.flush() # Get ID without committing

        # 2. Create Sequence (The Ordered Playlist)
        for index, s_id in enumerate(stop_ids):
            rs = schema.RouteStop(
                route_id=new_route.id,
                stop_id=s_id,
                sequence_order=index + 1 
            )
            self.db.add(rs)
        
        await self.db.commit()
        return new_route
    
    async def get_all_trips(self, status=None):
        stmt = (
            select(schema.ScheduledTrip)
            .options(
                joinedload(schema.ScheduledTrip.route),
                joinedload(schema.ScheduledTrip.driver),
                joinedload(schema.ScheduledTrip.vehicle)
            )
            .order_by(schema.ScheduledTrip.planned_start_at.desc())
        )
        if status:
            stmt = stmt.where(schema.ScheduledTrip.status == status)
            
        result = await self.db.execute(stmt)
        return result.unique().scalars().all()

    async def cancel_trip(self, trip_id: str, reason: str):
        trip_stmt = select(schema.ScheduledTrip).where(schema.ScheduledTrip.id == trip_id)
        res = await self.db.execute(trip_stmt)
        trip = res.scalar_one_or_none()

        if trip:
            trip.status = schema.ScheduledTripStatus.CANCELLED
            trip.admin_note = reason
            # Important: You would usually trigger a refund logic here for all bookings
            await self.db.commit()
            return True
        return False
# @router.post("/stops",response_model=None)
# def create_stop(stop_data:stopCreate, db:Session=Depends(get_db)):
#     new_stop=schema.Stop(
#         name=stop_data.name,
#         lat=stop_data.lat,
#         lng=stop_data.lng,
#         radius_meters=stop_data.radius_meters
#     )
#     db.add(new_stop)
#     db.commit()
#     db.refresh(new_stop)
#     return new_stop

# @router.post("/routes")
# def create_route_with_stops(route_data:RouteCreate,db:Session=Depends(get_db)):
#     new_route=schema.Route(
#         name=route_data.name,
#         code=route_data.code
#     )
#     db.add(new_route)
#     db.flush()

#     for stop_items in route_data.stops:
#         route_stop=schema.RouteStop(
#             route_id=new_route.id,
#             stop_id=stop_items.stop_id,
#             sequence_no=stop_items.sequence_no,
#             boarding_allowed=stop_items.bording_allowed,
#             deboarding_allowed=stop_items.debording_allowed
#         )
#         db.add(route_stop)

#     try:
#         db.commit()
#     except Exception as e:
#         db.rollback()
#         raise HTTPException(status_code=400,detail=str(e))
#     return {"message": "Route and Stops created successfully", "route_id": new_route.id}


# @router.get("/routes/{route_id}")
# def get_route_details(route_id: str, db: Session = Depends(get_db)):
#     """Fetch a route and its ordered stops"""
#     route = db.query(schema.Route).filter(schema.Route.id == route_id).first()
#     if not route:
#         raise HTTPException(status_code=404, detail="Route not found")

#     return {
#         "route_name": route.name,
#         "code": route.code,
#         "stops": [
#             {
#                 "stop_name": rs.stop.name,
#                 "sequence": rs.sequence_no,
#                 "lat": rs.stop.lat,
#                 "lng": rs.stop.lng
#             } for rs in route.route_stops
#         ]
#     }
