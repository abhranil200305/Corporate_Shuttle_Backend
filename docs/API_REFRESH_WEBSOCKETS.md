# API refresh WebSockets: architecture and migration guide

This document explains the complete feature at system level. The frontend
handoff is split into two standalone guides:

- [Passenger integration](PASSENGER_API_REFRESH_WEBSOCKET.md)
- [Driver integration](DRIVER_API_REFRESH_WEBSOCKET.md)

Each role guide contains its own connection code, event table, API-invalidation
mapping, UI behavior, and acceptance checklist. A developer implementing only
one application does not need to read the other role guide.

## 1. Executive contract

The backend now exposes two authenticated event channels:

| Client | WebSocket path | Required token role |
| --- | --- | --- |
| Passenger | `/passenger/ws/refresh?token=<access_token>` | `passenger` |
| Driver | `/driver/ws/refresh?token=<access_token>` | `driver` |

These channels do not stream complete domain objects. They tell a client that
server state changed and identify the resource groups and API patterns that may
now be stale. HTTP APIs remain the source of truth.

The minimum correct frontend behavior is:

1. Open exactly the socket matching the logged-in user's role.
2. Reply to every server `ping` with `pong`.
3. On every `api.refresh`, invalidate/refetch the listed active resources.
4. Reconnect after network or server disconnects.
5. Treat the reconnect-time `channel.connected` event as a mandatory initial
   resynchronization.
6. Never infer that an action succeeded solely from a WebSocket event; mutation
   API responses remain authoritative.

## 2. Why this is a separate feature

Before this feature, the backend already had two real-time mechanisms. They
remain in place and have different jobs.

| Channel | Path | Purpose | Persistent? | Carries current domain state? |
| --- | --- | --- | --- | --- |
| User notifications | `/notifications/ws?token=...` | Human-facing alerts such as “Trip started” or “Bus arrived” and notification read state | Yes, when the notification is persisted | No |
| Passenger seat map | `/passenger/seatmap/ws?token=...` | Topic-specific, authoritative seat availability snapshots for one trip and leg | No | Yes, for the subscribed seat-map topic |
| Passenger API refresh | `/passenger/ws/refresh?token=...` | Invalidates passenger route, trip, booking, status, and RFID API state | No | No |
| Driver API refresh | `/driver/ws/refresh?token=...` | Invalidates driver route, current-trip, manifest, and RFID state; signals action eligibility | No | No |

The new sockets supplement the old ones; they do not replace them.

### 2.1 Passenger application connection set

A logged-in passenger normally keeps these connections:

- `/notifications/ws` for notification-center content and human-readable
  alerts.
- `/passenger/ws/refresh` for application-wide cache invalidation.
- `/passenger/seatmap/ws` only while a seat selection experience needs live,
  leg-specific seat snapshots. Subscribe when entering the seat page and
  unsubscribe or disconnect when leaving it.

### 2.2 Driver application connection set

A logged-in driver normally keeps these connections:

- `/notifications/ws` for human-readable notifications.
- `/driver/ws/refresh` for route/trip/manifest refreshes and start/departure
  action eligibility.

There is no driver seat-map socket.

## 3. Authentication and transport

The token is the `access_token` returned by `POST /auth/login` or
`POST /auth/signup`. The backend validates the same persisted session used by
Bearer-authenticated HTTP APIs.

Browser connection:

```text
wss://api.example.com/passenger/ws/refresh?token=<URL-encoded-access-token>
wss://api.example.com/driver/ws/refresh?token=<URL-encoded-access-token>
```

Native/non-browser clients may omit the query token and send:

```http
Authorization: Bearer <access_token>
```

The browser WebSocket API cannot set an arbitrary Authorization header, so the
query parameter is the supported browser mechanism. Always use `wss://` in an
HTTPS environment and avoid logging full WebSocket URLs because they contain a
session credential.

