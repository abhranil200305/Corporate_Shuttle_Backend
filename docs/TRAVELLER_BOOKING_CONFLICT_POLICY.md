# Traveller Booking Conflict Policy

This feature allows one passenger account to book multiple journeys while
preventing one physical traveller from being booked into journeys they cannot
take at the same time.

It applies to both passenger booking APIs:

- `POST /passenger/booking-sessions`
- `POST /passenger/bookings`

The booking owner, route, vehicle, and scheduled trip are not the conflict
identity. The selected traveller is.

## Final rules

| Existing booking | Requested booking | Result |
|---|---|---|
| Different travellers | Any overlapping journey | Allowed |
| Same traveller, same trip, overlapping route legs | Any seat | Rejected |
| Same traveller, same trip, adjacent legs such as `[A,B)` and `[B,C)` | Any available seats | Allowed |
| Same traveller, same trip, disjoint route legs | Any available seats | Allowed |
| Same traveller, different trips, overlapping planned leg times | Same or different route/bus | Rejected |
| Same traveller, different trips, first dropoff and second pickup at the same stop and times only touch | Any bus/route | Allowed |
| Same traveller, different trips, different transfer stops with less than 15 minutes between legs | Any bus/route | Rejected |
| Same traveller, different trips, different transfer stops with at least 15 minutes between legs | Any bus/route | Allowed |

Route legs are half-open intervals. `[A,B)` and `[B,C)` do not overlap because
the first leg releases the traveller and seat at `B` before the second leg
starts.

The 15-minute transfer buffer is a backend constant named
`DEFAULT_TRANSFER_BUFFER_MINUTES`. Frontends must not duplicate this logic to
decide whether a booking is valid. The create API is authoritative.

## Which booking states block another booking

These states participate in conflict checks:

- `pending_payment` while `payment_hold_expires_at` is null or in the future
- `booked`
- `boarded`

These states do not block another booking:

- expired `pending_payment`
- `cancelled`
- `completed`
- `missed`
- any other non-active terminal state

An expired payment hold is ignored even if a cleanup job has not yet changed
its status.

## Traveller identity

`trip_bookings.traveller_identity_key` is an internal, immutable booking
snapshot. It is intentionally not returned in passenger API responses.

Identity is generated as follows:

- Booking for the account owner: `self:<owner_user_id>`
- Selecting a saved self profile: the same `self:<owner_user_id>` key
- Selecting another saved profile: `profile:<traveller_profile_id>`
- Entering an ad-hoc guest: `guest:<owner_user_id>:<sha256(normalized_phone)>`

Guest phone normalization removes non-digits. Formatting differences such as
`+91 98765-43210` and `919876543210` therefore resolve to the same guest for
the same booking owner. The phone itself is not embedded in the identity key.

Guest identities are owner-scoped. The backend does not claim that matching
phone numbers across two different passenger accounts represent the same
physical person.

If an ad-hoc guest phone matches any saved traveller profile owned by the same
account, the API rejects the guest input with
`guest_matches_saved_traveller` and returns the profile ID. The frontend should
select that profile and retry. If the profile is inactive, the frontend should
reactivate it first. This prevents deactivating a profile or switching between
saved-profile and guest input to bypass a conflict.

## Time calculation

For different scheduled trips, the backend compares only the booked leg, not
the entire route's `planned_start_at` to `planned_end_at` window.

The planned time at a route stop is calculated from:

1. `scheduled_trips.planned_start_at` at the first route stop.
2. The cumulative `route_stops.assume_time_diff_minutes` for later stops.

The requested journey window is:

```text
[planned time at pickup stop, planned time at dropoff stop)
```

If an existing booking remains `boarded` after its planned dropoff time, its
effective end is extended to at least the current time for the conflict check.

The policy uses planned stop times; it does not continuously rewrite bookings
when a vehicle is delayed. That is a deliberate first implementation boundary.

## Booking-session behavior

Every seat is resolved to a traveller before rows or a payment order are
created.

The same traveller cannot be assigned to two seats in one request, including:

- self plus a saved profile marked `is_self`
- the same saved profile on two seats
- two guest payloads whose normalized phones match

That request returns HTTP `409` with:

```json
{
  "detail": {
    "error": "duplicate_traveller_in_booking_session",
    "message": "The same traveller cannot occupy multiple seats in one booking session.",
    "seat_number_groups": [[2, 5]]
  }
}
```

Different traveller profiles under the same owner account may be booked into
overlapping journeys. They have different traveller identity keys.

## Conflict response contract

An existing active booking conflict returns HTTP `409`:

```json
{
  "detail": {
    "error": "traveller_booking_conflict",
    "message": "This traveller already has an active booking that conflicts with the requested journey.",
    "seat_number": 3,
    "conflicting_booking_id": "booking-uuid",
    "conflicting_scheduled_trip_id": "trip-uuid",
    "conflict_type": "overlapping_trip_window",
    "transfer_buffer_minutes": 15
  }
}
```

