# GST frontend integration: complete before/after API contract

This is the standalone frontend handoff for backend commit
`bd7ec36` (`feat: add admin-configurable GST processing`). It covers every
frontend-visible API, payload, response-field, calculation, persistence, payout,
RFID, cache, and rollout change introduced by that commit.

The intended reader should not need to inspect the backend source to integrate
GST correctly.

Passenger-application developers should use the consolidated passenger-only
handoff in
[PASSENGER_FE_LATER_COMMITS_INTEGRATION.md](./PASSENGER_FE_LATER_COMMITS_INTEGRATION.md)
and its exact
[wire contracts](./PASSENGER_FE_WIRE_CONTRACTS.md). Those documents also cover
the traveller-validation and payment-retry commits that landed after GST.

## 1. Executive summary

The commit adds centrally configured GST with these defaults:

| Setting | Default |
| --- | ---: |
| GST enabled | `true` |
| CGST | `2.50%` |
| SGST | `2.50%` |
| IGST | `0.00%` |
| Apply only to AC routes | `true` |
| Fare is GST-inclusive | `true` |

When the database has no `platform_settings` row with `settings_key=default`,
the backend reads those values from these environment variables:

```dotenv
GST_ENABLED=true
GST_CGST_RATE_PERCENT=2.50
GST_SGST_RATE_PERCENT=2.50
GST_IGST_RATE_PERCENT=0.00
GST_APPLY_ON_AC_ROUTES_ONLY=true
GST_INCLUSIVE_PRICING=true
```

If an environment variable is missing or blank, the default in the table above
is used. Once the database settings row exists, its stored values are
authoritative and environment changes do not override it.

The practical frontend changes are:

1. Admin gets new `GET` and `PATCH` GST settings APIs.
2. Fare preview and route discovery now return a complete live GST breakdown.
3. Booking, session, payment, transaction, invoice, and RFID responses now
   return persisted tax snapshots.
4. `fare_amount`, `amount`, and payment-order amounts are the final gross amount
   payable by the passenger.
5. Driver commission and payout are calculated from `taxable_amount`, not from
   the GST-inclusive gross fare.
6. Existing request bodies remain unchanged except for the new admin GST
   settings request.
7. Existing records retain their stored tax snapshot even when an admin changes
   GST settings later.

## 2. Terminology and money rules

### 2.1 Canonical meanings

| Field | Meaning | UI usage |
| --- | --- | --- |
| `configured_fare_amount` | Fare stored against the route/stop pair before the current GST mode is applied. | Admin/debug context; do not charge this blindly. |
| `fare_amount` | Final gross passenger fare after GST policy is applied. | Main price displayed and charged. |
| `amount` | Context-specific gross amount. In fare preview and payment records it is the final amount charged. | Payment/display total. |
| `taxable_amount` | Fare value on which GST and commission are calculated. | Invoice/tax breakdown. |
| `cgst_rate_percent` | Current CGST percentage used for a live preview. | Preview label. |
| `*_rate_percent_snapshot` | Rate permanently captured when a booking/ride/session was created or settled. | Historical detail/invoice label. |
| `cgst_amount`, `sgst_amount`, `igst_amount` | Tax component amounts. | Invoice/tax breakdown. |
| `total_tax_amount` | `cgst_amount + sgst_amount + igst_amount`. | Total tax row. |
| `gst_enabled` | Global GST switch at preview time. | Diagnostic/configuration state. |
| `gst_applicable` | Whether GST actually applies to this fare after the route/rate rules. | Decide whether to render a GST breakdown. |
| `gst_enabled_snapshot` | Whether GST applied when this persisted record was priced. | Historical display condition. |
| `gst_inclusive` | Whether the configured fare already contains GST. | Preview explanation. |
| `gst_inclusive_snapshot` | Inclusive/exclusive mode captured on a persisted record. | Historical explanation. |

### 2.2 Decimal transport

Money and percentage fields are backend decimals. Depending on whether an
endpoint has a Pydantic response model, JSON can contain a decimal string such
as `"2.50"` or a JSON number such as `2.5`.

Frontend types should therefore accept both at the API boundary:

```ts
type ApiDecimal = string | number;

const decimalText = (value: ApiDecimal): string => String(value);
```

Do not calculate money with binary JavaScript floating-point values. Use the
backend values for display and payment. If client-side arithmetic is needed for
presentation, use an integer-paise or decimal library.

### 2.3 Rounding

- Money is rounded to two decimal places.
- Percentages are rounded to two decimal places.
- Rounding mode is half-up.
- A booking session calculates one seat's breakdown first and multiplies the
  rounded per-seat values by the seat count.
- The backend is authoritative. The frontend must not reject a backend total
  because a locally recomputed value differs by a paise.

## 3. Pricing mechanism

Let:

```text
configured = configured route fare
rate = CGST + SGST + IGST
divisor = 1 + rate / 100
```

### 3.1 GST not applicable

GST is not applicable when any of these is true:

- `gst_enabled` is `false`;
- all configured tax rates total `0`;
- `gst_apply_on_ac_routes_only` is `true` and the route is not AC.

Then:

```text
fare_amount = configured
taxable_amount = configured
all rates exposed for that fare = 0
all tax amounts = 0
gst_applicable = false
```