Missing, invalid, expired, logged-out, or role-mismatched credentials are
rejected with WebSocket close code `1008`. A passenger token cannot open the
driver channel, and a driver token cannot open the passenger channel.

## 4. Connection protocol

### 4.1 Successful connection sequence

The server sends these messages in order:

```json
{
  "type": "ws.ready",
  "channel": "passenger",
  "user_id": "user-uuid",
  "message": "API refresh WebSocket authenticated."
}
```

```json
{
  "type": "api.refresh",
  "event": "channel.connected",
  "audience": "passenger",
  "resources": ["routes", "scheduled_trips", "bookings", "booking_sessions"],
  "endpoints": [
    "/passenger/routes",
    "/passenger/scheduled-trips",
    "/passenger/bookings/current",
    "/passenger/booking-sessions/current"
  ],
  "data": {"reason": "initial_sync"},
  "occurred_at": "2026-07-03T06:30:00+00:00"
}
```

Driver connections can then receive an immediate `trip.start_allowed` or
`trip.departure_allowed` if the currently persisted trip state already permits
that action. This reconnect check prevents a missed timer event from leaving a
button stale.

### 4.2 Heartbeat

- Server sends `{"type":"ping"}` every 15 seconds.
- Client must immediately send `{"type":"pong"}`.
- A connection with no recorded pong for 30 seconds is closed with code `1001`.
- Client may send `{"type":"ping"}`; server replies with
  `{"type":"pong"}`.
- No application-level acknowledgement is required for `api.refresh` events.

### 4.3 Common event envelope

```ts
type RefreshAudience = "passenger" | "driver";

interface ApiRefreshMessage {
  type: "api.refresh";
  event: string;
  audience: RefreshAudience;
  resources: string[];
  endpoints: string[];
  data: Record<string, unknown>;
  occurred_at: string; // UTC ISO-8601
}
```

Field meanings:

| Field | Meaning |
| --- | --- |
| `event` | The domain fact that caused invalidation. Use this for special UI behavior such as enabling the driver's Depart button. |
| `audience` | Always matches the socket role. Reject/ignore a mismatched message defensively. |
| `resources` | Stable logical cache groups. This is the preferred input to the frontend invalidation layer. |
| `endpoints` | Human-readable primary API path patterns affected by the event. Braced IDs are placeholders, not literal URLs. Related detail/derived queries may be omitted; `resources` is the canonical invalidation input. |
| `data` | IDs and event metadata. Keys vary by event. Nullable IDs may be present. |
| `occurred_at` | Server emission time, not necessarily the underlying database row's update time. |

## 5. Delivery model and required client assumptions

The hub is deliberately an invalidation bus, not a durable event log.

- Delivery is best effort and in-memory.
- Events are not persisted and are not replayed.
- A disconnected client can miss events.
- Multiple devices/sockets for the same user are supported; targeted events go
  to every currently connected device for that user.
- Broadcast events go to every currently connected user of that role.
- Per-connection writes are serialized, but frontend correctness must not rely
  on a global event order across independent backend requests.
- Duplicate invalidations are safe and may occur. Coalesce them for roughly
  50–200 ms and refetch the latest state once.
- HTTP responses are authoritative. WebSocket payloads are hints and IDs, not
  replacements for API responses.
- `channel.connected` closes the replay gap: it instructs the client to refresh
  its important current state after every initial connection or reconnect.

## 6. Server-side event production

The following committed mutations produce events.

### 6.1 Route and stop catalog

Admin operations broadcast `route.created` or `route.updated` to both roles:

- Route identity creation.
- Stop bulk upload.
- Single stop creation/update.
- Unused stop deletion.
- Stops appended to a route.
- Route activation/deactivation.
- Route fare creation/update.

### 6.2 Trip catalog and lifecycle

- Driver trip creation emits `trip.created`, schedules the start-window event,
  and refreshes passenger trip discovery.
