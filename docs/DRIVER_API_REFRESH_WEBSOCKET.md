# Driver API refresh WebSocket: complete frontend integration

This is the standalone implementation contract for the driver application. It
covers connection management, every event/payload, API cache invalidation, and
the exact Start/Depart button behavior.

## 1. What the driver socket does

Connect to:

```text
wss://<api-host>/driver/ws/refresh?token=<URL-encoded-access-token>
```

The socket updates these driver-side areas:

- Route catalog and route details.
- Current scheduled/in-progress trip.
- Trip details and stop progress.
- Passenger manifests, stop passenger lists, and drop events.
- Normal QR/OTP board/drop scan results.
- RFID scan details.
- Trip start-window eligibility.
- Stop departure eligibility.
- Trip completion, cancellation, and premature end.

Most messages are cache-invalidation hints. `trip.start_allowed` and
`trip.departure_allowed` additionally control whether the relevant action can
be attempted. The mutation APIs still make the final authorization and GPS
decision.

Current boundary: this channel does not emit for driver profile/KYC/vehicle
changes, support, ratings, fines, payouts, every location write, or all admin
RFID policy/device changes. Those screens retain their existing API response,
focus-refetch, polling, and notification behavior.

## 2. Existing driver real-time behavior remains

Keep the notification socket:

```text
/notifications/ws?token=<access-token>
```

It is for human-readable and persisted notifications. The driver refresh
socket is for data synchronization and action eligibility.

Examples:

- Notification socket: “Trip auto-cancelled.”
- Refresh socket: `trip.cancelled`, telling the app to refresh current trip and
  clear action state.

Use notification messages for toasts/banners. Do not create duplicate toasts
from refresh events unless product explicitly wants that behavior.

There is no driver seat-map WebSocket.

## 3. Authentication and socket lifecycle

Use `access_token` returned by login/signup. Open this socket only if
`user.role === "driver"`.

Browser URL:

```text
wss://api.example.com/driver/ws/refresh?token=<URL-encoded-access-token>
```

Native clients may send `Authorization: Bearer <token>` instead. Use WSS in
production. Never log the full URL because it contains a session credential.

Open after login/session restoration. Close on logout, token replacement,
account switch, or role switch.

Close code `1008` means missing/invalid/expired/logged-out token or wrong role.
Do not reconnect indefinitely on `1008`; refresh auth if supported or route to
login.

## 4. Initial connection and reconnect behavior

First message:

```json
{
  "type": "ws.ready",
  "channel": "driver",
  "user_id": "driver-user-id",
  "message": "API refresh WebSocket authenticated."
}
```

Second message:

```json
{
  "type": "api.refresh",
  "event": "channel.connected",
  "audience": "driver",
  "resources": ["routes", "current_trip", "trip_details"],
  "endpoints": [
    "/driver/routes/",
    "/driver/trips/current",
    "/driver/trips/{trip_id}/details"
  ],
  "data": {"reason": "initial_sync"},
  "occurred_at": "2026-07-03T06:30:00+00:00"
}
```

On `channel.connected`:

1. Mark locally cached action eligibility as unknown/false.
2. Refetch routes and `GET /driver/trips/current`.
3. If a current trip exists, refetch details/stops.
4. Wait for a following eligibility event before enabling Start/Depart.

The existing current-trip endpoint uses HTTP `204` when no scheduled or
in-progress trip exists. Treat that as a valid “no current trip” state and
clear all action candidates; do not display it as a generic request failure.

After `channel.connected`, the server immediately re-evaluates the driver's
current trip. It sends `trip.start_allowed` if the current scheduled trip is
inside its start window. For an in-progress trip at an active arrived stop, it
sends `trip.departure_allowed` if all persisted departure conditions are met.

This reconnect check repairs missed timer messages.

## 5. Heartbeat and reconnect

