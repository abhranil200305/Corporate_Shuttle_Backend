# Admin API refresh WebSocket: complete frontend integration

This is the standalone implementation contract for the admin application. An
admin frontend developer should not need the passenger guide, driver guide, or
backend source code to integrate this feature.

## 1. What this socket is

Connect to:

```text
wss://<api-host>/admin/ws/refresh?token=<URL-encoded-access-token>
```

The access token must belong to an active user whose role is `admin`. Missing,
invalid, expired, logged-out, or non-admin credentials are rejected with close
code `1008`.

The socket is a cache-invalidation channel. It reports that one or more admin
HTTP views may be stale. It does **not** return the new database object and it
does **not** replace any admin API. The required flow is always:

```text
api.refresh message -> invalidate matching query groups -> refetch HTTP API
```

HTTP remains the source of truth. Never update an admin record from the event
payload alone and never treat an event as proof that the current admin's own
mutation succeeded.

## 2. Connections the admin app should keep

After authentication/session restoration, keep these independently:

| Connection | Purpose |
| --- | --- |
| `/admin/ws/refresh` | Admin dashboard/list/detail cache invalidation. |
| `/notifications/ws` | Human-readable persisted notifications and notification read state. |

The refresh socket does not replace the notification socket. The passenger
seat-map socket and driver refresh socket must not be opened by an admin app.

Open one refresh socket per running browser tab. The backend supports multiple
tabs, devices, and concurrently logged-in admins. Every connected admin receives
admin broadcasts, including the admin who performed the mutation.

## 3. Wire protocol

### 3.1 Ready message

Immediately after authentication and socket registration:

```json
{
  "type": "ws.ready",
  "channel": "admin",
  "user_id": "admin-user-uuid",
  "message": "API refresh WebSocket authenticated."
}
```

`ws.ready` proves the role/session check passed. It is not an invalidation.

### 3.2 Initial synchronization

The next message is:

```json
{
  "type": "api.refresh",
  "event": "channel.connected",
  "audience": "admin",
  "resources": [
    "dashboard",
    "analytics",
    "drivers",
    "passengers",
    "vehicles",
    "trips",
    "bookings",
    "support_tickets",
    "payout_dashboard"
  ],
  "endpoints": [
    "/admin/view/all-drivers",
    "/admin/view/all-passengers",
    "/admin/vehicles/inspection-statuses",
    "/admin/trips/monitor",
    "/admin/tickets",
    "/admin/payouts/dashboard",
    "/admin/analytics/most-booked-routes",
    "/admin/analytics/top-pickup-stops"
  ],
  "data": {"reason": "initial_sync"},
  "occurred_at": "2026-07-03T06:30:00+00:00"
}
```

Process this after **every** connection and reconnect. It repairs the common
dashboard state after events were missed while offline. Screens must still
fetch their own query on mount; a socket event is never a substitute for an
initial HTTP fetch.

### 3.3 Heartbeat

- The server sends `{"type":"ping"}` every 15 seconds.
- Reply immediately with `{"type":"pong"}`.
- If the server records no pong for 30 seconds, it closes the socket with
  `1001`.
- A client may send `{"type":"ping"}`; the server answers
  `{"type":"pong"}`.
- Do not send acknowledgements for `api.refresh`.

### 3.4 Refresh envelope

```ts
type AdminRefreshEvent =
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
  | "rfid.scan_completed"
  | "admin.users_changed"
  | "admin.drivers_changed"
  | "admin.passengers_changed"
  | "admin.vehicles_changed"
  | "admin.rfid_changed"
  | "admin.support_changed"
  | "admin.reviews_changed"
  | "admin.payouts_changed"
  | "admin.settings_changed"
  | "admin.incidents_changed";

interface AdminApiRefreshMessage {
  type: "api.refresh";
  event: AdminRefreshEvent;
  audience: "admin";
  resources: string[];
  endpoints: string[];
  data: Record<string, unknown>;
  occurred_at: string; // UTC ISO-8601 server emission time
}
```

Use `resources` as the stable cache contract. `endpoints` is an exhaustive
human-readable list of the primary API patterns in that group. Values such as
`{trip_id}` are placeholders, not URLs to call literally. `data` is optional
context for telemetry, focused refreshes, and UI notices; its shape varies.