`gst_enabled` may still be `true` while `gst_applicable` is `false`, for
example on a non-AC route under the default AC-only policy.

### 3.2 Inclusive pricing

When `gst_inclusive_pricing` is `true`:

```text
fare_amount = configured
taxable_amount = round(configured / divisor, 2)
each tax = round(taxable_amount * component_rate / 100, 2)
```

Example with configured fare `105.00`, CGST `2.50%`, SGST `2.50%`:

```json
{
  "configured_fare_amount": "105.00",
  "fare_amount": "105.00",
  "taxable_amount": "100.00",
  "cgst_amount": "2.50",
  "sgst_amount": "2.50",
  "igst_amount": "0.00",
  "total_tax_amount": "5.00",
  "gst_inclusive": true
}
```

### 3.3 Exclusive pricing

When `gst_inclusive_pricing` is `false`:

```text
taxable_amount = configured
fare_amount = round(configured + configured * rate / 100, 2)
each tax = round(taxable_amount * component_rate / 100, 2)
```

Example with configured fare `100.00`, CGST `2.50%`, SGST `2.50%`:

```json
{
  "configured_fare_amount": "100.00",
  "fare_amount": "105.00",
  "taxable_amount": "100.00",
  "cgst_amount": "2.50",
  "sgst_amount": "2.50",
  "igst_amount": "0.00",
  "total_tax_amount": "5.00",
  "gst_inclusive": false
}
```

The payment order is created for `fare_amount`, not
`configured_fare_amount`.

## 4. Endpoint impact matrix

All paths below are relative to the API host.

| Area | Endpoint | Request change | Response/behavior change |
| --- | --- | --- | --- |
| Admin GST | `GET /admin/gst/settings` | New endpoint | Returns current GST configuration. |
| Admin GST | `PATCH /admin/gst/settings` | New partial-update body | Updates and returns GST configuration. |
| Fare preview | `POST /passenger/fare/preview` | None | Adds live GST breakdown; `amount` is gross. |
| Passenger discovery | `GET /passenger/route-trip-options` | None | Each route option adds live GST breakdown. |
| RFID discovery | `GET /passenger/rfid/route-trip-options` | None | Adds route-option and in-progress-trip tax fields; hold uses gross fares. |
| Booking create/mutate | `POST /passenger/bookings` and booking verify/cancel APIs | None | Nested booking/payment includes tax snapshots; payment gross may change under exclusive mode. |
| Booking lists/detail | Passenger booking list, upcoming, current, history, and detail APIs | None | Booking items include tax snapshots. |
| Booking invoice | `GET /passenger/bookings/{booking_id}/invoice` | None | Adds IGST and inclusive flag; uses stored snapshot instead of hard-coded current assumptions. |
| Booking sessions | Passenger session create/verify/cancel/list/current/detail/seat-cancel APIs | None | Session, seat, and payment objects include tax totals/snapshots. |
| Transactions | `GET /passenger/transactions` | None | Each payment adds tax amounts. |
| Passenger RFID | Summary, ride list, and ride detail APIs | None | Ride objects add tax snapshots. |
| Admin payouts | `GET /admin/payouts/bookings` and detail | None | Booking adds tax snapshots; commission/payout basis changes to taxable fare. |
| Admin refunds | `GET /admin/payouts/refunds` | None | Queue items add booking tax snapshots. |
| Admin RFID | `GET /admin/rfid/rides/{rfid_ride_id}/money-detail` | None | Nested ride adds tax snapshots. |
| Admin RFID reversal | `POST /admin/rfid/rides/{rfid_ride_id}/reverse-deduction` and payout reversal flow | None | Driver/platform reversal components use stored proportional snapshots; one old snapshot-limit rejection is removed. |

## 5. Admin GST settings

All `/admin/*` routes require an active admin access token through the existing
admin authentication mechanism.

### 5.1 Before

There was no GST configuration API. Passenger invoices assumed `2.50%` CGST +
`2.50%` SGST for AC trips and zero tax for non-AC trips.

### 5.2 Get settings

```http
GET /admin/gst/settings
Authorization: Bearer <admin-access-token>
```

No request body or query parameters.

Response:

```json
{
  "settings_key": "default",
  "gst_enabled": true,
  "gst_cgst_rate_percent": 2.5,
  "gst_sgst_rate_percent": 2.5,
  "gst_igst_rate_percent": 0,
  "gst_apply_on_ac_routes_only": true,
  "gst_inclusive_pricing": true,
  "created_at": "2026-07-09T05:40:00+00:00",
  "updated_at": "2026-07-09T05:40:00+00:00"
}
```

If the default settings row does not yet exist, the endpoint still returns the
environment-backed defaults, with `created_at` and `updated_at` equal to
`null`.

### 5.3 Patch settings

```http
PATCH /admin/gst/settings
Authorization: Bearer <admin-access-token>
Content-Type: application/json
```

Every property is optional. Omitted properties remain unchanged.

