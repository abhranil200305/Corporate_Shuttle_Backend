#app/driver/rfid/schemas.py
from pydantic import BaseModel


class DriverRFIDReservationSettingResponse(BaseModel):
    allow_driver_rfid_seat_reservation: bool