Unknown events/resources must be tolerated. Log them and invalidate any known
resources from the message. Do not disconnect because the backend added an
event.

## 4. Complete event-to-admin-surface mapping

### 4.1 Connection and users/devices

| Event | Resources to invalidate | Primary GET APIs |
| --- | --- | --- |
| `channel.connected` | `dashboard`, `analytics`, `drivers`, `passengers`, `vehicles`, `trips`, `bookings`, `support_tickets`, `payout_dashboard` | `/admin/view/all-drivers`, `/admin/view/all-passengers`, `/admin/vehicles/inspection-statuses`, `/admin/trips/monitor`, `/admin/tickets`, `/admin/payouts/dashboard`, both `/admin/analytics/*` APIs |
| `admin.users_changed` | `users`, `drivers`, `passengers`, `user_details`, `devices` | `/admin/view/all-drivers`, `/admin/view/all-passengers`, `/admin/passengers`, `/admin/reports/inactive-users`, `/admin/{user_id}/full_details`, `/admin/devices`, `/admin/users/{user_id}/devices` |

`admin.users_changed` is emitted for signup, login, logout, self-service device
removal, and successful admin device-session removal. Device dashboards should
therefore update across admin tabs without polling.

### 4.2 Driver, passenger, and vehicle administration

| Event | Resources to invalidate | Primary GET APIs |
| --- | --- | --- |
| `admin.drivers_changed` | `drivers`, `driver_details`, `vehicles`, `vehicle_inspections`, `available_vehicles`, `driver_ratings`, `payout_drivers` | `/admin/view/all-drivers`, `/admin/driver/{user_id}`, `/admin/driver/vehicle/{user_id}`, `/admin/drivers/verified_data`, `/admin/vehicles/inspection-statuses`, `/admin/available_vehicles`, `/admin/driver-ratings`, `/admin/payouts/drivers` |
| `admin.passengers_changed` | `passengers`, `passenger_details`, `user_details`, `passenger_current_trip`, `passenger_bookings`, `transactions` | `/admin/view/all-passengers`, `/admin/passengers`, `/admin/passenger/{user_id}`, `/admin/{user_id}/full_details`, `/admin/passengers/{user_id}/current-trip`, `/admin/user/{user_id}/bookings/detailed`, `/admin/{user_id}/transaction_history` |
| `admin.vehicles_changed` | `vehicles`, `vehicle_details`, `vehicle_inspections`, `available_vehicles`, `drivers` | `/admin/vehicles/inspection-statuses`, `/admin/vehicle/details/{vehicle_id}`, `/admin/available_vehicles`, `/admin/view/all-drivers` |

Driver events cover driver-created/updated profiles, KYC document updates and
submission, admin activation/deactivation/verification, and associated driver
administration. Vehicle events cover driver registration/update/submission and
admin vehicle verification/inspection. Passenger events cover passenger profile
and picture changes plus traveller-profile create/update/delete.

### 4.3 Routes and fares

| Event | Resources to invalidate | Primary GET APIs |
| --- | --- | --- |
| `route.created` | `stops`, `routes`, `route_details`, `fares`, `route_reports` | `/admin/stops/all`, `/admin/routes/all`, `/admin/routes/{route_id}`, `/admin/routes/{route_id}/fares`, `/admin/routes/{route_id}/full-report` |
| `route.updated` | Same as `route.created` | Same as `route.created` |

These events cover stop bulk upload, single-stop upsert, unused-stop deletion,
route creation, route-stop append, route activation/deactivation, and bulk fare
create/update. `data.reason` identifies the operation; IDs are included where
the source operation has them.

### 4.4 Trips and live operations

The following all invalidate the same admin trip group:

- `trip.created`
- `trip.start_allowed`
- `trip.started`
- `trip.stop_arrived`
- `trip.departure_allowed`
- `trip.stop_departed`
- `trip.completed`
- `trip.cancelled`
- `trip.premature_ended`

Resources:

```text
dashboard, trips, trip_details, trip_manifest, bookings, incidents,
available_vehicles
```

Primary APIs:

