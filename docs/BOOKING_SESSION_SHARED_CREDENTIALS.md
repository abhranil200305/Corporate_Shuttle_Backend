# Booking Session Shared OTP/QR Credentials

This feature makes one boarding credential represent the whole booking session.
It is intentionally backward-compatible with the old per-booking API shape.

## Core rule

For bookings created under a `booking_session_id`:

- `booking_sessions.otp` is the source-of-truth OTP.
- Every child `trip_bookings.otp` mirrors the same value for old FE/admin code.
- A QR generated from any child booking is scoped to the booking session, not the
  individual seat.
- A driver scan of that QR/OTP processes exactly one active seat. If the
  credential identifies a multi-seat session without a target seat, the backend
  asks the driver client to send `booking_id` or `seat_number`.

Legacy single-seat bookings with `booking_session_id = null` continue to behave
as booking-level credentials.

## Passenger FE contract

### Existing fields still work

All existing seat payloads still include:

```json
{
  "id": "booking-id",
  "booking_session_id": "session-id",
  "seat_number": 12,
  "otp": "123456"
}
```

For a booking session, every active seat in `bookings[]` will have the same
`otp`.

### Preferred session field

Booking session responses now also include:

```json
{
  "id": "session-id",
  "otp": "123456",
  "bookings": []
}
```

FE should prefer `booking_session.otp` when rendering a grouped booking session.
If a screen still reads `booking.otp`, it will continue to work.

### QR endpoint

The existing endpoint remains:

```http
GET /passenger/bookings/{booking_id}/qr
```

For a session booking, the response is:

```json
{
  "booking_id": "child-booking-id-that-was-requested",
  "booking_session_id": "session-id",
  "credential_scope": "booking_session",
  "qr_token": "...",
  "payload": {
    "credential_scope": "booking_session",
    "scheduled_trip_id": "trip-id",
    "booking_session_id": "session-id",
    "booking_id": "child-booking-id-that-was-requested",
    "issued_at": 1783410000,
    "expires_at": 1783453200
  }
}
```

For a legacy single booking, the response remains booking-scoped:

```json
{
  "booking_id": "booking-id",
  "booking_session_id": null,
  "credential_scope": "booking",
  "qr_token": "...",
  "payload": {
    "credential_scope": "booking",
    "scheduled_trip_id": "trip-id",
    "booking_id": "booking-id",
    "issued_at": 1783410000,
    "expires_at": 1783453200
  }
}
```

FE does not need to decode `qr_token`. Render the token as QR.

## Driver scan contract

Existing endpoints remain:

```http
POST /driver/scan/{trip_id}/scan
POST /driver/otp/{trip_id}/verify
```

A successful scan still returns legacy keys like `booking_id`, `seat_number`,
`scan_type`, `booking_status`, and `matched_stop_id`.

It now also returns target-aware fields:

```json
{
  "message": "Scan successful",
  "booking_id": "processed-booking-id",
  "booking_ids": ["processed-booking-id"],
  "booking_session_id": "session-id",
  "seat_number": 4,
  "seat_numbers": [4],
  "processed_count": 1,
  "scan_type": "board",
  "distance_meters": 7.21,
  "booking_status": "boarded",
  "booking_statuses": {
    "booking-1": "boarded",
    "booking-2": "boarded"
  },
  "matched_stop_id": "stop-id"
}
```

Driver FE can show a simple success message using `processed_count` and
`seat_numbers`.

Manual OTP scans for a multi-seat session should send one target:

```json
{
  "otp_code": "123456",
  "seat_number": 4,
  "lat": 22.57,
  "lng": 88.36
}
```

`booking_id` can be sent instead of `seat_number`.

## Scan behavior

For a session credential:

1. The driver must own the trip.
2. The credential must belong to the same `trip_id`.
3. The backend considers active session bookings only:
   - `booked`
   - `boarded`
4. If a concrete target is present, only that target booking is mutated.
5. If multiple active seats exist and no target is present, the backend returns
   `session_credential_requires_seat_selection`.
6. At pickup radius, the target `booked` seat is boarded.
7. At an active drop stop, the target `boarded` seat is dropped. This preserves
   the existing early-drop behavior: the active stop may be before the booked
   drop stop as long as it is after pickup and not after the booked drop stop.
8. Cancelled/completed/missed seats are ignored and cannot be selected.

Mixed state is handled safely:

- If some seats are `booked` and some are already `boarded`, the selected seat's
  own status decides whether the scan is a board or drop.
- A seat that was never boarded is not silently completed.
- Individual seat cancellation is safe because cancelled seats are not active
  scan candidates.
- `dropoff_stop` remains the booked/planned drop stop.
- `actual_drop_stop` is the scanned stop where the passenger actually got off.
  For an early drop, these two fields are intentionally different.

Ambiguous multi-seat OTP response:

```json
{
  "error": "session_credential_requires_seat_selection",
  "message": "This shared session credential has multiple active seats. Pass booking_id or seat_number to scan exactly one seat.",
  "booking_session_id": "session-id",
  "eligible_bookings": [
    {
      "booking_id": "booking-1",
      "seat_number": 4,
      "booking_status": "booked",
      "traveller_name": "Passenger"
    }
  ]
}
```

## OTP collision handling

New booking/session OTPs are generated uniquely within active credentials for
the scheduled trip. On PostgreSQL, generation is serialized with a trip-level
advisory lock.

If an old/manual data collision still makes one OTP match more than one active
booking group for the same trip, the driver OTP endpoint returns:

```json
{
  "error": "ambiguous_booking_otp",
  "message": "This OTP matches more than one active booking group for this trip. Ask the passenger to refresh and show the QR, or contact support."
}
```

QR is signed and does not have this ambiguity.

## Migration behavior

Migration `8f6a1c2d9b30_add_booking_session_shared_otp.py`:

1. Adds nullable `booking_sessions.otp`.
2. Backfills each existing session from its first child booking OTP.
3. Mirrors that session OTP back onto all child `trip_bookings.otp`.
4. Adds an active trip/session OTP uniqueness index on
   `(scheduled_trip_id, otp)` for booking sessions.

Run:

```bash
alembic upgrade head
```

before deploying the application code.

## Admin contract

Admin booking-session list/detail responses now include session-level `otp`.
The detail response also includes each child booking `otp`; for session bookings
those values mirror the session OTP.
