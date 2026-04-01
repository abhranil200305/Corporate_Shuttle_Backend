from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.db import schema
from sqlalchemy import update


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
            joinedload(schema.User.driver_profile)
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
            schema.User.id == user_id, 
            schema.User.role == schema.UserRole.DRIVER
        )
        .values(is_active=active)
    )
    
        await self.db.execute(stmt)
        await self.db.commit()
        return True
    
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