```ts
interface GSTSettingsPatch {
  gst_enabled?: boolean;
  gst_cgst_rate_percent?: ApiDecimal; // 0 through 100
  gst_sgst_rate_percent?: ApiDecimal; // 0 through 100
  gst_igst_rate_percent?: ApiDecimal; // 0 through 100
  gst_apply_on_ac_routes_only?: boolean;
  gst_inclusive_pricing?: boolean;
}
```

Example request:

```json
{
  "gst_enabled": true,
  "gst_cgst_rate_percent": "2.50",
  "gst_sgst_rate_percent": "2.50",
  "gst_igst_rate_percent": "0.00",
  "gst_apply_on_ac_routes_only": true,
  "gst_inclusive_pricing": true
}
```

Success response:

```json
{
  "message": "GST settings updated successfully.",
  "settings": {
    "settings_key": "default",
    "gst_enabled": true,
    "gst_cgst_rate_percent": 2.5,
    "gst_sgst_rate_percent": 2.5,
    "gst_igst_rate_percent": 0,
    "gst_apply_on_ac_routes_only": true,
    "gst_inclusive_pricing": true,
    "created_at": "2026-07-09T05:40:00+00:00",
    "updated_at": "2026-07-09T06:00:00+00:00"
  }
}
```

Rates below `0` or above `100` return FastAPI validation status `422`.

### 5.4 Admin UI behavior

- Use a partial patch for Save; sending the complete form is also valid.
- Use the returned `settings` object as the immediate source of truth in the
  initiating tab.
- Invalidate/refetch live fare previews and route-discovery queries after Save.
- Warn that a settings change affects new previews, bookings, and RFID
  settlements; it does not rewrite historical booking snapshots.
- Do not offer client-only GST overrides.
- Treat `created_at: null` as an environment-backed configuration that has not
  yet been persisted. The first settings-row creation seeds all GST fields from
  the environment, so a partial PATCH preserves the environment baseline for
  omitted fields.

A successful patch publishes the existing `admin.settings_changed` refresh
event to admin sockets with `gst_settings` among its resources. Other admin tabs
must invalidate `GET /admin/gst/settings` when that resource arrives. The
initiating tab should still use the HTTP response immediately rather than wait
for its own WebSocket event.

The event is admin-only. Open passenger tabs are not sent a GST-specific event;
they see new pricing on their next normal discovery/preview refetch or socket
reconnect.

## 6. Live fare preview

### 6.1 Request: unchanged

```http
POST /passenger/fare/preview
Content-Type: application/json
```

```json
{
  "route_id": "route-uuid",
  "pickup_stop_id": "pickup-stop-uuid",
  "dropoff_stop_id": "dropoff-stop-uuid"
}
```

| Parameter | Required | Validation |
| --- | --- | --- |
| `route_id` | Yes | String, 1–36 characters. |
| `pickup_stop_id` | Yes | String, 1–36 characters. |
| `dropoff_stop_id` | Yes | String, 1–36 characters. |

### 6.2 Before response

```json
{
  "route_id": "route-uuid",
  "route_name": "Airport Express",
  "route_code": "AEX",
  "has_ac": true,
  "pickup_stop": { "id": "stop-a", "name": "A" },
  "dropoff_stop": { "id": "stop-b", "name": "B" },
  "pickup_sequence_no": 1,
  "dropoff_sequence_no": 4,
  "amount": "105.00"
}
```

### 6.3 After response

Existing fields remain. These GST fields are additive:

```json
{
  "route_id": "route-uuid",
  "route_name": "Airport Express",
  "route_code": "AEX",
  "has_ac": true,
  "pickup_stop": { "id": "stop-a", "name": "A" },
  "dropoff_stop": { "id": "stop-b", "name": "B" },
  "pickup_sequence_no": 1,
  "dropoff_sequence_no": 4,
  "amount": "105.00",
  "configured_fare_amount": "105.00",
  "taxable_amount": "100.00",
  "cgst_rate_percent": "2.50",
  "cgst_amount": "2.50",
  "sgst_rate_percent": "2.50",
  "sgst_amount": "2.50",
  "igst_rate_percent": "0.00",
  "igst_amount": "0.00",
  "total_tax_amount": "5.00",
  "gst_enabled": true,
  "gst_applicable": true,
  "gst_inclusive": true
}
```

`amount` remains the simplest backward-compatible final-price field. New code
should treat it as equivalent to the gross fare and use the added fields for
the breakdown.

## 7. Route and trip discovery

### 7.1 Standard discovery

```http
GET /passenger/route-trip-options
  ?from_stop_id=<uuid>
  &to_stop_id=<uuid>
  &from_time=<optional-ISO-8601>
  &to_time=<optional-ISO-8601>
```

Request parameters are unchanged.

Before, each `items[]` route option contained:

```ts
interface OldRouteOption {
  route: Route;
  pickup_stop: StopBrief;
  dropoff_stop: StopBrief;
  pickup_sequence_no: number;
  dropoff_sequence_no: number;
  fare_amount: ApiDecimal;
  upcoming_scheduled_trips: ScheduledTripOption[];
  upcoming_scheduled_trip_count: number;
}
```

After, each option additionally contains:

```ts
interface LiveGSTBreakdown {
  fare_amount: ApiDecimal;             // final gross fare
  configured_fare_amount: ApiDecimal;  // route fare before GST mode
  taxable_amount: ApiDecimal;
  cgst_rate_percent: ApiDecimal;
  cgst_amount: ApiDecimal;
  sgst_rate_percent: ApiDecimal;
  sgst_amount: ApiDecimal;
  igst_rate_percent: ApiDecimal;
  igst_amount: ApiDecimal;
  total_tax_amount: ApiDecimal;
  gst_enabled: boolean;
  gst_applicable: boolean;
  gst_inclusive: boolean;
}
```

The top-level response remains:

```ts
interface RouteTripDiscoveryResponse {
  from_stop_id: string;
  to_stop_id: string;
  from_time: string | null;
  to_time: string | null;
  items: Array<OldRouteOption & LiveGSTBreakdown>;
  count: number;
}
```

### 7.2 Frontend discovery behavior

- Display `fare_amount` as the passenger-facing route option price.
- Optionally show “Includes GST” when `gst_applicable && gst_inclusive`.
- Optionally show “+ GST” when `gst_applicable && !gst_inclusive`; note that
  `fare_amount` already includes the added GST and is still the final total.
- Hide component rows when `gst_applicable` is false, or show a zero-tax row if
  the product requires it.
- Refresh discovery after admin GST settings change; these are live values, not
  snapshots.

## 8. Reusable persisted tax response fragments

The same tax fields appear in many endpoints. Implement these shared frontend
types once.

### 8.1 Booking or RFID ride tax snapshot

Before, a booking/ride exposed `fare_amount` only. After it also exposes:

```ts
interface TaxSnapshot {
  taxable_amount: ApiDecimal;
  cgst_rate_percent_snapshot: ApiDecimal;
  cgst_amount: ApiDecimal;
  sgst_rate_percent_snapshot: ApiDecimal;
  sgst_amount: ApiDecimal;
  igst_rate_percent_snapshot: ApiDecimal;
  igst_amount: ApiDecimal;
  total_tax_amount: ApiDecimal;
  gst_enabled_snapshot: boolean;
  gst_inclusive_snapshot: boolean;
}
```

Example:

```json
{
  "fare_amount": "105.00",
  "taxable_amount": "100.00",
  "cgst_rate_percent_snapshot": "2.50",
  "cgst_amount": "2.50",
  "sgst_rate_percent_snapshot": "2.50",
  "sgst_amount": "2.50",
  "igst_rate_percent_snapshot": "0.00",
  "igst_amount": "0.00",
  "total_tax_amount": "5.00",
  "gst_enabled_snapshot": true,
  "gst_inclusive_snapshot": true
}
```

The snapshot fields must be displayed as returned. Never combine them with the
current admin settings.

### 8.2 Payment tax snapshot

Before, a payment exposed `amount`. After it also exposes:

```ts
interface PaymentTaxSnapshot {
  taxable_amount: ApiDecimal;
  cgst_amount: ApiDecimal;
  sgst_amount: ApiDecimal;
  igst_amount: ApiDecimal;
  total_tax_amount: ApiDecimal;
}
```

Payment objects do not expose component rates. Use the containing booking or
session rate snapshot if rate labels are needed.

### 8.3 Booking-session tax snapshot

Before, a session exposed `total_fare_amount`. After it also exposes:

```ts
interface BookingSessionTaxSnapshot {
  total_taxable_amount: ApiDecimal;
  total_cgst_amount: ApiDecimal;
  total_sgst_amount: ApiDecimal;
  total_igst_amount: ApiDecimal;
  total_tax_amount: ApiDecimal;
  gst_enabled_snapshot: boolean;
  gst_inclusive_snapshot: boolean;
  cgst_rate_percent_snapshot: ApiDecimal;
  sgst_rate_percent_snapshot: ApiDecimal;
  igst_rate_percent_snapshot: ApiDecimal;
}
```

## 9. Passenger bookings

### 9.1 Requests: unchanged

Single-seat booking:

```http
POST /passenger/bookings
```

```json
{
  "scheduled_trip_id": "trip-uuid",
  "pickup_stop_id": "stop-a",
  "dropoff_stop_id": "stop-b",
  "seat_number": 7
}
```

Payment verification remains:

```json
{
  "razorpay_order_id": "order_...",
  "razorpay_payment_id": "pay_...",
  "razorpay_signature": "..."
}
```

No GST field is accepted from the passenger. The backend reads current admin
settings, calculates GST, snapshots it, and creates the Razorpay order for the
gross amount.

### 9.2 Affected single-booking endpoints

The nested/returned booking object adds `TaxSnapshot`, and every nested payment
adds `PaymentTaxSnapshot`, on:

- `POST /passenger/bookings`
- `POST /passenger/bookings/{booking_id}/verify-payment`
- `POST /passenger/bookings/{booking_id}/cancel`
- `GET /passenger/bookings`
- `GET /passenger/bookings/upcoming`
- `GET /passenger/bookings/current`
- `GET /passenger/history`
- `GET /passenger/bookings/{booking_id}`

The normal wrappers remain unchanged:

```ts
interface BookingCreateResponse {
  message: string;
  booking: ExistingBooking & TaxSnapshot & {
    payments: Array<ExistingPayment & PaymentTaxSnapshot>;
  };
  payment_order: Record<string, unknown> | null;
}

interface BookingMutationResponse {
  message: string;
  booking: ExistingBooking & TaxSnapshot;
}

interface BookingListResponse {
  items: Array<ExistingBooking & TaxSnapshot>;
  count: number;
}
```

The current-booking list returns the same `TaxSnapshot` additions on each item.

### 9.3 Frontend behavior

- Charge and display `booking.fare_amount` / `payment.amount`.
- Pass the backend-created Razorpay order through unchanged.
- Use booking snapshot fields on history, cancellation, detail, and receipt
  screens.
- Do not call fare preview and then assume its result is permanently locked.
  Booking creation is the authoritative pricing point.
- If settings change between preview and booking creation, the create response
  and payment order win. The UI should replace the previewed amount with the
  created booking amount.

## 10. Booking sessions

### 10.1 Request: unchanged

```http
POST /passenger/booking-sessions
```

```json
{
  "scheduled_trip_id": "trip-uuid",
  "pickup_stop_id": "stop-a",
  "dropoff_stop_id": "stop-b",
  "seats": [
    { "seat_number": 4 },
    { "seat_number": 5, "traveller_profile_id": "profile-uuid" },
    {
      "seat_number": 6,
      "traveller": {
        "full_name": "Guest Name",
        "phone": "+91 98765 43210",
        "email": "guest@example.com",
        "relationship_label": "Colleague"
      }
    }
  ]
}
```

GST fields are not accepted in the request.

### 10.2 Affected session endpoints

These endpoints now return the session `BookingSessionTaxSnapshot`; each
`bookings[]` seat adds `TaxSnapshot`; each `payments[]` item adds
`PaymentTaxSnapshot`:

- `POST /passenger/booking-sessions`
- `POST /passenger/booking-sessions/{booking_session_id}/verify-payment`
- `POST /passenger/booking-sessions/{booking_session_id}/cancel`
- `POST /passenger/booking-sessions/{booking_session_id}/bookings/{booking_id}/cancel`
- `GET /passenger/booking-sessions`
- `GET /passenger/booking-sessions/{booking_session_id}`
- `GET /passenger/booking-sessions/current`

Create wrapper:

```ts
interface BookingSessionCreateResponse {
  message: string;
  booking_session: ExistingBookingSession & BookingSessionTaxSnapshot & {
    bookings: Array<ExistingSessionSeat & TaxSnapshot>;
    payments: Array<ExistingSessionPayment & PaymentTaxSnapshot>;
  };
  payment_order: Record<string, unknown>;
}
```

For `/booking-sessions/current`, each session item uses
`booking_session_id` rather than `id`, as before, and now includes the same
session tax totals and rates.

### 10.3 Session amount rules

- `total_fare_amount` is the total gross passenger charge.
- `total_taxable_amount` is the total pre-tax basis.
- Component totals are the sum/multiplication of per-seat snapshots.
- Every seat on one session uses the same route fare and rates.
- The Razorpay session order uses `total_fare_amount`.
- Cancelling one seat does not cause the original tax snapshots to be
  recomputed under current settings.

## 11. Passenger transaction history

```http
GET /passenger/transactions
  ?status=<optional-payment-status>
  &month=<1-12>
  &year=<2000-2100>
  &limit=<1-200>
  &offset=<0-or-more>
```

Request/query behavior is unchanged.

Before, each item contained `amount` but no tax amounts. After, each item adds:

```json
{
  "amount": "105.00",
  "taxable_amount": "100.00",
  "cgst_amount": "2.50",
  "sgst_amount": "2.50",
  "igst_amount": "0.00",
  "total_tax_amount": "5.00"
}
```

All existing transaction identity, status, route, stop, Razorpay, and timestamp
fields remain unchanged.

## 12. Passenger invoice

```http
GET /passenger/bookings/{booking_id}/invoice
Authorization: Bearer <passenger-access-token>
```

### 12.1 Before

The invoice breakdown exposed CGST and SGST only. It derived `2.50% + 2.50%`
from whether the trip was AC, rather than reading the booking's immutable tax
snapshot.

### 12.2 After

The existing breakdown remains and adds:

```ts
interface PassengerInvoiceBreakdown {
  total_booking_amount: ApiDecimal; // gross paid amount
  divisor_used: ApiDecimal;
  taxable_value: ApiDecimal;
  cgst_rate_percent: ApiDecimal;
  cgst_amount: ApiDecimal;
  sgst_rate_percent: ApiDecimal;
  sgst_amount: ApiDecimal;
  igst_rate_percent: ApiDecimal; // new
  igst_amount: ApiDecimal;        // new
  total_tax_amount: ApiDecimal;
  gst_inclusive: boolean;         // new
  recomputed_total_amount: ApiDecimal;
  rounding_adjustment: ApiDecimal;
}
```

The values now come from the booking snapshot. An admin settings change does
not alter an old invoice.

Render recommendation:

```text
Taxable value                 ₹100.00
CGST @ 2.50%                    ₹2.50
SGST @ 2.50%                    ₹2.50
IGST @ 0.00%                    ₹0.00  (hide if zero if desired)
Total tax                       ₹5.00
Total paid                    ₹105.00
```