```text
GET /admin/trips/monitor
GET /admin/trips/{trip_id}
GET /admin/trips/{trip_id}/bookings
GET /admin/{trip_id}/passengers
GET /admin/booking/{booking_id}
GET /admin/trip/{trip_id}/status-only
GET /admin/incidents
GET /admin/available_vehicles
```

Trip events are broadcast to every connected admin. `data.trip_id` is present;
common additional keys are `route_id`, `stop_id`, `sequence_no`, and `reason`.
For `trip.start_allowed`, timing fields can include `planned_start_at` and
`start_window_ends_at`. Admin UI must not use `trip.start_allowed` or
`trip.departure_allowed` to perform driver actions; they are operational state
signals for monitor/detail refreshes only.

Sources include driver lifecycle actions, admin cancel/premature-end/manual
completion, automatic cancellation of unstarted trips, and restored scheduled
eligibility timers after backend startup.

### 4.5 Bookings, manifests, payments, and analytics

| Event | Resources to invalidate | Primary GET APIs |
| --- | --- | --- |
| `booking.changed` | `dashboard`, `bookings`, `booking_sessions`, `trip_manifest`, `transactions`, `passengers`, `payout_bookings`, `ratings`, `analytics` | `/admin/booking-sessions`, `/admin/booking-sessions/{booking_session_id}`, `/admin/trips/{trip_id}/bookings`, `/admin/bookings/{booking_id}/trip-detail`, `/admin/booking/{booking_id}`, `/admin/bookings/{booking_id}/rating`, `/admin/user/{user_id}/bookings/detailed`, `/admin/transactions/all`, `/admin/{user_id}/transaction_history`, both `/admin/analytics/*` APIs, `/admin/payouts/bookings` |
| `passenger.scan_completed` | Same booking group | Same booking APIs |

`booking.changed` is emitted after material booking/session/payment state
changes, including passenger creation/payment verification/cancellation,
per-seat cancellation, admin no-show, and material payment reconciliation
outcomes. Its usual data is:

```json
{
  "trip_id": "trip-uuid",
  "booking_id": "booking-uuid-or-null",
  "booking_session_id": "session-uuid-or-null",
  "reason": "machine-readable-reason"
}
```

Unlike passenger broadcasts, the admin event may include booking identifiers.
Still fetch the API; nullable IDs and evolving reason values are valid.

`passenger.scan_completed` follows an accepted QR/OTP board or drop scan and
includes trip/booking/passenger scan context. Refresh the manifest and booking
views. Rejected scans do not emit it.

### 4.6 RFID operations

| Event | When it is used |
| --- | --- |
| `admin.rfid_changed` | Admin device/card/recharge/ride reversal/payout-transfer/seat-policy mutations and passenger recharge creation/verification. |
| `rfid.scan_completed` | An RFID board/drop scan was accepted. |

Both invalidate `rfid_devices`, `rfid_cards`, `rfid_ledger`,
`rfid_recharges`, `rfid_rides`, `rfid_payouts`, and `rfid_settings`, covering:

```text
GET /admin/rfid/device-vehicle-options
GET /admin/rfid/card-options
GET /admin/rfid/devices
GET /admin/rfid/cards
GET /admin/rfid/cards/{card_id}
GET /admin/rfid/cards/{card_id}/ledger
GET /admin/rfid/cards/{card_id}/recharges
GET /admin/rfid/rides/payout-ready
GET /admin/rfid/payout-transfers
GET /admin/rfid/payout-transfer-reversals
GET /admin/rfid/payout-transfers/{transfer_id}
GET /admin/rfid/rides/{rfid_ride_id}/money-detail
GET /admin/rfid/payout-operations-summary
GET /admin/rfid/seat-policy
```

Accepted scan data normally contains `trip_id`, `route_id`, `stop_id`, and
`scan_type`. Treat it as a lookup hint only. Rejected scans do not cause
refresh events.

### 4.7 Support, incidents, and reviews

| Event | Resources | GET APIs |
| --- | --- | --- |
| `admin.support_changed` | `support_tickets`, `incidents` | `/admin/tickets`, `/admin/incidents` |
| `admin.incidents_changed` | `incidents`, `trips`, `trip_details` | `/admin/incidents`, `/admin/trips/monitor`, `/admin/trips/{trip_id}` |
| `admin.reviews_changed` | `reviews`, `driver_ratings`, `review_stats` | `/admin/reviews`, `/admin/reviews/drivers`, `/admin/reviews/stats`, `/admin/driver-ratings` |

