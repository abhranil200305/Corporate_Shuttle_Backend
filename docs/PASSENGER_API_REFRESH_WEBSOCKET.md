# Passenger API refresh WebSocket: complete frontend integration

This is the standalone implementation contract for the passenger application.
It includes everything needed to ship the feature without reading backend code
or the driver guide.

## 1. What the passenger socket does

Connect to:

```text
wss://<api-host>/passenger/ws/refresh?token=<URL-encoded-access-token>
```

This socket tells the passenger app when these API-backed areas may be stale:

- Stops, routes, fares, and route/trip discovery.
- Scheduled trip lists/details and REST seat availability.
- The logged-in passenger's bookings and multi-seat booking sessions.
- Current-trip status and location views.
- QR/OTP board/drop state.
- RFID account, ride, ledger, and RFID trip availability.

It sends invalidation instructions, not complete replacement objects. After an
event, the frontend refetches/invalidate-caches the relevant existing HTTP
queries. HTTP remains authoritative.

Current boundary: this channel does not emit for profile/traveller-profile
edits, support tickets, ratings, notification read state, every recharge/refund
transition, or each driver location write. Keep existing mutation-response,
focus-refetch, polling, and notification behavior for those screens.

## 2. What it does not replace

Keep both existing passenger real-time integrations:

### 2.1 Notification WebSocket

```text
/notifications/ws?token=<access-token>
```

It carries notification-center records and human-readable alerts. Continue to
use it for banners/toasts, unread counts, persistence, and `mark_read`.

Do not show a second toast merely because the refresh socket emitted
`trip.started` or `trip.cancelled`. Use the refresh event to update data; use
the notification event for user-facing copy.

### 2.2 Seat-map WebSocket

```text
/passenger/seatmap/ws?token=<access-token>
```

It remains the authoritative live seat snapshot channel for a selected trip
and route leg. Keep it connected/subscribed while the passenger is choosing a
seat. The new refresh socket covers global trip discovery and REST caches; it
does not contain occupied/available seat arrays.

## 3. Authentication and lifecycle

Use `access_token` from the authentication response:

```json
{
  "access_token": "session-token",
  "token_type": "bearer",
  "expires_at": "...",
  "user": {
    "user_id": "...",
    "role": "passenger"
  }
}
```

Open the passenger socket only after all of these are true:

- Session restoration/login completed.
- An access token exists.
- `user.role === "passenger"`.

Close it when:

- Passenger logs out.
- Access token is removed/replaced.
- The application switches account or role.

For browser clients, pass the token in the query string. Native clients may
instead send `Authorization: Bearer <token>`. Use `wss://` in production.

A missing, expired, invalid, logged-out, or non-passenger token closes with
code `1008`. Stop reconnecting on `1008`; refresh the session if the app has a
supported refresh mechanism, otherwise route to login.

## 4. Wire messages

### 4.1 Ready

```json
{
  "type": "ws.ready",
  "channel": "passenger",
  "user_id": "passenger-user-id",
  "message": "API refresh WebSocket authenticated."
}
```

`ws.ready` confirms transport/auth only. The following
`channel.connected` refresh is what triggers initial data synchronization.

### 4.2 Heartbeat

Server message:

```json
{"type":"ping"}
```

Required immediate client reply:

```json
{"type":"pong"}
```

The server pings every 15 seconds and closes stale clients after 30 seconds.
The passenger app does not need its own heartbeat interval.

### 4.3 API refresh

```ts
type PassengerResource =
  | "routes"
  | "stops"
  | "route_trip_options"
  | "scheduled_trips"
  | "seat_availability"
  | "bookings"
  | "booking_sessions"
  | "current_bookings"
  | "transactions"
  | "trip_status"
  | "trip_location"
  | "rfid_summary"
  | "rfid_rides"
  | "rfid_ledger";

interface PassengerRefreshMessage {
  type: "api.refresh";
  event: PassengerRefreshEvent;
  audience: "passenger";
  resources: PassengerResource[];
  endpoints: string[];
  data: Record<string, unknown>;
  occurred_at: string;
}
```

