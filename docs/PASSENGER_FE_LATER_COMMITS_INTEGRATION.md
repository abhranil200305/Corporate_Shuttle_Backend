# Passenger frontend integration: GST, travellers, and payment recovery

This is the passenger-app implementation guide for every passenger-facing
change added after the repository restore point `a197ed3` (the restore of
`98db79a5cec42e25c4ae7d6f080143ed628508ea`). It covers these backend commits:

| Commit | Passenger impact |
| --- | --- |
| `bd7ec36` | GST calculation, immutable booking/payment/RFID tax snapshots, invoice and discovery tax fields |
| `3604355`, `c8e1d5d` | GST environment fallback when the settings row does not exist |
| `97e6649` | Strict traveller phone and email validation/normalization |
| `bdc288d` | Safe retry of a pending multi-seat payment and background recovery/refund of late captures |

This document is intentionally passenger-only. The passenger frontend neither
reads nor edits the admin GST settings. Exact TypeScript contracts and endpoint
examples are in [PASSENGER_FE_WIRE_CONTRACTS.md](./PASSENGER_FE_WIRE_CONTRACTS.md).

## 1. Ship this in this order

1. Deploy the backend and run `alembic upgrade head`. GST requires migration
   `8d7f4c2a9b31`. The payment-retry change itself adds no migration.
2. Confirm Razorpay keys are configured. Webhooks are recommended but are not
   required for passenger checkout to recover correctly.
3. Deploy the frontend with the additive response fields typed and rendered.
4. Enable the retry action only for `booking_session.status ===
   "pending_payment"` and while `payment_hold_expires_at` is in the future.
5. Keep the refresh, notification, and seat-map sockets. They solve different
   problems and none replaces the others.

The new response fields are additive, but generated/strict decoders must be
updated before backend deployment if they reject unknown fields.

## 2. What changed in the passenger experience

Before these commits, the UI could treat a configured fare as an undivided
amount, traveller contact validation was permissive, and a dismissed or failed
multi-seat Razorpay checkout had no safe retry operation.

After these commits:

- Every visible fare can include an authoritative GST breakdown.
- A fare shown during discovery is a live calculation; a created booking stores
  an immutable tax snapshot so historical screens do not change when settings
  change later.
- Saved and one-off travellers use the same phone/email rules.
- The backend detects duplicate traveller identities and unsafe overlapping or
  too-close journeys and returns structured errors.
- A pending booking session can reopen the same Razorpay order without
  extending the seat hold or creating another order.
- The retry endpoint reconciles with Razorpay before deciding what to do. It
  may discover that payment already succeeded and confirm the session without
  opening checkout again.
- If the browser callback and webhook are both missed, the payment reconciler
  still detects captured payments. A capture found after the hold/session has
  closed is queued for refund by the existing refund worker.

## 3. Non-negotiable frontend rules

### 3.1 Money and percentage values

Backend `Decimal` fields may be serialized as JSON strings by some clients and
as JSON numbers by others. Accept both at the API boundary:

```ts
type DecimalWire = string | number;

const decimalText = (value: DecimalWire): string => String(value);
```

Use a decimal library or integer paise for arithmetic. Never use binary
floating-point to recompute the amount charged, tax component totals, or refund
amount. Money is rounded half-up to two decimal places; percentage snapshots
are also two decimal places.

The backend is authoritative. The frontend may format or sum provided values
for display, but it must not calculate the Razorpay amount from rates.

### 3.2 Live calculation versus immutable snapshot

Use fields without `_snapshot` on selection/search screens:

- `GET /passenger/route-trip-options`
- `GET /passenger/rfid/route-trip-options`
- `POST /passenger/fare/preview`

Use snapshot fields returned by a booking/session/payment/RFID ride everywhere
after creation. Do not replace them with a new fare preview.

Example: an admin changes GST from 5% to 12% after a passenger paid. Discovery
uses 12%; that passenger's booking, payment history, refund, and invoice keep
the original 5% snapshot.

### 3.3 Gross amount semantics

These fields are always the final passenger-facing charge:

- live `amount` and `fare_amount`
- booking `fare_amount`
- session `total_fare_amount`
- payment `amount`

Do not add `total_tax_amount` to them. In inclusive pricing, tax is already
inside the amount. In exclusive pricing, the backend has already added tax to
produce the gross amount.

For a line-item display:

```text
Fare / taxable value       taxable_amount
CGST                       cgst_amount
SGST                       sgst_amount
IGST                       igst_amount
Rounding adjustment       only available on invoice
Total                      fare_amount or amount
```

Hide zero-valued component rows if desired, but retain all component fields in
the frontend type. Do not assume IGST is permanently zero.

### 3.4 Error envelope

Domain errors use:

```ts
interface DomainErrorBody {
  detail: {
    error: string;
    message: string;
    [key: string]: unknown;
  };
}
```

FastAPI request-validation failures use HTTP 422 and `detail` is an array, not
the domain-error object. Your error adapter must support both shapes.

## 4. GST integration

### 4.1 Configuration behavior the UI needs to understand

The backend resolves GST in this precedence order:

1. persisted default `platform_settings` row;
2. `GST_*` environment values if no row exists;
3. built-in defaults.

Built-in defaults are GST enabled, CGST 2.50%, SGST 2.50%, IGST 0.00%, GST only
on AC routes, and inclusive pricing. These are operational defaults, not values
the passenger UI should copy. The UI must consume each response.

GST is not applicable when any of these is true:

- GST is disabled;
- the total configured rate is zero;
- settings restrict GST to AC routes and this route is not AC.

When not applicable, component rates/amounts and `total_tax_amount` are zero,
while the gross and taxable values equal the configured fare.

### 4.2 Search and fare preview

On each route option render:

- price CTA: `fare_amount` (gross);
- optional base label: `taxable_amount`;
- tax label: `total_tax_amount`;
- tax mode: `gst_inclusive ? "GST included" : "plus GST"`;
- tax applicability: use `gst_applicable`, not `gst_enabled` alone.

`configured_fare_amount` means the route fare before the backend applies its
inclusive/exclusive rule. It is diagnostic/display data. Do not charge it.

When pickup/drop changes, discard the previous preview and request a new one.
Avoid showing stale tax values while the new request is loading.

### 4.3 Booking/session rendering

For each seat, render that booking's snapshot. For the order summary, render
the session totals. The totals are already multiplied by seat count and rounded
by the backend. Do not calculate `per-seat tax * count` as the payment source.

Use `gst_enabled_snapshot` to describe whether GST was enabled at creation and
the component amounts to determine what was actually charged. A snapshot can
have GST enabled but zero tax because the route was non-AC or rates were zero.

### 4.4 Transactions and invoice

Transaction history has payment-level `taxable_amount`, component amounts, and
`total_tax_amount`. The invoice endpoint is available only after a booking is
completed and has a paid payment.

Invoice `breakdown.total_booking_amount` is the charged gross. The invoice also
returns `divisor_used`, `recomputed_total_amount`, and `rounding_adjustment`.
Render the returned adjustment if non-zero; never invent one client-side.

### 4.5 RFID

The RFID route-discovery option has the same live GST fields as normal route
discovery. An in-progress candidate exposes `selected_*` tax values and a gross
`required_hold_amount`. A settled RFID ride exposes immutable snapshot fields.
An older/open ride may legitimately contain zero snapshot values; do not fetch
the current GST rate and retrofit it.

## 5. Traveller forms and identity rules

### 5.1 Where validation applies

The same rules apply to:

- `POST /passenger/traveller-profiles`;
- `PATCH /passenger/traveller-profiles/{profile_id}` when contact fields are
  included;
- inline guest `traveller` objects in `POST /passenger/booking-sessions`.

The backend always validates again. Client validation is for immediate UX only.

### 5.2 Phone validation and normalization

Accepted Indian examples:

```text
9876543210
09876543210
91 98765 43210
+91 98765-43210
```

Indian numbers must have ten national digits and start with 6, 7, 8, or 9.
They are returned/stored as `+919876543210`.