`conflict_type` is one of:

- `overlapping_route_segment`: overlapping legs on the same scheduled trip
- `overlapping_trip_window`: booked leg times overlap across scheduled trips
- `insufficient_transfer_time`: leg times do not overlap, but a different-stop
  transfer has less than 15 minutes

When guest input matches a saved profile, HTTP `409` is:

```json
{
  "detail": {
    "error": "guest_matches_saved_traveller",
    "message": "This phone belongs to a saved traveller. Use or reactivate that traveller profile instead of entering guest details.",
    "seat_number": 3,
    "traveller_profile_id": "profile-uuid",
    "traveller_profile_is_active": false
  }
}
```

## Frontend integration

No new request fields are required. Existing self, saved-profile, and guest
payloads remain valid.

Frontend behavior should be:

1. Submit the existing booking request normally.
2. Treat the create API response as authoritative.
3. For `traveller_booking_conflict`, display `detail.message`. The optional IDs
   can be used to link to the conflicting booking if that surface exists.
4. For `duplicate_traveller_in_booking_session`, highlight every seat number in
   each `seat_number_groups` entry.
5. For `guest_matches_saved_traveller`, reactivate the profile first when
   `detail.traveller_profile_is_active` is false, replace the guest editor
   selection with `detail.traveller_profile_id`, then let the user retry.
6. Do not calculate overlap, stop timing, active status, or transfer buffers in
   the frontend. A client-side warning is fine, but it must never override the
   backend result.

Existing seat-capacity errors such as `seat_unavailable` and
`trip_segment_full` remain unchanged.

## Legacy single-seat endpoint

`POST /passenger/bookings` remains idempotent for the exact same self journey
and seat. Repeating that request returns the existing booking/payment state.

Its previous blanket "one booking per passenger per scheduled trip" rule is
removed. The endpoint now permits adjacent and disjoint legs on the same trip
and applies the same traveller conflict policy as booking sessions.

A pending booking that belongs to a booking session is not reused as a legacy
single-seat payment attempt. It is evaluated as an existing traveller conflict.

## Concurrency protection

Conflict validation and row creation run in the same database transaction.

On PostgreSQL, the backend acquires transaction-scoped advisory locks derived
from each traveller identity key, in sorted order. It then locks matching
active booking rows and checks conflicts. Concurrent requests for the same
traveller therefore cannot both pass validation before either row is visible.

Existing scheduled-trip row locking continues to protect seat allocation and
capacity checks.

## Database migration

Revision `4b8c2d1e7f90` adds:

- non-null `trip_bookings.traveller_identity_key`
- `ck_trip_bookings_traveller_identity_nonempty`
- index `ix_trip_bookings_traveller_identity_status`

Backfill rules are:

- saved self profile or `Self` snapshot -> canonical self identity
- saved non-self profile -> profile identity
- old legacy single-seat booking -> canonical self identity
- old ad-hoc booking-session guest -> row-local `legacy:<booking_id>` identity

Historical ad-hoc guests receive row-local keys because the migration avoids
turning raw phone data into a portable database-side hash. Newly created guests
receive stable hashed identities. Therefore, conflict matching between two old
guest rows is not retroactively guaranteed; all new requests are protected.

Deploy the migration before deploying application code:

```bash
alembic upgrade head
```

The application code expects the new non-null column to exist.

## Verification matrix

At minimum, QA should cover:

1. Self books `[A,B)`, then self books `[B,C)` on the same trip: allowed.
2. Self books `[A,C)`, then self books `[B,D)` on the same trip: rejected.
3. Self books two disjoint same-trip legs with different seats: allowed.
4. Saved traveller X and saved traveller Y book overlapping legs: allowed.
5. Saved traveller X books overlapping legs on different buses: rejected.
6. Same traveller transfers at the same stop at an exact time boundary:
   allowed.
7. Same traveller transfers at different stops with 14 minutes: rejected.
8. Same traveller transfers at different stops with 15 minutes: allowed.
9. Unexpired pending payment conflicts; expired pending payment does not.
10. Cancelled/completed/missed bookings do not conflict.
11. Omitted self and a saved `is_self` profile in one request: rejected as a
    duplicate traveller.
12. Guest phone formatting variants in one request: rejected as a duplicate.
13. Guest phone matching a saved profile: `guest_matches_saved_traveller`.
14. Two simultaneous requests for the same traveller: only one conflicting
    booking succeeds.

## Known identity boundary

The system can reliably identify self, a saved traveller profile, and a guest
within one booking-owner account. It cannot safely deduplicate the same person
across separate user accounts without a verified global traveller identity.
Phone-only cross-account matching would create false positives for shared or
recycled phone numbers and is intentionally not used.