- Server sends `{"type":"ping"}` every 15 seconds.
- Driver client must immediately reply `{"type":"pong"}`.
- No pong for 30 seconds causes close code `1001`.
- Client may send `ping`; server returns `pong`.
- Reconnect code `1001`, `1011`, `1006`, and ordinary network failures with
  exponential backoff.
- Do not reconnect-loop on `1008`.
- Disable Start and Depart while the socket is disconnected or resynchronizing;
  a stale eligibility flag is unsafe.

Recommended delays: 1 s, 2 s, 5 s, 10 s, then 30 s, with jitter.

## 6. Wire types

```ts
type DriverResource =
  | "routes"
  | "route_details"
  | "current_trip"
  | "trip_details"
  | "trip_stops"
  | "trip_bookings"
  | "current_trip_passengers"
  | "stop_passengers"
  | "drop_events"
  | "departure_action"
  | "rfid_scan_details";

interface DriverRefreshMessage {
  type: "api.refresh";
  event: DriverRefreshEvent;
  audience: "driver";
  resources: DriverResource[];
  endpoints: string[];
  data: Record<string, unknown>;
  occurred_at: string; // UTC ISO-8601
}
```

`endpoints` contain placeholders. Do not request literal `{trip_id}` or
`{route_id}`. Resolve IDs from message `data` or the refetched current trip.
Treat `resources` as canonical; endpoint patterns are primary hints and may not
list every related detail query.

## 7. Resource-to-API mapping

| Resource | APIs/cache families to invalidate |
| --- | --- |
| `routes` | `GET /driver/routes/` |
| `route_details` | Matching `GET /driver/routes/{route_id}/trips/details` |
| `current_trip` | `GET /driver/trips/current` |
| `trip_details` | Matching `GET /driver/trips/{trip_id}/details`; matching route/trip detail if used |
| `trip_stops` | `GET /driver/trips/{trip_id}/stops` |
| `trip_bookings` | `GET /driver/trips/{trip_id}/bookings` |
| `current_trip_passengers` | `GET /driver/trips/current/passengers` |
| `stop_passengers` | `GET /driver/trips/stop-passengers?trip_id=...&stop_id=...` for the active stop |
| `drop_events` | `GET /driver/trips/{trip_id}/drop-events` |
| `rfid_scan_details` | `GET /driver/rfid/scan-details?scheduled_trip_id=...` |
| `departure_action` | UI-state signal. There is no GET endpoint for this resource; consume the `trip.departure_allowed` event and still use the stop-action mutation API. |

Invalidate active queries instead of immediately calling every listed API.

## 8. Exhaustive driver event catalog

### 8.1 Connection and route catalog

| Event | Delivery | `data` | Required behavior |
| --- | --- | --- | --- |
| `channel.connected` | This connection | `{ reason: "initial_sync" }` | Clear stale action flags and refetch route/current-trip state. |
| `route.created` | Broadcast to all connected drivers | `route_id`, `route_name`, `is_active` | Refresh routes and relevant route detail. Newly created routes may remain inactive until enough stops exist. |
| `route.updated` | Broadcast | Variant-specific route/stop fields | Refresh routes and visible route detail. |

Possible `route.updated.data` variants:

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

### 8.2 Trip creation and start

| Event | `data` | Behavior |
| --- | --- | --- |
| `trip.created` | `trip_id`, `route_id` | Targeted to the creating driver. Refresh current trip/details/stops. Keep Start disabled until the time event arrives or reconnect eligibility confirms it. |
| `trip.start_allowed` | `trip_id`, `route_id`, `planned_start_at`, `start_window_ends_at`, `gps_check_still_required: true` | Enable “Start trip” only for the matching current trip. Show deadline/countdown if desired. Capture live coordinates and call the start API when tapped. |
| `trip.started` | `trip_id`, `route_id` | Clear start eligibility. Refresh current trip/details/stops; transition UI to in-progress mode. |

