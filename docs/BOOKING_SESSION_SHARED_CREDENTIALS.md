# Booking Session Shared OTP/QR Credentials

This feature makes one boarding credential represent the whole booking session.
It is intentionally backward-compatible with the old per-booking API shape.

## Core rule

For bookings created under a `booking_session_id`:

- `booking_sessions.otp` is the source-of-truth OTP.
- Every child `trip_bookings.otp` mirrors the same value for old FE/admin code.
- A QR generated from any child booking is scoped to the booking session, not the
  individual seat.
- A driver scan of that QR/OTP processes all currently eligible active seats in
  that booking session for that trip.

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

It now also returns group-aware fields:

```json
{
  "message": "Scan successful",
  "booking_id": "first-processed-booking-id",
  "booking_ids": ["booking-1", "booking-2"],
  "booking_session_id": "session-id",
  "seat_number": 4,
  "seat_numbers": [4, 5],
  "processed_count": 2,
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
`seat_numbers`. No FE-side grouping logic is required.

## Scan behavior

For a session credential:

1. The driver must own the trip.
2. The credential must belong to the same `trip_id`.
3. The backend expands the credential to active session bookings only:
   - `booked`
   - `boarded`
4. At pickup radius, the scan boards every `booked` seat in the session.
5. At an active drop stop, the scan drops every `boarded` seat in the session.
6. Cancelled/completed/missed seats are ignored by the group scan.

Mixed state is handled backend-side:

- If some seats are `booked` and some are already `boarded`, scanning at pickup
  boards the remaining `booked` seats.
- If some seats are `booked` and some are `boarded`, scanning at a valid active
  drop stop drops the `boarded` seats.
- A seat that was never boarded is not silently completed.

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