`endpoints` are patterns. Never request a URL containing literal
`{trip_id}`/`{booking_id}`. Resolve IDs from `data` or cached/current state.
Treat `resources` as canonical; `endpoints` lists primary hints and may omit a
related detail/derived query that shares the resource.

## 5. Resource-to-API mapping

This is the recommended complete frontend mapping for the resource groups sent
by the current backend.

| Resource | APIs/cache families to invalidate |
| --- | --- |
| `stops` | `GET /passenger/stops` |
| `routes` | `GET /passenger/routes`, `GET /passenger/routes/{route_id}` |
| `route_trip_options` | `GET /passenger/route-trip-options` using the active search filters; `GET /passenger/rfid/route-trip-options` using the active RFID search filters |
| `scheduled_trips` | `GET /passenger/scheduled-trips`; matching `GET /passenger/scheduled-trips/{trip_id}`; matching driver/vehicle info query if visible |
| `seat_availability` | `POST /passenger/scheduled-trips/{trip_id}/available-seats` using the screen's stored route/pickup/drop payload. The seat-map socket remains preferred while subscribed. |
| `bookings` | `GET /passenger/bookings`, `/bookings/upcoming`, `/bookings/current`, `/history`, and relevant booking detail/invoice/QR queries |
| `booking_sessions` | `GET /passenger/booking-sessions`, `/booking-sessions/current`, and relevant session detail |
| `current_bookings` | `GET /passenger/bookings/current` and `/passenger/booking-sessions/current` |
| `transactions` | `GET /passenger/transactions` and relevant payment/invoice displays |
| `trip_status` | Relevant `/passenger/bookings/{booking_id}/current-status` and `/passenger/booking-sessions/{booking_session_id}/current-status` queries |
| `trip_location` | Relevant `/passenger/bookings/{booking_id}/live-location` and booking-session live-location queries |
| `rfid_summary` | `GET /passenger/rfid/summary`, `GET /passenger/rfid/me` |
| `rfid_rides` | `GET /passenger/rfid/rides`, visible `/passenger/rfid/rides/{rfid_ride_id}` |
| `rfid_ledger` | `GET /passenger/rfid/ledger`, plus summary because balances may have changed |

Frontend rule: invalidate cache families and refetch active observers. Do not
eagerly call every API in the table when its screen is not mounted.

## 6. Exhaustive passenger event catalog

### 6.1 Connection and catalog events

| Event | Delivery | `data` keys | Required frontend response |
| --- | --- | --- | --- |
| `channel.connected` | This connection only | `reason: "initial_sync"` | Mandatory initial refresh of routes, scheduled trips, current bookings, and current booking sessions. Also runs after reconnect. |
| `route.created` | Broadcast to all connected passengers | `route_id`, `route_name`, `is_active` | Invalidate stops/routes, route discovery, RFID route discovery, and scheduled trips. A newly created identity can initially be inactive until stops are added. |
| `route.updated` | Broadcast | Variant-specific fields listed below | Invalidate the same route catalog resources. Do not branch behavior by reason unless useful for telemetry. |
| `trip.created` | Broadcast | `trip_id`, `route_id` | Invalidate trip discovery, scheduled trips, and seat availability for the trip. |
| `trip.catalog_changed` | Broadcast | Always `trip_id`; may include `route_id`, `reason`, `automatic` | A trip entered/left a discovery-relevant state. Invalidate discovery/scheduled-trip/availability queries. |

Possible `route.updated.data` shapes:

```ts
type RouteUpdatedData =
  | { reason: "stops_bulk_uploaded"; count: number }
  | { reason: "stop_upserted"; stop_name: string }
  | { reason: "stop_deleted"; stop_id: string }
  | {
      reason: "route_stops_added";
      route_id: string;
      is_active: boolean;
      added_count: number;
    }
  | {
      reason: "route_status_changed";
      route_id: string;
      is_active: boolean;
    }
  | { reason: "route_fares_changed"; route_id: string };
```

### 6.2 Trip lifecycle events

These events are targeted to users who have a booking on the trip. A separate
`trip.catalog_changed` broadcast follows lifecycle changes that affect general
discovery.