International numbers are accepted only with a leading `+` and must contain
8–15 digits after separators are removed. Accepted separators are spaces,
parentheses, periods, and hyphens. Letters and other punctuation are invalid.

Input `phone` is declared 5–20 characters before normalization. Keep the UI
maximum at 20 to match the API exactly.

### 5.3 Email validation

Email is optional and has a 255-character maximum. Blank optional text is
cleaned to null. Syntax is validated without a deliverability/DNS lookup and
the normalized address is returned. Do not claim that validation proves the
mailbox exists.

### 5.4 Saved traveller versus guest versus self

Each session seat must use exactly one mode:

```ts
{ seat_number: 1 }                                  // account owner/self
{ seat_number: 2, traveller_profile_id: "uuid" }   // saved traveller
{ seat_number: 3, traveller: { ... } }              // one-off guest
```

Never send both `traveller_profile_id` and `traveller`. An explicit traveller
using the account owner's email is rejected: book self by omitting both.

If a guest phone matches any saved traveller—including an inactive one—the
backend returns `guest_matches_saved_traveller`. Select/reactivate the returned
profile rather than silently resubmitting the guest.

Profile edits do not rewrite booking snapshots. A ticket must keep the name
and contact details captured when it was booked.

### 5.5 Conflict errors

Treat traveller conflicts as field/seat errors, not generic payment failures:

- `duplicate_traveller_in_booking_session`: one identity appears on multiple
  selected seats. Highlight all `seat_number_groups`.
- `traveller_booking_conflict`: the traveller already has an unsafe journey.
  Display the message and identify `seat_number`; preserve the selected seats
  so the user can correct the traveller.
- conflict types are `overlapping_route_segment`, `overlapping_trip_window`,
  and `insufficient_transfer_time`.

Transfer safety currently uses a 15-minute backend buffer. Do not duplicate
that calculation in the frontend; schedules and policy can change.

## 6. Recommended booking and payment implementation

### 6.1 Create the multi-seat session

1. Search route/trip options and show gross live fare/GST.
2. Subscribe to the chosen leg on the seat-map socket.
3. Load active traveller profiles.
4. Ask for one traveller assignment per selected seat.
5. Submit `POST /passenger/booking-sessions` once.
6. Store the entire returned `booking_session`, especially its ID, fixed
   `payment_hold_expires_at`, seat IDs, and tax snapshots.
7. Open Razorpay using the returned `payment_order`; use
   `amount_subunits` and `razorpay_order_id` exactly as supplied.

Do not create a second session merely because checkout was dismissed. The
first session still holds its seats until its fixed expiry.

### 6.2 Razorpay success callback

Send the three callback values to:

```text
POST /passenger/booking-sessions/{id}/verify-payment
```

Do not mark the ticket confirmed from the client callback alone. Wait for the
backend response, because it verifies the signature, fetches the payment,
checks order/amount, and captures an authorized payment if necessary.

On success, replace the cached session with the response and route to the
confirmation screen. Invalidate booking/session/current/transaction queries.

### 6.3 Dismissal, network failure, or failed checkout

A dismissed modal does not prove the provider payment failed. A client timeout
does not prove verification failed. Always recover through:

```text
POST /passenger/booking-sessions/{id}/retry-payment
Authorization: Bearer <token>
body: none
```

The endpoint first reconciles the stored order with Razorpay, then returns one
of these actionable outcomes:

| Result | Frontend action |
| --- | --- |
| HTTP 200, `payment_order` object | Replace cached session; reopen Razorpay with this exact existing order. |
| HTTP 200, `payment_order: null`, session `confirmed` | Payment had already succeeded; show confirmation and do not open Razorpay. |
| HTTP 409 `payment_processing` | Disable duplicate attempts, show “confirming payment”, then refetch/poll session status. |
| HTTP 409 `payment_hold_expired` | Stop retrying, clear checkout UI, show seats released; offer a fresh search/session. |
| HTTP 409 `booking_session_not_retryable` | Refetch; its status is cancelled/expired or otherwise closed. |