`trip.start_allowed` means only that the backend time window is open and the
trip is still scheduled. The mutation API still verifies ownership/status,
time, first-stop location, and current GPS.

### 8.3 Stop lifecycle and departure

| Event | `data` | Behavior |
| --- | --- | --- |
| `trip.stop_arrived` | `trip_id`, `route_id`, `stop_id`, `sequence_no`, `mode: "arrive"` | Clear any previous Depart flag for this trip/stop, refresh trip details/stops and active stop passenger list. A matching `trip.departure_allowed` can immediately follow. |
| `trip.departure_allowed` | `trip_id`, `route_id`, `stop_id`, `sequence_no` | Enable Depart only when both current trip ID and active stop ID match. Refetch stop passengers/details. |
| `trip.stop_departed` | `trip_id`, `route_id`, `stop_id`, `sequence_no`, `mode: "depart"` | Clear Depart eligibility, refresh trip progress/stops, and move UI toward the next stop. |

Deterministic successful-arrival order:

```text
trip.stop_arrived
    -> optional trip.departure_allowed immediately
    -> or trip.departure_allowed later after time/drop constraints clear
```

Do not enable Depart merely because a passenger DROP scan arrived. Wait for the
backend's `trip.departure_allowed` event.

### 8.4 Completion and cancellation

| Event | Possible `data` | Behavior |
| --- | --- | --- |
| `trip.completed` | `trip_id`, normally `route_id`; admin path can add `reason: "admin_completed"` | Clear Start/Depart state. Refresh current trip/details. Navigate/transition only after authoritative HTTP state confirms completion. |
| `trip.cancelled` | `trip_id`, usually `route_id`, optional `reason`; automatic path adds `automatic: true` | Clear all trip action state and refresh current trip. Notification socket supplies human-facing cancellation copy. |
| `trip.premature_ended` | `trip_id`; driver path includes `route_id`, `reason`; admin path may include only `trip_id` | Clear action state, refresh current trip/details, and leave in-progress UI after HTTP confirms state. |

### 8.5 Booking/manifest changes

```json
{
  "trip_id": "...",
  "booking_id": "... or null",
  "booking_session_id": "... or null",
  "reason": "booking_payment_verified"
}
```

`booking.changed` is private to the assigned driver and booking owner. Refresh
trip bookings, current passengers, active stop passengers, and relevant drop
events. The driver should not need to interpret `reason`; it is extensible.

Current direct reasons:

```text
booking_session_created
booking_session_payment_verified
booking_session_cancelled
booking_session_seat_cancelled
booking_created
booking_payment_verified
booking_cancelled
passenger_marked_no_show
payment_reconcile:<outcome>
```

### 8.6 Normal QR/OTP scans

```json
{
  "trip_id": "...",
  "booking_id": "...",
  "stop_id": "...",
  "scan_type": "drop",
  "booking_status": "completed"
}
```

`passenger.scan_completed` is sent after an accepted QR or OTP board/drop. It
does not identify which scan mechanism was used.

On receipt:

- Refresh trip bookings/current passengers/active stop passengers/drop events.
- For `drop`, keep Depart disabled until `trip.departure_allowed` arrives.
- Do not update a manifest exclusively from payload fields; refetch latest
  state to cover concurrent scans and booking changes.

### 8.7 RFID scans

```json
{
  "trip_id": "...",
  "route_id": "...",
  "stop_id": "...",
  "scan_type": "board"
}
```

`rfid.scan_completed` is sent to the assigned driver only for an accepted RFID
scan. Refresh RFID scan details and trip details. Rejected RFID scans do not
emit this event.

RFID scan events do not directly enable normal-booking departure. The current
departure eligibility implementation checks normally booked `TripBooking`
passengers due to drop; RFID ride settlement remains governed by existing RFID
and trip-end logic.

## 9. Exact action eligibility semantics

### 9.1 Start trip

Backend scheduling:

- A timer is created when the trip is created.
- It fires at `planned_start_at`.
- Event is emitted only if trip is still `scheduled` and time is no later than
  `planned_start_at + 15 minutes`.
- Timer is restored for future scheduled trips when the app starts.
- Reconnect independently recalculates the current start window.
- Starting/cancelling the trip cancels its pending start timer.
- The background job auto-cancels an unstarted trip after the grace window.

Start mutation:

```http
POST /driver/scheduled-trips/{trip_id}/start
Content-Type: multipart/form-data

lat=<current latitude>
lng=<current longitude>
```

The API still checks:

- Trip exists, belongs to the driver, and is scheduled.
- Current time is within the start window and before planned end.
- Route has a first stop.
- Driver coordinates are inside first-stop radius plus the backend GPS buffer.

UI rule: `trip.start_allowed` enables an attempt, not a guaranteed success.
Disable the button during the mutation. On success, wait for response and
`trip.started`; on failure, show the API error and refetch current trip.

### 9.2 Arrive at stop

```http
POST /driver/scheduled-trips/{trip_id}/stop-action
Content-Type: multipart/form-data

stop_id=<stop id>
mode=arrive
driver_lat=<current latitude>
driver_lng=<current longitude>
```

The API verifies trip ownership/in-progress state, route membership, stop GPS
radius, stop boarding policy, event state, and previous-stop departure.

After success, `trip.stop_arrived` is emitted first. Departure eligibility is
then evaluated.

### 9.3 Depart from stop

`trip.departure_allowed` is emitted only when persisted state satisfies:

1. Trip is in progress.
2. Arrival exists at this stop.
3. Departure does not exist.
4. The route stop allows deboarding/departure under its route policy.
5. For non-first stops, previous stop departure exists.
6. `assume_time_diff_minutes` for this stop has elapsed since previous
   departure.
7. Every normally booked passenger who is `boarded` and due to drop here has a
   DROP scan.

The check runs:

- After successful arrival.
- When the scheduled minimum-time callback fires.
- After each accepted normal QR/OTP drop scan.
- During server startup timer restoration.
- On driver reconnect.

Depart mutation:

```http
POST /driver/scheduled-trips/{trip_id}/stop-action
Content-Type: multipart/form-data

stop_id=<active stop id>
mode=depart
driver_lat=<current latitude>
driver_lng=<current longitude>
```

The API repeats state/time/passenger checks and current GPS validation. It may
reject even after an event if location or concurrent state changed. Always show
the API error and refetch.

On successful departure, booked passengers who failed to board at their pickup
stop can be marked missed in the same committed operation. The resulting
`trip.stop_departed` invalidation covers the manifest refresh.

### 9.4 End trip

```http
POST /driver/scheduled-trips/{trip_id}/end
Content-Type: multipart/form-data

lat=<current latitude>
lng=<current longitude>
```

Existing API checks remain authoritative: all stop arrival/departure records,
normal board/drop balance, planned end time, last-stop GPS, and RFID missing
drop settlement. Success emits `trip.completed`.

Emergency end and driver cancellation continue using their existing APIs and
emit `trip.premature_ended` or `trip.cancelled` after commit.

## 10. Required driver UI state machine

Maintain action eligibility separately from fetched trip data:

```ts
interface DriverActionState {
  socketSynchronized: boolean;
  start: {
    tripId: string;
    allowed: boolean;
    windowEndsAt: string | null;
  } | null;
  departure: {
    tripId: string;
    stopId: string;
    sequenceNo: number | null;
    allowed: boolean;
  } | null;
}
```

Reducer rules:

| Input | State transition |
| --- | --- |
| Socket connecting/disconnected | `socketSynchronized=false`; clear/disable both action flags. |
| `channel.connected` | Clear both flags and refetch current trip; set synchronized only after initial query refresh is settled. |
| `trip.created` | Clear old flags for other trips; Start remains false. |
| `trip.start_allowed` | Store the Start candidate by trip ID and `start_window_ends_at`, even if reconnect-time HTTP data has not loaded yet. Render enabled only when the candidate ID matches the current scheduled trip. |
| Start mutation begins | Temporarily disable Start to prevent double submission. |
| `trip.started` | Clear Start and any old departure state. |
| `trip.stop_arrived` | Clear departure state, then associate a false Depart flag with this trip/stop. |
| `trip.departure_allowed` | Store the Depart candidate by trip and stop IDs, even if reconnect-time HTTP data has not loaded yet. Render enabled only when both IDs match current HTTP state. |
| Depart mutation begins | Temporarily disable Depart. |
| `trip.stop_departed` | Clear departure for the departed stop. |
| `trip.completed`, `trip.cancelled`, `trip.premature_ended` | Clear Start and Depart state. |
| `passenger.scan_completed` | Refresh manifests; do not directly change Depart flag. |

Always compare trip and stop IDs in the final button selector. Retaining a
candidate until current-trip HTTP data loads prevents reconnect-time
eligibility from being lost; mismatched old candidates still cannot enable a
button.

```ts
const canStart =
  actionState.socketSynchronized &&
  actionState.start?.allowed === true &&
  actionState.start.tripId === currentTrip?.id;

const canDepart =
  actionState.socketSynchronized &&
  actionState.departure?.allowed === true &&
  actionState.departure.tripId === currentTrip?.id &&
  actionState.departure.stopId === activeStop?.id;
```

## 11. Copy-paste TypeScript driver socket

```ts
export type DriverRefreshEvent =
  | "channel.connected"
  | "route.created"
  | "route.updated"
  | "trip.created"
  | "trip.start_allowed"
  | "trip.started"
  | "trip.stop_arrived"
  | "trip.departure_allowed"
  | "trip.stop_departed"
  | "trip.completed"
  | "trip.cancelled"
  | "trip.premature_ended"
  | "booking.changed"
  | "passenger.scan_completed"
  | "rfid.scan_completed";

export interface DriverRefreshMessage {
  type: "api.refresh";
  event: DriverRefreshEvent;
  audience: "driver";
  resources: string[];
  endpoints: string[];
  data: Record<string, unknown>;
  occurred_at: string;
}

type ConnectionState = "connecting" | "connected" | "disconnected";

interface DriverRefreshSocketOptions {
  apiBaseUrl: string;
  accessToken: string;
  onEvent: (event: DriverRefreshMessage) => void; // eligibility reducer
  onRefreshBatch: (events: DriverRefreshMessage[]) => void; // query cache
  onConnectionState?: (state: ConnectionState) => void;
  onAuthenticationFailure?: () => void;
  onProtocolError?: (value: unknown) => void;
}

export function createDriverRefreshSocket(options: DriverRefreshSocketOptions) {
  const retryDelays = [1_000, 2_000, 5_000, 10_000, 30_000];
  let token = options.accessToken;
  let socket: WebSocket | null = null;
  let retryAttempt = 0;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let batchTimer: ReturnType<typeof setTimeout> | null = null;
  let pendingEvents: DriverRefreshMessage[] = [];
  let stopped = true;
  let generation = 0;

  const buildUrl = () => {
    const url = new URL(options.apiBaseUrl);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.pathname = `${url.pathname.replace(/\/$/, "")}/driver/ws/refresh`;
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

  const enqueue = (event: DriverRefreshMessage) => {
    options.onEvent(event); // action state should react immediately
    pendingEvents.push(event);
    if (batchTimer === null) batchTimer = setTimeout(flushBatch, 100);
  };

  const isRefreshMessage = (value: unknown): value is DriverRefreshMessage => {
    if (typeof value !== "object" || value === null) return false;
    const item = value as Record<string, unknown>;
    return (
      item.type === "api.refresh" &&
      item.audience === "driver" &&
      typeof item.event === "string" &&
      Array.isArray(item.resources) &&
      Array.isArray(item.endpoints) &&
      typeof item.data === "object" &&
      item.data !== null &&
      typeof item.occurred_at === "string"
    );
  };

  const scheduleReconnect = () => {
    if (stopped || retryTimer !== null || !navigator.onLine) return;
    const base = retryDelays[Math.min(retryAttempt, retryDelays.length - 1)];
    retryAttempt += 1;
    const delay = Math.round(base * (0.8 + Math.random() * 0.4));
    retryTimer = setTimeout(() => {
      retryTimer = null;
      connect();
    }, delay);
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

For React Native/Flutter/native clients, replace browser online listeners with
the platform network API. Keep the same close-code and heartbeat behavior.

## 12. Reference action-state reducer

```ts
export const initialDriverActionState: DriverActionState = {
  socketSynchronized: false,
  start: null,
  departure: null,
};