`rounding_adjustment` explains any difference between component recomputation
and `total_booking_amount`. Do not silently add it to the payment again.

The nested `payment`, when present, also includes `PaymentTaxSnapshot`.

## 13. Passenger RFID discovery

### 13.1 Route option additions

```http
GET /passenger/rfid/route-trip-options
  ?from_stop_id=<uuid>
  &to_stop_id=<uuid>
  &from_time=<optional-ISO-8601>
  &to_time=<optional-ISO-8601>
```

Each `items[]` option gains the same `LiveGSTBreakdown` described in standard
route discovery.

### 13.2 In-progress trip additions

Each `items[].rfid_in_progress_trips[]` previously exposed only
`selected_fare_amount`. It now also exposes:

```ts
interface RFIDSelectedFareTax {
  selected_taxable_amount: ApiDecimal;
  selected_cgst_amount: ApiDecimal;
  selected_sgst_amount: ApiDecimal;
  selected_igst_amount: ApiDecimal;
  selected_total_tax_amount: ApiDecimal;
}
```

Example:

```json
{
  "selected_fare_amount": "105.00",
  "selected_taxable_amount": "100.00",
  "selected_cgst_amount": "2.50",
  "selected_sgst_amount": "2.50",
  "selected_igst_amount": "0.00",
  "selected_total_tax_amount": "5.00",
  "required_hold_amount": "210.00",
  "available_balance": "300.00",
  "balance_shortfall": "0.00"
}
```

`required_hold_amount` is now calculated from the maximum downstream **gross**
fare under current GST settings. Continue using the returned hold and balance
flags. Do not derive hold sufficiency from `selected_fare_amount`.

## 14. Passenger RFID ride responses

The reusable `PassengerRFIDRideResponse` adds `TaxSnapshot`. It is returned by:

- `GET /passenger/rfid/summary` in `current_ride` and `recent_rides[]`;
- `GET /passenger/rfid/rides` in `items[]`;
- `GET /passenger/rfid/rides/{rfid_ride_id}` in `ride`.

Existing fields remain:

```ts
interface PassengerRFIDRideMoney {
  hold_amount: ApiDecimal;
  fare_amount: ApiDecimal;          // gross settled fare
  fare_reversed_amount: ApiDecimal;
  fare_net_amount: ApiDecimal;
}
```

The new snapshot explains the gross fare. RFID board records initially contain
zero tax snapshots because the final drop stop/fare is not yet known. On
successful drop or backend settlement, the backend calculates and persists the
actual breakdown.

The frontend should therefore tolerate a currently boarded ride with:

```json
{
  "fare_amount": "0.00",
  "taxable_amount": "0.00",
  "total_tax_amount": "0.00",
  "gst_enabled_snapshot": false,
  "gst_inclusive_snapshot": true
}
```

## 15. Admin payout booking and refund responses

### 15.1 Payout booking list and detail

Affected endpoints:

- `GET /admin/payouts/bookings`
- `GET /admin/payouts/bookings/{booking_id}` in the nested `booking`

Request filters are unchanged. Each serialized payout booking adds
`TaxSnapshot`.

The important money relationship is now:

```text
commission_amount = taxable_amount * commission_percent_snapshot / 100
driver_payout_amount = taxable_amount - commission_amount
```

Before this commit, commission and driver payout used `fare_amount` as the
basis. With inclusive GST, that incorrectly included tax in distributable
revenue.

Example at gross `105.00`, taxable `100.00`, commission `10%`:

```json
{
  "fare_amount": "105.00",
  "taxable_amount": "100.00",
  "total_tax_amount": "5.00",
  "commission_percent_snapshot": "10.00",
  "commission_amount": "10.00",
  "driver_payout_amount": "90.00"
}
```

Frontend payout screens must not expect:

```text
driver_payout_amount + commission_amount == fare_amount
```

Under GST, the correct relationship is approximately:

```text
driver_payout_amount + commission_amount == taxable_amount
taxable_amount + total_tax_amount (+ rounding adjustment) == fare_amount
```

### 15.2 Refund queue

```http
GET /admin/payouts/refunds
```

Each `items[]` refund queue item adds `TaxSnapshot`. Existing refund status,
retry, booking, driver, passenger, and timestamp fields remain unchanged.

Use `fare_amount` as the original gross booking amount. The tax fields explain
that amount; they do not instruct the frontend to split or initiate separate tax
refunds.

## 16. Admin RFID money detail and reversal behavior

### 16.1 Money detail response

```http
GET /admin/rfid/rides/{rfid_ride_id}/money-detail
```

The nested `ride` adds `TaxSnapshot`. Existing commission, driver payout,
platform, funding allocation, ledger, transfer, and reversal fields remain.

RFID commission is also calculated from the ride's taxable amount, not gross
fare.

### 16.2 Reversal behavior change

Request payloads are unchanged. For example, ride deduction reversal still
uses its existing amount/reason fields.

Before, the service could reject a ride reversal with HTTP `409` and:

```json
{
  "detail": {
    "error": "rfid_reversal_exceeds_remaining_snapshot_amount",
    "message": "Cannot reverse more than the remaining unreversed driver/platform snapshot amount."
  }
}
```