- Entering the planned start window emits `trip.start_allowed` to the assigned
  driver.
- Successful start emits `trip.started` to the driver and booked passengers,
  plus `trip.catalog_changed` to all passengers.
- Successful arrival/departure emits `trip.stop_arrived` or
  `trip.stop_departed` to the driver and booked passengers.
- Successful normal completion emits `trip.completed` and a passenger catalog
  refresh.
- Driver/admin/automatic cancellation emits `trip.cancelled` and a passenger
  catalog refresh.
- Driver/admin premature end emits `trip.premature_ended` and a passenger
  catalog refresh.
- Admin manual completion emits `trip.completed` and a passenger catalog
  refresh.

### 6.3 Booking and seat state

Booking creation, payment verification, session cancellation, individual seat
cancellation, legacy booking cancellation, admin no-show, and material payment
reconciliation outcomes produce:

- Private `booking.changed` to the booking owner and assigned driver.
- Broadcast `trip.seat_availability_changed` to passenger clients. This event
  intentionally contains no private booking identifier.

The existing passenger seat-map hub also continues pushing full
`seat_map.snapshot` messages to subscribers for matching trip/leg topics.

### 6.4 Passenger scans and RFID

- Accepted QR and OTP board/drop scans emit private
  `passenger.scan_completed` to that passenger and the assigned driver.
- A successful normal drop scan re-evaluates driver departure eligibility.
- Accepted RFID scans emit private `rfid.scan_completed` to the scanned
  passenger and assigned driver.
- Accepted RFID scans also broadcast `trip.rfid_occupancy_changed` to
  passengers so RFID discovery/availability screens can refresh.
- Rejected RFID scans do not produce refresh events.

### 6.5 Background jobs

- The payment reconciliation loop emits booking/seat invalidations when a
  pending booking materially changes status. Still-pending outcomes do not emit.
- The unstarted-trip canceller emits `trip.cancelled` after the start grace
  window expires and the trip is automatically cancelled.
- Existing notification and seat-map emissions from those jobs remain active.

### 6.6 Current coverage boundaries

The refresh catalog is exhaustive for the route/trip/booking/scan flows listed
above, but it is not a universal change-data-capture system. The following
areas currently do not emit through these new sockets:

- Passenger or driver profile edits, traveller-profile edits, KYC, vehicle
  edits/verification, support tickets, ratings, fines, and payout operations.
- Notification read/unread mutations; those belong to the notification APIs
  and notification WebSocket.
- Every live driver location write. The driver near-stop API updates persisted
  coordinates, but there is no location-stream event here.
- Passenger RFID recharge/order verification and every refund reconciliation
  transition. Accepted RFID board/drop scans are covered.
- Admin RFID policy/device/card changes unless they lead to a covered scan or
  trip/booking event.

Frontend screens for these areas continue using their existing mutation
responses, polling/focus refetch, and notification behavior. Add a cataloged
event explicitly if cross-device live invalidation is later required.

## 7. Driver action eligibility semantics

These events deserve special treatment because they drive buttons as well as
cache invalidation.

### 7.1 `trip.start_allowed`

Emitted at `planned_start_at` only while the trip is still `scheduled` and the
current time is within the 15-minute start window. It is also recalculated on a
driver reconnect.

It means the time gate is open. It does not bypass the start API's live GPS
check. The frontend may enable “Start trip,” but must submit current coordinates
to the existing start endpoint and display any API validation error.

### 7.2 `trip.departure_allowed`

Emitted only when persisted state satisfies all of these conditions:

1. Trip is `in_progress`.
2. Driver has successfully recorded arrival at the stop.
3. Departure has not already been recorded.
4. The route stop permits deboarding/departure under the current route policy.
5. For non-first stops, the previous stop has a departure time.
6. The route stop's `assume_time_diff_minutes` has elapsed since the previous
   stop's departure.
7. No normally booked `TripBooking` passenger who is currently `boarded` and
   due to drop at this stop remains without a DROP scan.