Support refreshes follow passenger/driver ticket creation and successful admin
ticket action/support creation. Incident refreshes follow admin trip resolution.
Review refreshes follow passenger rating creation.

### 4.8 Payout administration

`admin.payouts_changed` invalidates:

```text
payout_settings, payout_drivers, payout_bookings, payout_adjustments,
payout_transfers, refunds, payout_dashboard, transactions
```

Covered APIs:

```text
GET /admin/payouts/settings
GET /admin/payouts/drivers
GET /admin/payouts/drivers/{driver_user_id}
GET /admin/payouts/bookings
GET /admin/payouts/bookings/{booking_id}
GET /admin/payouts/bookings/{booking_id}/adjustments
GET /admin/payouts/drivers/{driver_user_id}/open-adjustments
GET /admin/payouts/transfers
GET /admin/payouts/transfers/{transfer_id}
GET /admin/payouts/refunds
GET /admin/payouts/dashboard
GET /admin/payouts/drivers/{driver_user_id}/linked-account/provider
GET /admin/transactions/all
```

It follows successful payout settings, driver payout details/link/eligibility,
adjustment, trigger, bulk/batch, refund reconciliation, and linked-account
mutations. Driver payout-account setup under
`/admin/drivers/{driver_id}/setup-payout-account` is included.

Booking events also invalidate `payout_bookings` because booking state can
change payout eligibility. RFID payout operations use the RFID event group,
not this general payout group.

### 4.9 Settings and commercial rules

`admin.settings_changed` invalidates `device_settings`, `commercial_rules`, and
`rfid_settings`:

```text
GET /admin/device-settings
GET /admin/commercial-rules
GET /admin/commercial-rules/{rule_id}
GET /admin/rfid/seat-policy
```

It follows device settings changes and commercial-rule create/update/delete or
status changes. RFID seat-policy mutations emit `admin.rfid_changed`, which also
invalidates `rfid_settings`.

## 5. Exact source-operation coverage

### 5.1 Mutations performed by an admin

Only successful responses (`200` through `399`) produce middleware-driven
admin refresh events. Failed validation/auth/conflict/server responses do not.

| Admin mutation path family | Event |
| --- | --- |
| `/admin/users/*/devices*` | `admin.users_changed` |
| `/admin/driver/*` | `admin.drivers_changed` |
| `/admin/vehicle/*` | `admin.vehicles_changed` |
| `/admin/rfid/*` | `admin.rfid_changed` |
| `/admin/tickets/*`, `/admin/support/create` | `admin.support_changed` |
| `/admin/resolve-trip/*` | `admin.incidents_changed` |
| `/admin/payouts/*`, `/admin/drivers/*/setup-payout-account` | `admin.payouts_changed` |
| `/admin/device-settings`, `/admin/commercial-rules*` | `admin.settings_changed` |

Middleware-driven event data is:

```json
{
  "reason": "admin_mutation_completed",
  "method": "PATCH",
  "path": "/admin/payouts/settings",
  "status_code": 200
}
```

Route/stop/fare, trip lifecycle, and booking no-show endpoints intentionally
bypass the generic middleware because their endpoint/service implementations
emit richer `route.*`, `trip.*`, or `booking.changed` events after commit.

`POST /admin/send-notification/{user_id}` does not invalidate an admin domain
surface. The recipient's notification channel handles that operation.

### 5.2 Mutations performed outside the admin app

| Source | Resulting admin event |
| --- | --- |
| Signup, login, logout, device removal | `admin.users_changed` |
| Passenger profile/picture/traveller-profile changes | `admin.passengers_changed` |
| Driver profile or KYC changes | `admin.drivers_changed` |
| Driver vehicle registration/update/submission | `admin.vehicles_changed` |
| Passenger or driver support creation | `admin.support_changed` |
| Passenger rating creation | `admin.reviews_changed` |
| Passenger RFID recharge create/verify | `admin.rfid_changed` |
| Driver trip create/lifecycle/cancel/emergency end | Corresponding `trip.*` event |
| Passenger booking/session/payment/cancel | `booking.changed` |
| Accepted QR/OTP scan | `passenger.scan_completed` |
| Accepted RFID scan | `rfid.scan_completed` and RFID occupancy refresh |
| Payment reconciliation material state transition | `booking.changed` |
| Automatic unstarted-trip cancellation | `trip.cancelled` |