That pre-check was removed because driver payout plus platform commission no
longer equals gross fare after GST is excluded. The backend now calculates
driver and platform reversal components proportionally from their stored ride
snapshots and caps each component by its remaining reversible balance.

Frontend implications:

- Do not pre-validate a reversal using
  `driver_payout_amount + platform_amount` as if it were gross fare.
- Submit the requested gross reversal through the existing API and handle the
  returned result/error.
- Existing response envelopes remain unchanged.
- Tax is explanatory snapshot data; frontend does not allocate reversal money
  among tax/driver/platform components.

## 17. Current versus snapshot values

This distinction is essential.

| API/data | Uses current admin settings? | Can change after refetch? |
| --- | --- | --- |
| Fare preview | Yes | Yes |
| Standard route discovery | Yes | Yes |
| RFID route discovery | Yes | Yes |
| New booking creation | Yes, once at creation | Created snapshot does not change later |
| New session creation | Yes, once at creation | Created snapshot does not change later |
| Existing booking/session detail | No; reads snapshot | No because of later settings changes |
| Invoice | No; reads booking snapshot | No because of later settings changes |
| RFID open-ride final settlement | Uses settings when fare is settled | Snapshot remains after settlement |
| Existing settled RFID ride | No; reads snapshot | No because of later settings changes |

The frontend must never label a historical record using the current result of
`GET /admin/gst/settings`.

## 18. Backward compatibility

### 18.1 Requests

All pre-existing passenger, payment, payout, and RFID request payloads are
backward-compatible and unchanged.

### 18.2 Responses

Changes are additive except for monetary behavior under exclusive pricing:

- Existing clients reading only `amount` or `fare_amount` continue to receive
  the final gross total.
- Strict response validators that reject unknown keys must be updated before
  deployment.
- Exclusive mode can make the final gross amount larger than the configured
  route fare. Clients must not overwrite the backend total with a locally cached
  route fare.

### 18.3 Historical fallback

Serializers contain fallbacks for legacy rows:

- If persisted `taxable_amount` is zero while `fare_amount` is positive,
  responses can fall back to the fare as taxable.
- If persisted `total_tax_amount` is zero, serializers can recompute it from
  stored component amounts.

These fallbacks make old data readable; they are not a reason for the frontend
to invent missing tax values.

### 18.4 Environment fallback precedence

GST configuration precedence is:

```text
persisted default platform_settings row
    > GST_* environment variables
    > built-in defaults
```

Environment booleans accept `1/0`, `true/false`, `yes/no`, and `on/off`
case-insensitively. Rates must be decimal values from `0` through `100`.
Malformed configured values fail explicitly instead of silently changing tax
behavior.

## 19. Cache and real-time integration

Recommended query invalidation after a successful GST settings patch:

```ts
await Promise.all([
  queryClient.invalidateQueries({ queryKey: ["gst_settings"] }),
  queryClient.invalidateQueries({ queryKey: ["passenger", "fare-preview"] }),
  queryClient.invalidateQueries({ queryKey: ["passenger", "routeTripOptions"] }),
  queryClient.invalidateQueries({ queryKey: ["passenger", "rfidRouteTripOptions"] }),
]);
```

Adapt keys to the application's query-key factory.

Do not invalidate historical booking/invoice data merely to rewrite it with the
new settings. Normal booking refresh events can still refetch persisted
snapshots when booking/payment status changes.

On `admin.settings_changed`, invalidate the `gst_settings` resource along with
the other resources listed by the event. The initiating admin mutation response
remains authoritative.

## 20. Suggested shared frontend types

```ts
export type ApiDecimal = string | number;

export interface LiveGSTBreakdown {
  fare_amount: ApiDecimal;
  configured_fare_amount: ApiDecimal;
  taxable_amount: ApiDecimal;
  cgst_rate_percent: ApiDecimal;
  cgst_amount: ApiDecimal;
  sgst_rate_percent: ApiDecimal;
  sgst_amount: ApiDecimal;
  igst_rate_percent: ApiDecimal;
  igst_amount: ApiDecimal;
  total_tax_amount: ApiDecimal;
  gst_enabled: boolean;
  gst_applicable: boolean;
  gst_inclusive: boolean;
}

export interface TaxSnapshot {
  taxable_amount: ApiDecimal;
  cgst_rate_percent_snapshot: ApiDecimal;
  cgst_amount: ApiDecimal;
  sgst_rate_percent_snapshot: ApiDecimal;
  sgst_amount: ApiDecimal;
  igst_rate_percent_snapshot: ApiDecimal;
  igst_amount: ApiDecimal;
  total_tax_amount: ApiDecimal;
  gst_enabled_snapshot: boolean;
  gst_inclusive_snapshot: boolean;
}

export interface PaymentTaxSnapshot {
  taxable_amount: ApiDecimal;
  cgst_amount: ApiDecimal;
  sgst_amount: ApiDecimal;
  igst_amount: ApiDecimal;
  total_tax_amount: ApiDecimal;
}

export interface BookingSessionTaxSnapshot {
  total_taxable_amount: ApiDecimal;
  total_cgst_amount: ApiDecimal;
  total_sgst_amount: ApiDecimal;
  total_igst_amount: ApiDecimal;
  total_tax_amount: ApiDecimal;
  gst_enabled_snapshot: boolean;
  gst_inclusive_snapshot: boolean;
  cgst_rate_percent_snapshot: ApiDecimal;
  sgst_rate_percent_snapshot: ApiDecimal;
  igst_rate_percent_snapshot: ApiDecimal;
}

export interface GSTSettings {
  settings_key: "default" | string;
  gst_enabled: boolean;
  gst_cgst_rate_percent: ApiDecimal;
  gst_sgst_rate_percent: ApiDecimal;
  gst_igst_rate_percent: ApiDecimal;
  gst_apply_on_ac_routes_only: boolean;
  gst_inclusive_pricing: boolean;
  created_at: string | null;
  updated_at: string | null;
}
```