| Event | `data` keys | Meaning and response |
| --- | --- | --- |
| `trip.started` | `trip_id`, `route_id` | Trip is now in progress. Refresh current bookings/sessions, current status/location, trip detail, and scheduled trips. |
| `trip.stop_arrived` | `trip_id`, `route_id`, `stop_id`, `sequence_no`, `mode: "arrive"` | Arrival committed. Refresh current progress/status and any visible live trip view. |
| `trip.stop_departed` | `trip_id`, `route_id`, `stop_id`, `sequence_no`, `mode: "depart"` | Departure committed. Refresh progress/status/location. A missed passenger status may have been committed in the same operation. |
| `trip.completed` | `trip_id`; normally `route_id`; admin completion can add `reason: "admin_completed"` | Refresh current lists, history/status, trip detail, and location state. Current-trip UI should derive closure from refetched APIs. |
| `trip.cancelled` | `trip_id`; usually `route_id`; optional `reason`; auto-cancel adds `automatic: true` | Refresh bookings/sessions/current/history and trip detail. Use notification socket for user-facing cancellation copy. |
| `trip.premature_ended` | `trip_id`; driver path includes `route_id` and `reason`; admin path may contain only `trip_id` | Refresh current/history/status/location. Do not assume optional keys exist. |

### 6.3 Booking and availability events

| Event | Delivery | `data` keys | Required response |
| --- | --- | --- | --- |
| `booking.changed` | Private: booking owner only | `trip_id`, nullable `booking_id`, nullable `booking_session_id`, `reason` | Invalidate bookings, sessions, current/upcoming/history, and transactions. Use provided IDs for targeted detail invalidation when non-null. |
| `trip.seat_availability_changed` | Broadcast | `trip_id`, nullable `route_id`, `reason` | Invalidate REST availability and trip discovery for that trip. Seat-selection screens should primarily consume their seat-map subscription snapshot. |

Current direct mutation reasons:

```text
booking_session_created
booking_session_payment_verified
booking_session_cancelled
booking_session_seat_cancelled
booking_created
booking_payment_verified
booking_cancelled
passenger_marked_no_show
```

Payment reconciliation reasons use:

```text
payment_reconcile:<outcome>
```

Material outcomes currently include:

```text
expired_without_local_payment
paid_after_hold_expiry
promoted_local_paid
expired_without_provider_payment
captured_after_hold_expiry
booked_from_captured_payment
expired_with_authorized_payment
booked_after_capture
```

Treat `reason` as an extensible diagnostic string. Do not make cache
correctness depend on a closed list of reasons.

### 6.4 Normal passenger scan event

```json
{
  "trip_id": "...",
  "booking_id": "...",
  "stop_id": "...",
  "scan_type": "board",
  "booking_status": "boarded"
}
```

`passenger.scan_completed` is private to the scanned passenger. It is emitted
after an accepted QR or OTP scan. `scan_type` is `board` or `drop`; the event
does not identify whether QR or OTP was used. Refetch current booking/trip
status rather than mutating the cached booking solely from the payload.

### 6.5 RFID events

Accepted scan payload:

```json
{
  "trip_id": "...",
  "route_id": "...",
  "stop_id": "...",
  "scan_type": "board"
}
```

| Event | Delivery | Response |
| --- | --- | --- |
| `rfid.scan_completed` | Private to scanned passenger | Refresh RFID summary/card/account, rides, ride detail if open, and ledger. |
| `trip.rfid_occupancy_changed` | Broadcast | Refresh RFID route discovery and trip/seat availability for the identified trip. It contains no passenger ID. |

Rejected RFID scans emit neither event.

## 7. Page-by-page behavior

### Application bootstrap/home

- Start notification and passenger refresh sockets after session restoration.
- On `channel.connected`, invalidate the home route/trip data and current
  booking/session badges.
- Do not block initial HTTP rendering while waiting for WebSocket connection.

### Route search/discovery

- Respond to `route.created`, `route.updated`, `trip.created`,
  `trip.catalog_changed`, `trip.seat_availability_changed`, and
  `trip.rfid_occupancy_changed`.
- Preserve the user's active from/to/time filters and refetch with those exact
  filters.