export function reduceDriverRefreshEvent(
  state: DriverActionState,
  message: DriverRefreshMessage,
): DriverActionState {
  const tripId = typeof message.data.trip_id === "string"
    ? message.data.trip_id
    : null;

  switch (message.event) {
    case "channel.connected":
      return { socketSynchronized: false, start: null, departure: null };

    case "trip.created":
      return { ...state, start: null, departure: null };

    case "trip.start_allowed":
      if (!tripId) return state;
      return {
        ...state,
        start: {
          tripId,
          allowed: true,
          windowEndsAt:
            typeof message.data.start_window_ends_at === "string"
              ? message.data.start_window_ends_at
              : null,
        },
      };

    case "trip.started":
      if (!tripId) return state;
      return {
        ...state,
        start: state.start?.tripId === tripId ? null : state.start,
        departure:
          state.departure?.tripId === tripId ? null : state.departure,
      };

    case "trip.stop_arrived": {
      if (!tripId) return state;
      const stopId = typeof message.data.stop_id === "string"
        ? message.data.stop_id
        : "";
      return {
        ...state,
        departure: {
          tripId,
          stopId,
          sequenceNo:
            typeof message.data.sequence_no === "number"
              ? message.data.sequence_no
              : null,
          allowed: false,
        },
      };
    }

    case "trip.departure_allowed": {
      const stopId = typeof message.data.stop_id === "string"
        ? message.data.stop_id
        : null;
      if (!tripId || !stopId) return state;
      return {
        ...state,
        departure: {
          tripId,
          stopId,
          sequenceNo:
            typeof message.data.sequence_no === "number"
              ? message.data.sequence_no
              : null,
          allowed: true,
        },
      };
    }

    case "trip.stop_departed": {
      const stopId = typeof message.data.stop_id === "string"
        ? message.data.stop_id
        : null;
      const isMatchingCandidate =
        state.departure?.tripId === tripId &&
        state.departure?.stopId === stopId;
      return isMatchingCandidate ? { ...state, departure: null } : state;
    }

    case "trip.completed":
    case "trip.cancelled":
    case "trip.premature_ended":
      if (!tripId) return state;
      return {
        ...state,
        start: state.start?.tripId === tripId ? null : state.start,
        departure:
          state.departure?.tripId === tripId ? null : state.departure,
      };

    default:
      return state;
  }
}
```

After the `channel.connected` query batch succeeds, set
`socketSynchronized=true`. On socket disconnect, reset to the initial state.

## 13. TanStack Query reference invalidator

Adjust key names to the existing frontend query-key factory.

```ts
import type { QueryClient, QueryKey } from "@tanstack/react-query";