## 21. UI rendering rules

Use these rules consistently across discovery, checkout, booking detail,
invoice, RFID, refunds, and payouts:

1. Main passenger total: `fare_amount`, `amount`, or `total_fare_amount` for
   the relevant object.
2. Taxable row: `taxable_amount` or `total_taxable_amount`.
3. Render each tax component when its amount or rate is non-zero; retaining a
   zero IGST row on formal invoices is also valid.
4. Render “GST included” from `gst_inclusive`/`gst_inclusive_snapshot`, not from
   a guess based on the numbers.
5. For persisted records, use snapshot rates only.
6. For payment checkout, trust the Razorpay order and create response.
7. For payout screens, clearly separate gross passenger fare, tax, taxable
   revenue, commission, and driver payout.

Suggested payout column order:

```text
Gross fare | Tax | Taxable fare | Commission | Driver payout | Transfer state
```

## 22. Rollout dependency

Backend migration `8d7f4c2a9b31_add_gst_settings_and_snapshots.py` must be
applied before deploying application code from the GST commit.

It adds GST settings, tax snapshots, totals, and constraints across:

- `platform_settings`;
- `trip_bookings`;
- `booking_sessions`;
- `booking_payments`;
- `booking_session_payments`;
- `rfid_trip_rides`.

The migration also backfills existing booking/RFID data using the previous
effective policy: `2.50%` CGST + `2.50%` SGST, inclusive, for AC routes; zero
GST for non-AC routes.

Frontend deployment should occur only after the backend reports the new fields.

## 23. Acceptance test matrix

### Admin settings

- Load defaults when no settings row exists.
- Patch one field without overwriting omitted fields.
- Reject a rate below `0` or above `100`.
- Disable GST and confirm preview tax becomes zero.
- Toggle AC-only and verify non-AC preview behavior.
- Toggle inclusive/exclusive and verify gross fare behavior.
- Confirm the initiating tab updates without waiting for WebSocket delivery.

### Passenger preview and checkout

- AC + default inclusive `5%`: configured `105`, taxable `100`, gross `105`.
- AC + exclusive `5%`: configured `100`, taxable `100`, gross `105`.
- Non-AC + AC-only: taxable equals gross, tax zero.
- GST disabled: taxable equals gross, tax zero.
- IGST-only configuration renders IGST correctly.
- Preview followed by settings change uses create-response/payment-order total.

### Bookings and sessions

- Single booking returns booking and payment tax snapshots.
- Multi-seat session totals equal returned seat/payment totals.
- Lists, current trips, details, and mutations all accept added fields.
- Historical booking remains unchanged after settings patch.
- Invoice matches booking snapshot and includes IGST/inclusive fields.

### RFID

- RFID discovery shows gross selected fare and tax components.
- Required hold uses gross downstream fare.
- Boarded/unsettled ride tolerates zero tax snapshot.
- Dropped/settled ride returns final snapshot.
- Admin money detail renders tax separately from commission/payout.
- Reversal UI does not pre-reject based on driver + platform snapshots.

### Payouts and refunds

- Commission is shown against taxable fare.
- Driver payout plus commission reconciles to taxable fare, not gross fare.
- Refund queue accepts and displays the added tax snapshot.

## 24. Frontend completion checklist

- [ ] Add `ApiDecimal`, `LiveGSTBreakdown`, `TaxSnapshot`,
      `PaymentTaxSnapshot`, and `BookingSessionTaxSnapshot` shared types.
- [ ] Add admin GST settings page/form and GET/PATCH API client methods.
- [ ] Invalidate `gst_settings` on `admin.settings_changed` and current-price
      queries after a local settings patch.
- [ ] Update standard and RFID discovery cards to use gross `fare_amount`.
- [ ] Update checkout to trust booking/session create response and payment order.
- [ ] Update all booking/session/payment serializers or runtime validators for
      additive fields.
- [ ] Add GST rows to transaction, booking detail, and invoice screens.
- [ ] Add GST rows to passenger/admin RFID ride detail.
- [ ] Add taxable fare and tax columns to admin payout/refund views.
- [ ] Remove any payout assertion that commission plus driver payout equals
      gross passenger fare.
- [ ] Remove RFID reversal pre-validation based on driver/platform sum.
- [ ] Test inclusive, exclusive, disabled, AC-only, non-AC, and IGST cases.
- [ ] Keep historical display based on snapshots, never current settings.
