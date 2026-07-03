# API refresh WebSockets

The backend exposes two authenticated, role-specific refresh channels:

- Passenger: `wss://<host>/passenger/ws/refresh?token=<session-token>`
- Driver: `wss://<host>/driver/ws/refresh?token=<session-token>`

The normal session token is accepted through the `token` query parameter. A
non-browser client may instead send `Authorization: Bearer <session-token>`.
Passenger tokens cannot connect to the driver channel and vice versa. Invalid
or mismatched credentials are closed with WebSocket code `1008`.

## Client behavior

On connection, the server sends `ws.ready`, followed by an `api.refresh` event
with `event: "channel.connected"`. The client should use that first refresh to
sync the current screen after a reconnect.

Every change event has this shape:

```json
{
  "type": "api.refresh",
  "event": "trip.stop_arrived",
  "audience": "passenger",
  "resources": ["current_bookings", "trip_status"],
  "endpoints": [
    "/passenger/bookings/current",
    "/passenger/bookings/{booking_id}/current-status"
  ],
  "data": {
    "trip_id": "...",
    "stop_id": "..."
  },
  "occurred_at": "2026-07-03T06:30:00+00:00"
}
```

`endpoints` contains patterns, not URLs that must all be fetched. Match the
event's `resources` against the data used by the current page, substitute IDs
from `data` or local state, and refetch only those APIs. Debounce duplicate
events when several mutations happen together.

The server sends `{"type":"ping"}` every 15 seconds. Reply with
`{"type":"pong"}`. A client can also send `ping`; the server replies with
`pong`.

```javascript
const role = "driver"; // or "passenger"
const ws = new WebSocket(
  `${apiBase.replace(/^http/, "ws")}/${role}/ws/refresh?token=${encodeURIComponent(token)}`,
);

ws.onmessage = ({ data }) => {
  const message = JSON.parse(data);
  if (message.type === "ping") {
    ws.send(JSON.stringify({ type: "pong" }));
    return;
  }
  if (message.type === "api.refresh") {
    refreshVisibleResources(message.resources, message.data);
  }
};
```

## Emitted events

Passenger clients can receive:

- `route.created`, `route.updated`
- `trip.created`, `trip.catalog_changed`, `trip.started`, `trip.stop_arrived`,
  `trip.stop_departed`, `trip.completed`, `trip.cancelled`,
  `trip.premature_ended`
- `booking.changed`, `trip.seat_availability_changed`
- `passenger.scan_completed`, `rfid.scan_completed`,
  `trip.rfid_occupancy_changed`

Driver clients can receive:

- `route.created`, `route.updated`, `trip.created`
- `trip.start_allowed` when the 15-minute start window opens. GPS validation
  is still performed by the start API.
- `trip.started`, `trip.stop_arrived`, `trip.stop_departed`,
  `trip.completed`, `trip.cancelled`, `trip.premature_ended`
- `trip.departure_allowed` only after the stop has been reached, the minimum
  travel-time gate has elapsed, and every passenger due to drop there has a
  drop scan.
- `booking.changed`, `passenger.scan_completed`, `rfid.scan_completed`

Events concerning a passenger's private booking are sent only to that
passenger and the assigned driver. Catalog and anonymous availability changes
are broadcast to the applicable role.

The hub is process-local, matching the existing notification WebSocket. Run a
single application worker unless fan-out is later backed by Redis pub/sub.
