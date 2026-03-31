from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.admin.structs.dto import RouteCreate, stopCreate
from app.db.database import get_db
from app.db import schema
import uuid

router = APIRouter(prefix="/admin", tags=["Admin - Route Management"])


@router.post("/stops",response_model=None)
def create_stop(stop_data:stopCreate, db:Session=Depends(get_db)):
    new_stop=schema.Stop(
        name=stop_data.name,
        lat=stop_data.lat,
        lng=stop_data.lng,
        radius_meters=stop_data.radius_meters
    )
    db.add(new_stop)
    db.commit()
    db.refresh(new_stop)
    return new_stop

@router.post("/routes")
def create_route_with_stops(route_data:RouteCreate,db:Session=Depends(get_db)):
    new_route=schema.Route(
        name=route_data.name,
        code=route_data.code
    )
    db.add(new_route)
    db.flush()

    for stop_items in route_data.stops:
        route_stop=schema.RouteStop(
            route_id=new_route.id,
            stop_id=stop_items.stop_id,
            sequence_no=stop_items.sequence_no,
            boarding_allowed=stop_items.bording_allowed,
            deboarding_allowed=stop_items.debording_allowed
        )
        db.add(route_stop)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400,detail=str(e))
    return {"message": "Route and Stops created successfully", "route_id": new_route.id}


@router.get("/routes/{route_id}")
def get_route_details(route_id: str, db: Session = Depends(get_db)):
    """Fetch a route and its ordered stops"""
    route = db.query(schema.Route).filter(schema.Route.id == route_id).first()
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    
    return {
        "route_name": route.name,
        "code": route.code,
        "stops": [
            {
                "stop_name": rs.stop.name,
                "sequence": rs.sequence_no,
                "lat": rs.stop.lat,
                "lng": rs.stop.lng
            } for rs in route.route_stops
        ]
    }