The check runs after arrival, when the travel-time timer expires, after a
normal QR/OTP drop scan, during startup schedule restoration, and on driver
reconnect.

The arrival event is emitted before a resulting departure-allowed event. This
lets the driver UI clear stale eligibility on `trip.stop_arrived` and then
enable it on `trip.departure_allowed` deterministically.

The event does not replace the stop-action API's live GPS and concurrency
validation. The Depart API can still reject the request if state or location
changed after the event.

## 8. Backend implementation map

No database migration or new environment variable is required. The feature is
implemented as an application-lifecycle service plus explicit post-commit
emitters.

| File | Responsibility |
| --- | --- |
| `app/realtime/catalog.py` | Canonical event-to-role resource and endpoint mappings. An event cannot be emitted to a role unless it is registered here. |
| `app/realtime/hub.py` | Role/user/connection registry, multi-device delivery, per-connection send locks, JSON encoding, failure cleanup, delayed callback scheduling, and shutdown. |
| `app/realtime/router.py` | Passenger/driver WebSocket routes, session authentication, role enforcement, ready/initial-sync messages, heartbeat, and inbound ping/pong handling. |
| `app/realtime/events.py` | Target resolution, trip passenger/driver lookup, booking privacy split, eligibility evaluation, timer creation/restoration, and reconnect eligibility. |
| `main.py` | Creates `app.state.api_refresh_hub`, restores timers at startup, injects the hub into relevant background jobs, includes the router, and shuts the hub down. |
| `app/admin/endpoints/router.py` | Route/stop/fare, admin trip lifecycle, manual completion, and no-show emitters. |
| `app/driver/trips/scheduled_trip.py` | Trip create/start/arrive/depart/end/emergency events and eligibility scheduling. |
| `app/driver/trips/cancel_trip.py` | Driver cancellation event and start-timer cancellation. |
| `app/driver/scan_events/scan.py` | QR board/drop event and post-drop departure recheck. |
| `app/driver/scan_events/otp.py` | OTP board/drop event and post-drop departure recheck. |
| `app/passenger/router.py` | Booking/session mutation emitters through one shared helper. |
| `app/rfid/router.py` | Accepted RFID private and occupancy-broadcast events. |
| `app/jobs/payment_reconciler.py` | Material background payment outcome events. |
| `app/jobs/unstarted_scheduled_trip_canceller.py` | Automatic trip-cancellation event. |
| `tests/test_api_refresh_hub.py` | Role isolation, multi-device targeting, delayed callbacks, and departure-policy coverage. |

### 8.1 Connection storage

Connections are keyed as:

```text
role -> user_id -> connection_id -> RefreshConnection
```

This structure provides:

- Strict passenger/driver fan-out separation.
- Multiple tabs/devices per user.
- Targeted user delivery without exposing private IDs to role broadcasts.
- Per-socket send serialization with an async lock.
- Cleanup of failed connections without aborting delivery to healthy devices.

### 8.2 Event publication flow

```text
HTTP mutation/background job
    -> validate and mutate database
    -> commit transaction
    -> resolve affected driver/passenger IDs
    -> build payload from catalog
    -> send to all matching live connections
    -> frontend invalidates active HTTP cache
    -> HTTP refetch returns authoritative state
```

Domain emitters run after the relevant database commit. A client receiving an
event can therefore refetch committed state. Individual socket send failures
are caught by the hub; the failed connection is closed/unregistered while
other devices continue receiving the event.

### 8.3 Privacy split for booking changes

`publish_booking_change` deliberately produces two messages:

1. Private `booking.changed`, containing booking/session IDs, to the owner and
   assigned driver.
2. Public-to-role `trip.seat_availability_changed`, containing trip/route and
   reason but no private booking ID, to all connected passengers.

Do not merge those payloads or add passenger identity to the broadcast event.

### 8.4 Scheduler lifecycle