The returned retry order has the same Razorpay order ID. The retry does not
create a new order, extend the hold, change fare, or recalculate GST.

Recommended button guard:

```ts
function canRetryPayment(session: BookingSession): boolean {
  if (session.status !== "pending_payment") return false;
  if (!session.payment_hold_expires_at) return false;
  return Date.parse(session.payment_hold_expires_at) > Date.now();
}
```

The browser clock is only a UX guard. A request racing expiry may still receive
`payment_hold_expired`; handle it normally.

### 6.4 Safe retry pseudocode

```ts
async function resumePayment(sessionId: string) {
  setPaymentUi("checking");

  try {
    const result = await api.post<BookingSessionRetryPaymentResponse>(
      `/passenger/booking-sessions/${sessionId}/retry-payment`,
    );

    cacheSession(result.booking_session);

    if (!result.payment_order) {
      setPaymentUi("confirmed");
      invalidatePassengerBookingQueries();
      return;
    }

    setPaymentUi("checkout_open");
    openRazorpay(result.payment_order, {
      onSuccess: values => verifySessionPayment(sessionId, values),
      onDismiss: () => setPaymentUi("retry_available"),
    });
  } catch (error) {
    const code = getDomainErrorCode(error);
    if (code === "payment_processing") {
      setPaymentUi("checking");
      startBoundedSessionPolling(sessionId);
      return;
    }
    if (code === "payment_hold_expired" ||
        code === "booking_session_not_retryable") {
      await refetchSession(sessionId);
      setPaymentUi("closed");
      return;
    }
    setPaymentUi("retry_available");
    throw error;
  }
}
```

Prevent double clicks with an in-flight mutex. Do not call create-session and
retry in parallel.

### 6.5 Webhooks and background reconciliation

The frontend never calls `/passenger/payments/razorpay/webhook`. Razorpay calls
it server-to-server when configured.

Checkout remains functional without the webhook:

- normal success is confirmed by the frontend verify call;
- retry reconciles before reopening checkout;
- the scheduled payment reconciler scans pending sessions;
- it also looks back over recently closed sessions (24 hours by default) for a
  late capture and queues refunds;
- the existing refund job issues/retries the actual Razorpay refund.

Therefore the frontend must render backend status, not assume that missing its
callback leaves payment permanently unknown.

## 7. Session, seat, payment, cancellation, and refund states

### 7.1 Session status

| Status | UI meaning |
| --- | --- |
| `pending_payment` | Seats held temporarily; checkout may be verified/retried before expiry. |
| `confirmed` | Session payment succeeded; active seats are booked. |
| `cancelled` | User/backend closed the session; no payment retry. |
| `expired` | Fixed payment hold elapsed; seats released; no payment retry. |

### 7.2 Seat booking status

Possible values are `pending_payment`, `booked`, `boarded`, `completed`,
`cancelled`, and `missed`. In a confirmed multi-seat session, individual seats
can later differ—for example one cancelled/refunding while another remains
booked.

### 7.3 Payment status

Raw `status` values are `created`, `paid`, `failed`, `refunded`. Prefer
`effective_status` for user-facing state because it adds `refund_pending` when
the stored payment is paid but refund work has been requested.

### 7.4 Whole-session and per-seat cancellation

- Pending session: cancel the whole session. Individual pending-seat
  cancellation is rejected.
- Confirmed session: whole-session cancellation requests refunds for eligible
  seats.
- Confirmed session: cancel one seat with
  `/booking-sessions/{session_id}/bookings/{booking_id}/cancel`.
- Cancelled/expired session cancellation calls are idempotent and return the
  current closed session.

The standard cancellation window is enforced by the backend. Never promise a
refund before the response says it was requested.

### 7.5 Refund rendering

Each session seat can have `refund: null` or a refund request with status:
`pending`, `processing`, `succeeded`, `failed`, or `skipped`.

Recommended copy:

- `pending` / `processing`: “Refund in progress”;
- `succeeded`: “Refund issued” plus provider refund ID if useful;
- `failed`: “Refund retry pending” unless product support explicitly wants the
  internal `failure_reason` shown;
- `skipped`: “No refund required”.

Background retry fields (`attempt_count`, `retry_after`) are operational. They
can support support/debug UI but are not a countdown guarantee.

## 8. Cache invalidation and realtime behavior

Keep all three passenger sockets:

- `/passenger/ws/refresh`: cache-invalidation hints;
- `/notifications/ws`: persisted human-readable notifications;
- `/passenger/seatmap/ws`: authoritative live seat snapshots for a selected
  trip leg.

After any create/verify/retry/cancel mutation, immediately cache the returned
object and invalidate:

- booking sessions list/current/detail;
- bookings list/upcoming/current/history and affected details;
- transactions;
- selected trip availability and route discovery.

The refresh socket will also emit `booking.changed` and
`trip.seat_availability_changed`. Treat `reason` as extensible. Current retry
adds `booking_session_payment_retried`; webhook events use
`booking_session_payment_webhook:<outcome>`. Cache correctness must depend on
`resources` and IDs, not a closed list of reason strings.

Full socket transport, heartbeat, reconnect, and invalidation mapping remains
in [PASSENGER_API_REFRESH_WEBSOCKET.md](./PASSENGER_API_REFRESH_WEBSOCKET.md).

## 9. Implementation checklist

### API/types

- [ ] Accept `string | number` for every decimal wire field.
- [ ] Add all live and snapshot GST fields from the wire-contract document.
- [ ] Add `payment_order: PaymentOrder | null` to retry response.
- [ ] Add session payment `effective_status` and refund fields.
- [ ] Add nullable per-seat `refund`.
- [ ] Support domain-object and FastAPI-array error bodies.

### Search and checkout

- [ ] Display gross backend fare; never add GST again.
- [ ] Label inclusive/exclusive using the returned boolean.
- [ ] Replace live data with immutable snapshots once a session is created.
- [ ] Persist session ID and hold expiry through Razorpay modal lifecycle.
- [ ] Verify server-side after Razorpay success.
- [ ] Retry through the new endpoint after dismiss/timeout/failure.
- [ ] Lock duplicate retry/verify requests.
- [ ] Never create a replacement session automatically.

### Travellers

- [ ] Match 20-character phone and 255-character email limits.
- [ ] Accept/display normalized `+E.164` phone returned by backend.
- [ ] Do not send guest and profile ID together.
- [ ] Render 422 validation next to the exact traveller field.
- [ ] Handle saved-guest match and traveller conflict codes per seat.
- [ ] Keep historical traveller snapshots unchanged in the UI.

### Status/refunds

- [ ] Prefer `effective_status` for payment copy.
- [ ] Render per-seat refund state independently.
- [ ] Poll/refetch only while payment is processing or a mounted screen needs
  fresh state; stop on a terminal session status.
- [ ] Do not expose the Razorpay webhook route from the client.

## 10. Acceptance scenarios

1. AC inclusive GST: search, preview, session, payment, history, and invoice all
   show the same gross and component snapshots.
2. Non-AC under AC-only settings: tax components are zero and gross is not
   altered.
3. GST settings change after payment: historical booking and invoice remain
   unchanged while a new search uses new settings.
4. Indian phone in each accepted format is returned normalized to `+91...`.
5. Invalid phone and malformed email produce field-level 422 without creating
   a profile/session/order.
6. Guest phone matching an inactive profile directs the user to reactivate it.
7. Duplicate/overlapping traveller errors preserve seat selections and point
   to affected seat(s).
8. Checkout dismissed before payment: retry returns the same order and hold.
9. Payment captured but client callback lost: retry returns confirmed with no
   order, or processing followed by confirmed after refetch.
10. Retry after hold expiry: UI stops retrying and offers a fresh booking.
11. Late capture after expiry with no webhook: reconciler queues refund and the
    seat refund state eventually becomes succeeded.
12. One seat in a confirmed session is cancelled: only that seat shows refund
    progress; other seats remain booked.