- Do not reset navigation or form input on an invalidation.

### Seat selection

- Open `/passenger/seatmap/ws` and send `seat_map.subscribe` with
  `scheduled_trip_id`, `route_id`, `pickup_stop_id`, `dropoff_stop_id`.
- Render `seat_map.snapshot` as the authoritative seat state.
- Keep the global refresh socket running; use its availability events to
  invalidate surrounding trip/fare summaries.
- On leaving, send `seat_map.unsubscribe` or close the seat-map socket.

Exact existing seat-map subscription message:

```json
{
  "type": "seat_map.subscribe",
  "scheduled_trip_id": "...",
  "route_id": "...",
  "pickup_stop_id": "...",
  "dropoff_stop_id": "..."
}
```

The server answers with `type: "seat_map.snapshot"` and the leg state,
including `seat_capacity`, `overlapping_active_bookings`, `available_seats`,
`occupied_seat_numbers`, `available_seat_numbers`, and `trip_bookable`. To
unsubscribe, send the same IDs with `type: "seat_map.unsubscribe"`. The
seat-map socket has the same server-ping/client-pong heartbeat requirement.

### Bookings and booking sessions

- On `booking.changed`, invalidate all list variants and the identified detail.
- If `booking_id` is null, refresh the session and its booking children.
- If `booking_session_id` is null, refresh the individual legacy booking.
- Payment/error HTTP responses still control immediate mutation UX.

### Current trip

- Lifecycle and scan events invalidate current bookings/session, status, and
  location queries.
- Polling, if retained, can be reduced but should not be removed unless product
  has accepted best-effort WebSocket delivery. Reconnect sync repairs missed
  events but does not provide second-by-second location streaming.

### RFID

- Private RFID scan refreshes balances/rides/ledger.
- Broadcast occupancy refreshes discovery/availability only.
- Do not expose another passenger's identity; broadcast payload has none.

## 8. Copy-paste TypeScript socket client

The following client is framework-independent. Instantiate one per logged-in
passenger session.