## 6. Copy-paste WebSocket client

This browser TypeScript implementation handles authentication, heartbeat,
reconnect, auth failure, malformed messages, and duplicate invalidation bursts.

```ts
type RefreshHandler = (message: AdminApiRefreshMessage) => void;

interface AdminRefreshClientOptions {
  apiBaseUrl: string; // e.g. https://api.example.com
  getAccessToken: () => string | null;
  onRefresh: RefreshHandler;
  onAuthRejected: () => void;
  onStateChange?: (state: "connecting" | "open" | "closed") => void;
}

export class AdminRefreshClient {
  private socket: WebSocket | null = null;
  private stopped = true;
  private retryAttempt = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly options: AdminRefreshClientOptions) {}

  start(): void {
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    const socket = this.socket;
    this.socket = null;
    if (socket && socket.readyState < WebSocket.CLOSING) {
      socket.close(1000, "logout_or_app_shutdown");
    }
    this.options.onStateChange?.("closed");
  }

  private connect(): void {
    if (this.stopped || this.socket) return;
    const token = this.options.getAccessToken();
    if (!token) return;

    this.options.onStateChange?.("connecting");
    const base = new URL(this.options.apiBaseUrl);
    base.protocol = base.protocol === "https:" ? "wss:" : "ws:";
    base.pathname = "/admin/ws/refresh";
    base.search = "";
    base.searchParams.set("token", token);

    const socket = new WebSocket(base.toString());
    this.socket = socket;

    socket.onopen = () => {
      this.retryAttempt = 0;
      this.options.onStateChange?.("open");
    };

    socket.onmessage = (event) => {
      let payload: unknown;
      try {
        payload = JSON.parse(String(event.data));
      } catch {
        return;
      }
      if (!payload || typeof payload !== "object") return;
      const message = payload as Record<string, unknown>;

      if (message.type === "ping") {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: "pong" }));
        }
        return;
      }
      if (message.type !== "api.refresh" || message.audience !== "admin") {
        return;
      }
      if (!Array.isArray(message.resources)) return;
      this.options.onRefresh(message as unknown as AdminApiRefreshMessage);
    };

    socket.onclose = (event) => {
      if (this.socket === socket) this.socket = null;
      this.options.onStateChange?.("closed");
      if (this.stopped) return;
      if (event.code === 1008) {
        this.options.onAuthRejected();
        return;
      }
      this.scheduleReconnect();
    };

    socket.onerror = () => {
      // onclose owns retry scheduling; do not create a second retry here.
    };
  }

  private scheduleReconnect(): void {
    const delays = [1000, 2000, 5000, 10000, 30000];
    const base = delays[Math.min(this.retryAttempt, delays.length - 1)];
    this.retryAttempt += 1;
    const delay = base + Math.floor(Math.random() * 500);
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.connect();
    }, delay);
  }
}
```

Instantiate it once after auth restoration and call `stop()` before clearing
auth state. Never log `base.toString()` because it contains the access token.
Use `wss://` in production.

## 7. Copy-paste TanStack Query invalidation

The recommended query-key rule is that the first key element is one of the
backend resource names:

```ts
useQuery({ queryKey: ["trips", "monitor", filters], queryFn: ... });
useQuery({ queryKey: ["trip_details", tripId], queryFn: ... });
useQuery({ queryKey: ["payout_drivers", filters], queryFn: ... });
```

Then the complete invalidator is:

```ts
import type { QueryClient } from "@tanstack/react-query";

const pendingResources = new Set<string>();
let flushTimer: ReturnType<typeof setTimeout> | null = null;

export function invalidateAdminRefresh(
  queryClient: QueryClient,
  message: AdminApiRefreshMessage,
): void {
  for (const resource of message.resources) pendingResources.add(resource);

  // Collapse scan/payment/bulk-operation bursts into one latest-state fetch.
  if (flushTimer) return;
  flushTimer = setTimeout(() => {
    const resources = new Set(pendingResources);
    pendingResources.clear();
    flushTimer = null;

    void queryClient.invalidateQueries({
      predicate: (query) => {
        const resource = query.queryKey[0];
        return typeof resource === "string" && resources.has(resource);
      },
      // TanStack refetches active mounted views now; inactive views remain
      // stale and fetch when the admin navigates to them.
      refetchType: "active",
    });
  }, 100);
}
```