export async function applyDriverRefreshBatch(
  queryClient: QueryClient,
  events: DriverRefreshMessage[],
) {
  const resources = new Set(events.flatMap((event) => event.resources));
  const tripIds = new Set(
    events
      .map((event) => event.data.trip_id)
      .filter((id): id is string => typeof id === "string" && id.length > 0),
  );
  const routeIds = new Set(
    events
      .map((event) => event.data.route_id)
      .filter((id): id is string => typeof id === "string" && id.length > 0),
  );
  const stopIds = new Set(
    events
      .map((event) => event.data.stop_id)
      .filter((id): id is string => typeof id === "string" && id.length > 0),
  );

  const keys: QueryKey[] = [];
  if (resources.has("routes")) keys.push(["driver", "routes"]);
  if (resources.has("route_details")) {
    for (const id of routeIds) keys.push(["driver", "routeDetails", id]);
  }
  if (resources.has("current_trip")) keys.push(["driver", "currentTrip"]);
  if (resources.has("trip_details")) {
    for (const id of tripIds) keys.push(["driver", "tripDetails", id]);
  }
  if (resources.has("trip_stops")) {
    for (const id of tripIds) keys.push(["driver", "tripStops", id]);
  }
  if (resources.has("trip_bookings")) {
    for (const id of tripIds) keys.push(["driver", "tripBookings", id]);
  }
  if (resources.has("current_trip_passengers")) {
    keys.push(["driver", "currentTripPassengers"]);
  }
  if (resources.has("stop_passengers")) {
    for (const tripId of tripIds) {
      for (const stopId of stopIds) {
        keys.push(["driver", "stopPassengers", tripId, stopId]);
      }
    }
    keys.push(["driver", "stopPassengers"]); // prefix fallback
  }
  if (resources.has("drop_events")) {
    for (const id of tripIds) keys.push(["driver", "dropEvents", id]);
  }
  if (resources.has("rfid_scan_details")) {
    for (const id of tripIds) keys.push(["driver", "rfidScanDetails", id]);
  }

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
const driverRefresh = createDriverRefreshSocket({
  apiBaseUrl: config.apiBaseUrl,
  accessToken: auth.accessToken,
  onEvent: (event) => {
    driverActionStore.applyRefreshEvent(event);
  },
  onRefreshBatch: (events) => {
    void applyDriverRefreshBatch(queryClient, events).then(() => {
      if (events.some((event) => event.event === "channel.connected")) {
        driverActionStore.markSocketSynchronized();
      }
    });
  },
  onConnectionState: (state) => {
    driverUiStore.setRefreshSocketState(state);
    if (state !== "connected") driverActionStore.clearEligibility();
  },
  onAuthenticationFailure: () => auth.logoutAndRouteToLogin(),
  onProtocolError: (value) => telemetry.capture("driver_ws_protocol", value),
});

// After login/session restoration:
driverRefresh.start();

// If the session token changes:
driverRefresh.replaceAccessToken(auth.accessToken);

// Before logout/account switch/application teardown:
driverRefresh.stop();
```

## 14. Driver acceptance checklist

- Driver connects and receives ready/initial sync.
- Passenger/admin token is rejected without reconnect loop.
- Heartbeat keeps connection alive beyond 30 seconds.
- Reconnect clears stale buttons and refreshes current trip.
- Route creation/status/stops/fares refresh route screens.
- Creating a trip refreshes current trip but does not prematurely enable Start.
- `trip.start_allowed` enables only the matching trip and displays/obeys the
  deadline.
- Start API still sends current GPS and handles rejection.
- Arrival produces `trip.stop_arrived` before any immediate
  `trip.departure_allowed`.
- Depart stays disabled while minimum time or passenger drop requirements are
  incomplete.
- The last required QR/OTP drop can cause `trip.departure_allowed`.
- A stale eligibility candidate for another trip/stop cannot enable a button.
- Successful departure clears the button and refreshes next-stop progress.
- Booking changes/scans update manifests on every logged-in driver device.
- Accepted RFID scans refresh driver RFID detail; rejected scans do not.
- Completion/cancel/premature end clears all action state.
- Notification copy still comes from `/notifications/ws` without duplicate
  refresh-event toasts.
- Logout closes socket and cancels retries.
- Production uses one WebSocket-serving backend process/replica until shared
  pub/sub exists.