```ts
export type PassengerRefreshEvent =
  | "channel.connected"
  | "route.created"
  | "route.updated"
  | "trip.created"
  | "trip.catalog_changed"
  | "trip.started"
  | "trip.stop_arrived"
  | "trip.stop_departed"
  | "trip.completed"
  | "trip.cancelled"
  | "trip.premature_ended"
  | "booking.changed"
  | "trip.seat_availability_changed"
  | "passenger.scan_completed"
  | "rfid.scan_completed"
  | "trip.rfid_occupancy_changed";

export interface PassengerRefreshMessage {
  type: "api.refresh";
  event: PassengerRefreshEvent;
  audience: "passenger";
  resources: string[];
  endpoints: string[];
  data: Record<string, unknown>;
  occurred_at: string;
}

type ConnectionState = "connecting" | "connected" | "disconnected";

interface PassengerRefreshSocketOptions {
  apiBaseUrl: string;
  accessToken: string;
  onRefreshBatch: (events: PassengerRefreshMessage[]) => void;
  onConnectionState?: (state: ConnectionState) => void;
  onAuthenticationFailure?: () => void;
  onProtocolError?: (value: unknown) => void;
}

export function createPassengerRefreshSocket(
  options: PassengerRefreshSocketOptions,
) {
  const retryDelays = [1_000, 2_000, 5_000, 10_000, 30_000];
  let token = options.accessToken;
  let socket: WebSocket | null = null;
  let retryAttempt = 0;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let batchTimer: ReturnType<typeof setTimeout> | null = null;
  let pendingEvents: PassengerRefreshMessage[] = [];
  let stopped = true;
  let generation = 0;

  const buildUrl = () => {
    const url = new URL(options.apiBaseUrl);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.pathname = `${url.pathname.replace(/\/$/, "")}/passenger/ws/refresh`;
    url.search = "";
    url.searchParams.set("token", token);
    return url.toString();
  };

  const flushBatch = () => {
    batchTimer = null;
    const batch = pendingEvents;
    pendingEvents = [];
    if (batch.length > 0) options.onRefreshBatch(batch);
  };

  const enqueue = (event: PassengerRefreshMessage) => {
    pendingEvents.push(event);
    if (batchTimer === null) batchTimer = setTimeout(flushBatch, 100);
  };

  const scheduleReconnect = () => {
    if (stopped || retryTimer !== null || !navigator.onLine) return;
    const base = retryDelays[Math.min(retryAttempt, retryDelays.length - 1)];
    retryAttempt += 1;
    const jittered = Math.round(base * (0.8 + Math.random() * 0.4));
    retryTimer = setTimeout(() => {
      retryTimer = null;
      connect();
    }, jittered);
  };

  const isRefreshMessage = (value: unknown): value is PassengerRefreshMessage => {
    if (typeof value !== "object" || value === null) return false;
    const item = value as Record<string, unknown>;
    return (
      item.type === "api.refresh" &&
      item.audience === "passenger" &&
      typeof item.event === "string" &&
      Array.isArray(item.resources) &&
      Array.isArray(item.endpoints) &&
      typeof item.data === "object" &&
      item.data !== null &&
      typeof item.occurred_at === "string"
    );
  };

  function connect() {
    if (stopped || !token || !navigator.onLine) return;
    if (socket?.readyState === WebSocket.OPEN ||
        socket?.readyState === WebSocket.CONNECTING) return;

    const myGeneration = generation;
    options.onConnectionState?.("connecting");
    const ws = new WebSocket(buildUrl());
    socket = ws;

    ws.onopen = () => {
      if (myGeneration !== generation) return;
      retryAttempt = 0;
      options.onConnectionState?.("connected");
    };

    ws.onmessage = ({ data }) => {
      if (myGeneration !== generation) return;
      let message: unknown;
      try {
        message = JSON.parse(String(data));
      } catch {
        options.onProtocolError?.(data);
        return;
      }

      if (
        typeof message === "object" &&
        message !== null &&
        (message as { type?: unknown }).type === "ping"
      ) {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "pong" }));
        }
        return;
      }

      if (
        typeof message === "object" &&
        message !== null &&
        (message as { type?: unknown }).type === "ws.ready"
      ) return;

      if (isRefreshMessage(message)) enqueue(message);
      else options.onProtocolError?.(message);
    };

    ws.onclose = ({ code }) => {
      if (myGeneration !== generation) return;
      socket = null;
      options.onConnectionState?.("disconnected");
      if (stopped) return;
      if (code === 1008) {
        options.onAuthenticationFailure?.();
        return;
      }
      scheduleReconnect();
    };
  }

  const onOnline = () => {
    if (!stopped && socket?.readyState !== WebSocket.OPEN) connect();
  };

  return {
    start() {
      if (!stopped) return;
      stopped = false;
      window.addEventListener("online", onOnline);
      connect();
    },
    stop() {
      stopped = true;
      generation += 1;
      window.removeEventListener("online", onOnline);
      if (retryTimer !== null) clearTimeout(retryTimer);
      if (batchTimer !== null) clearTimeout(batchTimer);
      retryTimer = null;
      batchTimer = null;
      pendingEvents = [];
      socket?.close(1000, "client shutdown");
      socket = null;
      options.onConnectionState?.("disconnected");
    },
    replaceAccessToken(nextToken: string) {
      token = nextToken;
      generation += 1;
      socket?.close(1000, "token replaced");
      socket = null;
      if (!stopped) connect();
    },
  };
}
```

If `navigator`/`window` are unavailable in a native runtime, replace the online
listener with that platform's network-state API. The protocol logic is the
same.

## 9. TanStack Query reference invalidator

Adapt key names to the application's existing query-key factory. The important
behavior is prefix invalidation with `refetchType: "active"`.