Usage:

```ts
const client = new AdminRefreshClient({
  apiBaseUrl: env.API_BASE_URL,
  getAccessToken: () => authStore.getState().accessToken,
  onRefresh: (message) => invalidateAdminRefresh(queryClient, message),
  onAuthRejected: () => authStore.getState().expireSession(),
});

client.start();
```

If the existing application uses different query keys, create a single adapter
from resource name to existing prefixes. Do not scatter event-name switches
across pages. `resources` is intentionally many-to-many: one event may make
several derived endpoints stale.

## 8. Page implementation matrix

Every query used by a page should start with one of these resource keys:

| Admin page/surface | Resource keys |
| --- | --- |
| Home/dashboard | `dashboard`, `analytics`, `payout_dashboard`, plus any visible card groups |
| User devices/sessions | `devices`, `users`, `user_details` |
| Driver list/detail/KYC | `drivers`, `driver_details`, `driver_ratings` |
| Passenger list/detail | `passengers`, `passenger_details`, `user_details` |
| Passenger trip/bookings/history | `passenger_current_trip`, `passenger_bookings`, `transactions` |
| Vehicle list/detail/inspection | `vehicles`, `vehicle_details`, `vehicle_inspections`, `available_vehicles` |
| Stops/routes/fares/report | `stops`, `routes`, `route_details`, `fares`, `route_reports` |
| Trip monitor/detail/status | `trips`, `trip_details`, `dashboard` |
| Trip manifest/passengers | `trip_manifest`, `bookings` |
| Booking/session/detail | `bookings`, `booking_sessions` |
| Transactions/booking analytics | `transactions`, `analytics` |
| RFID device/card/detail | `rfid_devices`, `rfid_cards` |
| RFID ledger/recharge | `rfid_ledger`, `rfid_recharges` |
| RFID rides/transfers/policy | `rfid_rides`, `rfid_payouts`, `rfid_settings` |
| Tickets/incidents | `support_tickets`, `incidents` |
| Reviews/ratings/stats | `reviews`, `driver_ratings`, `review_stats` |
| Payout configuration/drivers | `payout_settings`, `payout_drivers` |
| Payout bookings/adjustments | `payout_bookings`, `payout_adjustments` |
| Payout transfers/refunds/dashboard | `payout_transfers`, `refunds`, `payout_dashboard` |
| Device settings/commercial rules | `device_settings`, `commercial_rules` |

After an admin mutation succeeds, keep using the mutation response for immediate
success/error UI. The socket will also invalidate all admin tabs. It is safe if
the initiating tab both invalidates in `onSuccess` and receives the event;
TanStack Query coalescing/latest-state refetch makes this idempotent.

## 9. Delivery guarantees and UI rules

- Delivery is best effort, process-local, and not replayed.
- A disconnected browser can miss events. `channel.connected` plus normal
  fetch-on-mount behavior repairs state.
- Events may be duplicated or arrive close together. Invalidation must be
  idempotent; use the 100 ms coalescer above.
- Per-socket writes are serialized, but there is no global transaction ordering
  across concurrent API requests.
- All currently connected admins receive the same admin broadcast. Do not use
  the socket as a private admin-to-admin message channel.
- Do not eagerly call every URL in `endpoints`. Invalidate cached resources;
  refetch active screens now and inactive screens when mounted.
- Do not show destructive-operation success toasts based on an event. Use the
  initiating HTTP response.
- A detail screen may optionally compare `data.trip_id`, `booking_id`,
  `user_id`, etc. to prioritize a focused refetch, but it must still support the
  resource-only fallback.

## 10. Close codes and recovery