The hub stores delayed callbacks by unique key:

```text
trip-start-<trip_id>
trip-depart-<trip_id>-<stop_id>
```

Scheduling the same key replaces/cancels the previous task. On application
startup:

- Future scheduled trips have their start timers recreated.
- In-progress trips have next-stop departure timers reconstructed from
  persisted trip-event departure times and route-stop travel durations.

On shutdown all scheduled tasks are cancelled and awaited before connections
are closed.

### 8.5 Adding a new event safely

Backend change checklist:

1. Add the event and each permitted audience to `EVENT_CATALOG`.
2. Choose minimal resource groups and real endpoint patterns.
3. Decide whether delivery is targeted or role-broadcast.
4. Keep private user/booking identifiers out of broadcast payloads.
5. Emit only after the mutation is committed.
6. Add the event to the correct role guide and TypeScript event union.
7. Add tests for audience isolation, payload, and timing/eligibility if relevant.
8. Verify reconnect initial sync repairs the state if this event is missed.

Do not make frontend correctness rely on a new `reason` string without also
providing appropriate `resources`.

## 9. Close codes and recovery policy

| Close code | Meaning in this implementation | Frontend action |
| --- | --- | --- |
| `1000` | Normal close initiated by client/framework | Reconnect only if the user is still authenticated and closure was not intentional. |
| `1001` | Heartbeat timeout or server shutdown | Reconnect with backoff. |
| `1008` | Missing/invalid/expired token or wrong role | Do not loop reconnect. Refresh auth if supported; otherwise log out. |
| `1011` | Unexpected server-side connection error | Reconnect with backoff and report telemetry. |
| `1006` client-observed | Network/proxy interruption | Reconnect when online. |

Recommended retry delays: 1 s, 2 s, 5 s, 10 s, then 30 s maximum, with small
random jitter. Reset the attempt count after a successful open.

## 10. Deployment constraint

`APIRefreshHub`, the notification hub, seat-map hub, and scheduled eligibility
callbacks are process-local. With multiple Uvicorn/Gunicorn workers, a mutation
handled by one worker cannot directly reach sockets connected to another
worker. Timer ownership also becomes per-process.

Current safe deployment: one WebSocket-serving application process in total
(one worker and one replica).

Before horizontal/multi-worker WebSocket scaling, place event fan-out and timer
coordination behind Redis pub/sub (or another shared broker) and ensure only one
logical timer execution per trip/stop.

## 11. Verification and observability

Current automated checks cover hub role isolation, multi-device targeted
delivery, scheduled callback execution, and route-policy gating for departure.
The complete backend verification command used for this feature also compiles
all modules and runs undefined-name/static checks.

Relevant logs:

- `api_refresh_published`: audience, event, and live connection send count.
- `api_refresh_send_failed`: role/user/connection/event for failed delivery.
- `api_refresh_scheduled_callback_failed`: timer callback key.
- `api_refresh_connection_error`: unexpected connection handler failure.

Do not log access tokens or full WebSocket URLs.

Recommended production telemetry additions before broader scale:

- Current connections by role.
- Reconnect/close counts by close code.
- Publish-to-send latency and failed-send rate.
- Scheduled eligibility callback count/failure rate.
- API refetch failure rate after refresh events (frontend telemetry).

## 12. Migration checklist

- Keep the existing notification socket.
- Keep the passenger seat-map socket on seat-selection screens.
- Add the role-specific refresh socket after login/session restoration.
- Shut it down on logout or role/account switch.
- Respond to heartbeat ping.
- Implement reconnect with `1008` auth handling.
- Process `channel.connected` as initial synchronization.
- Invalidate active cache entries, not every endpoint eagerly.
- Add the driver action-state logic documented in the driver guide.
- Test two tabs/devices for the same user.
- Test offline/reconnect across a trip start and stop transition.
- Deploy with a single worker until shared fan-out exists.