```ts
import type { QueryClient, QueryKey } from "@tanstack/react-query";

export async function applyPassengerRefreshBatch(
  queryClient: QueryClient,
  events: PassengerRefreshMessage[],
) {
  const resources = new Set(events.flatMap((event) => event.resources));
  const tripIds = new Set(
    events
      .map((event) => event.data.trip_id)
      .filter((id): id is string => typeof id === "string" && id.length > 0),
  );
  const bookingIds = new Set(
    events
      .map((event) => event.data.booking_id)
      .filter((id): id is string => typeof id === "string" && id.length > 0),
  );
  const sessionIds = new Set(
    events
      .map((event) => event.data.booking_session_id)
      .filter((id): id is string => typeof id === "string" && id.length > 0),
  );

  const keys: QueryKey[] = [];
  if (resources.has("stops")) keys.push(["passenger", "stops"]);
  if (resources.has("routes")) keys.push(["passenger", "routes"]);
  if (resources.has("route_trip_options")) {
    keys.push(["passenger", "routeTripOptions"]);
    keys.push(["passenger", "rfidRouteTripOptions"]);
  }
  if (resources.has("scheduled_trips")) {
    keys.push(["passenger", "scheduledTrips"]);
    for (const id of tripIds) keys.push(["passenger", "scheduledTrip", id]);
  }
  if (resources.has("seat_availability")) {
    for (const id of tripIds) keys.push(["passenger", "availableSeats", id]);
  }
  if (resources.has("bookings") || resources.has("current_bookings")) {
    keys.push(["passenger", "bookings"]);
    for (const id of bookingIds) keys.push(["passenger", "booking", id]);
  }
  if (resources.has("booking_sessions") || resources.has("current_bookings")) {
    keys.push(["passenger", "bookingSessions"]);
    for (const id of sessionIds) keys.push(["passenger", "bookingSession", id]);
  }
  if (resources.has("transactions")) keys.push(["passenger", "transactions"]);
  if (resources.has("trip_status")) keys.push(["passenger", "tripStatus"]);
  if (resources.has("trip_location")) keys.push(["passenger", "tripLocation"]);
  if (resources.has("rfid_summary")) {
    keys.push(["passenger", "rfidSummary"]);
    keys.push(["passenger", "rfidMe"]);
  }
  if (resources.has("rfid_rides")) keys.push(["passenger", "rfidRides"]);
  if (resources.has("rfid_ledger")) keys.push(["passenger", "rfidLedger"]);

  const unique = new Map(keys.map((key) => [JSON.stringify(key), key]));
  await Promise.all(
    [...unique.values()].map((queryKey) =>
      queryClient.invalidateQueries({ queryKey, refetchType: "active" }),
    ),
  );
}
```

Application-session wiring:

```ts
const passengerRefresh = createPassengerRefreshSocket({
  apiBaseUrl: config.apiBaseUrl,
  accessToken: auth.accessToken,
  onRefreshBatch: (events) => {
    void applyPassengerRefreshBatch(queryClient, events);
  },
  onConnectionState: (state) => passengerUiStore.setRefreshSocketState(state),
  onAuthenticationFailure: () => auth.logoutAndRouteToLogin(),
  onProtocolError: (value) => telemetry.capture("passenger_ws_protocol", value),
});

// After login/session restoration:
passengerRefresh.start();

// If the session token changes:
passengerRefresh.replaceAccessToken(auth.accessToken);

// Before logout/account switch/application teardown:
passengerRefresh.stop();
```

## 10. Passenger acceptance checklist

- Passenger role connects successfully and receives `ws.ready`, then
  `channel.connected`.
- Driver/admin token is rejected and does not cause an infinite reconnect loop.
- Client replies to ping and stays connected beyond 30 seconds.
- Network loss reconnects with backoff; reconnect refreshes current data.
- Route create/toggle/fare/stop changes refresh an open search page without
  clearing its filters.
- Trip creation/start/cancel/completion updates scheduled/current screens.
- Booking changes update all open tabs for the same passenger.
- Another passenger's booking does not expose their booking ID; only anonymous
  availability invalidation is broadcast.
- QR/OTP board and drop update current status.
- RFID board/drop updates balance/ride/ledger and discovery.
- Seat-selection screen still consumes full `seat_map.snapshot` messages.
- Notification toasts come from `/notifications/ws`, not duplicated from this
  invalidation socket.
- Logout closes all sockets and clears reconnect timers.
- Production uses WSS and does not log query-string tokens.
- Production uses one WebSocket-serving backend process/replica until shared
  pub/sub exists.