| Code | Meaning | Required behavior |
| --- | --- | --- |
| `1000` | Normal/intentional close | Reconnect only if auth still exists and the app did not intentionally stop. |
| `1001` | Heartbeat timeout or server shutdown | Reconnect with backoff. |
| `1008` | Missing/invalid/expired/logged-out token or wrong role | Stop retrying; refresh auth if the product supports it, otherwise log out. |
| `1011` | Unexpected server handler error | Reconnect with backoff and report telemetry. |
| `1006` observed by browser | Network/proxy interruption | Reconnect when online. |

Recommended delays are 1 s, 2 s, 5 s, 10 s, then 30 s with jitter. Reset the
attempt counter after a successful open. The provided client implements this.

## 11. Security requirements

- Use only an admin access token on this path.
- Use `wss://` when the API uses HTTPS.
- URL-encode the token; `URLSearchParams` does this in the sample.
- Never log full WebSocket URLs, proxy request URLs, or error objects that expose
  the query string.
- Clear/close the socket before discarding credentials on logout or role switch.
- Defensively ignore a refresh envelope whose `audience` is not `admin`.
- Event data contains operational identifiers. Do not forward it to passenger
  or driver clients.

Native clients that can set WebSocket headers may omit the query token and use:

```http
Authorization: Bearer <access_token>
```

Browser WebSocket APIs cannot set arbitrary Authorization headers, so the query
parameter is the supported browser mechanism.

## 12. Deployment limitation

The refresh hub and eligibility timers are in application memory. With multiple
Uvicorn/Gunicorn workers or multiple replicas, a mutation handled by one process
cannot reach sockets connected to another process.

Until fan-out/timer coordination is moved behind Redis or another broker, run
one WebSocket-serving application process in total: one worker and one replica.
This is an operational constraint, not something the frontend can repair.

## 13. Frontend acceptance checklist

- [ ] Admin token opens `/admin/ws/refresh`; passenger/driver tokens are rejected.
- [ ] `ws.ready.channel` equals `admin`.
- [ ] `channel.connected` invalidates the overview groups after every reconnect.
- [ ] Every server `ping` receives a `pong`.
- [ ] `1008` stops the reconnect loop and enters auth recovery/logout.
- [ ] Other disconnects use bounded backoff with jitter.
- [ ] Query keys use the page matrix resource names or one central adapter.
- [ ] Only active queries refetch immediately; inactive queries become stale.
- [ ] A burst of events is coalesced without losing any resource name.
- [ ] Driver/passenger profile edits update an already-open admin view.
- [ ] Route/fare edits update route lists and details in another admin tab.
- [ ] Trip lifecycle changes update monitor, detail, manifest, and vehicle state.
- [ ] Booking/payment/no-show changes update booking and analytics surfaces.
- [ ] Accepted scans update manifest/RFID operational surfaces.
- [ ] Ticket, review, device, payout, and settings mutations refresh their pages.
- [ ] The initiating tab still trusts its HTTP mutation response, not the event.
- [ ] Two admin tabs/devices both receive the broadcast.
- [ ] Logout closes the socket and no token-bearing URL is logged.
- [ ] Offline/reconnect testing confirms dashboard sync and fetch-on-mount detail
      recovery.

## 14. Backend files for maintainers

| File | Responsibility |
| --- | --- |
| `app/realtime/catalog.py` | Canonical admin event/resource/API mappings. |
| `app/realtime/hub.py` | Role/user/tab registry, fan-out, serialization, cleanup, and timers. |
| `app/realtime/router.py` | `/admin/ws/refresh`, auth/role enforcement, initial sync, and heartbeat. |
| `app/realtime/events.py` | Admin broadcast helper and shared route/trip/booking/scan events. |
| `app/realtime/admin_middleware.py` | Classifies successful admin mutation path families. |
| `app/auth/router.py` | Signup/login/logout/device admin invalidations. |
| `app/passenger/router.py` | Passenger/profile/RFID/review/support invalidations. |
| `app/driver/*` | Driver profile/KYC/vehicle/support/trip/scan invalidations. |
| `app/rfid/router.py` | Accepted RFID scan invalidations. |
| `main.py` | Hub lifecycle, router, middleware, scheduler restoration, and job wiring. |
| `tests/test_api_refresh_hub.py` | Role isolation, admin mapping, targeting, timer, and policy tests. |

No database migration and no new environment variable are required.
