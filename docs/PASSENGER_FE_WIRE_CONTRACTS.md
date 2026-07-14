# Passenger FE wire contracts for the later commits

This is the copy-ready passenger API companion to
[PASSENGER_FE_LATER_COMMITS_INTEGRATION.md](./PASSENGER_FE_LATER_COMMITS_INTEGRATION.md).
It lists the request/response deltas introduced by GST, traveller validation,
and safe booking-session payment retry.

Assumptions:

- HTTP base path is `/passenger`.
- Authenticated routes require `Authorization: Bearer <access_token>`.
- Dates are ISO-8601 strings and must be parsed as instants.
- UUID-like IDs are represented as `string`.
- `DecimalWire` is `string | number`; preserve decimal precision.

## 1. Shared TypeScript

```ts
export type DecimalWire = string | number;
export type ISODateTime = string;

export type BookingStatus =
  | "pending_payment" | "booked" | "boarded"
  | "completed" | "cancelled" | "missed";

export type BookingSessionStatus =
  | "pending_payment" | "confirmed" | "cancelled" | "expired";

export type RawPaymentStatus = "created" | "paid" | "failed" | "refunded";
export type EffectivePaymentStatus = RawPaymentStatus | "refund_pending";
export type SeatRefundStatus =
  | "pending" | "processing" | "succeeded" | "failed" | "skipped";

export interface StopBrief {
  id: string;
  name: string;
  lat: DecimalWire;
  lng: DecimalWire;
  radius_meters: number;
  is_active: boolean;
}

export interface LiveTaxBreakdown {
  configured_fare_amount: DecimalWire;
  taxable_amount: DecimalWire;
  cgst_rate_percent: DecimalWire;
  cgst_amount: DecimalWire;
  sgst_rate_percent: DecimalWire;
  sgst_amount: DecimalWire;
  igst_rate_percent: DecimalWire;
  igst_amount: DecimalWire;
  total_tax_amount: DecimalWire;
  gst_enabled: boolean;
  gst_applicable: boolean;
  gst_inclusive: boolean;
}

export interface BookingTaxSnapshot {
  taxable_amount: DecimalWire;
  cgst_rate_percent_snapshot: DecimalWire;
  cgst_amount: DecimalWire;
  sgst_rate_percent_snapshot: DecimalWire;
  sgst_amount: DecimalWire;
  igst_rate_percent_snapshot: DecimalWire;
  igst_amount: DecimalWire;
  total_tax_amount: DecimalWire;
  gst_enabled_snapshot: boolean;
  gst_inclusive_snapshot: boolean;
}

export interface PaymentTaxSnapshot {
  taxable_amount: DecimalWire;
  cgst_amount: DecimalWire;
  sgst_amount: DecimalWire;
  igst_amount: DecimalWire;
  total_tax_amount: DecimalWire;
}

export interface SessionTaxSnapshot {
  total_taxable_amount: DecimalWire;
  total_cgst_amount: DecimalWire;
  total_sgst_amount: DecimalWire;
  total_igst_amount: DecimalWire;
  total_tax_amount: DecimalWire;
  gst_enabled_snapshot: boolean;
  gst_inclusive_snapshot: boolean;
  cgst_rate_percent_snapshot: DecimalWire;
  sgst_rate_percent_snapshot: DecimalWire;
  igst_rate_percent_snapshot: DecimalWire;
}

export interface PaymentOrder {
  provider: "razorpay";
  razorpay_key_id: string;
  razorpay_order_id: string;
  amount: DecimalWire;
  amount_subunits: number;
  currency: string;
  receipt: string | null;
}

export interface BookingPayment extends PaymentTaxSnapshot {
  id: string;
  booking_id: string;
  razorpay_order_id: string;
  razorpay_payment_id: string | null;
  status: RawPaymentStatus;
  amount: DecimalWire;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

export interface DomainErrorDetail {
  error: string;
  message: string;
  [extra: string]: unknown;
}
```

## 2. Endpoint delta: before and after

| Endpoint | Request change | Response/behavior change |
| --- | --- | --- |
| `POST /fare/preview` | none | Adds complete live GST breakdown; `amount` is gross. |
| `GET /route-trip-options` | none | Each option adds live GST breakdown; `fare_amount` is gross. |
| `GET /rfid/route-trip-options` | none | Same live GST fields plus selected in-progress tax amounts. |
| Traveller profile create/patch | same field names | Phone/email now strictly validated and normalized. |
| `POST /booking-sessions` | same seat modes | Inline traveller validation/conflicts are stricter; response adds session, seat, and payment tax snapshots. |
| Session list/current/detail/verify/cancel/per-seat cancel | none | Adds tax snapshots and refund/effective-payment state. |
| `POST /booking-sessions/{id}/retry-payment` | **new**, no body | Safely reconciles and reuses existing Razorpay order. |
| Booking create/list/current/history/detail/verify/cancel | none | Adds booking/payment tax snapshots. |
| `GET /transactions` | none | Adds payment tax snapshot fields and effective status. |
| `GET /bookings/{id}/invoice` | none | Adds IGST, tax inclusion, rounding data; snapshot-driven. |
| RFID summary/rides/detail | none | Adds immutable ride tax snapshots. |

No passenger request sends GST settings, rates, taxable value, or tax amount.

## 3. Fare preview and route discovery

### `POST /passenger/fare/preview`

Public; no bearer token required.

```ts
interface FarePreviewRequest {
  route_id: string;          // 1..36 chars
  pickup_stop_id: string;    // 1..36 chars
  dropoff_stop_id: string;   // 1..36 chars
}

interface FarePreviewResponse extends LiveTaxBreakdown {
  route_id: string;
  route_name: string;
  route_code: string;
  has_ac: boolean | null;
  pickup_stop: StopBrief;
  dropoff_stop: StopBrief;
  pickup_sequence_no: number;
  dropoff_sequence_no: number;
  amount: DecimalWire;       // final gross charge
}
```

Example:

```json
{
  "route_id": "route-id",
  "route_name": "Airport Express",
  "route_code": "AE-1",
  "has_ac": true,
  "pickup_stop": {"id":"a","name":"A","lat":"22.57","lng":"88.36","radius_meters":100,"is_active":true},
  "dropoff_stop": {"id":"b","name":"B","lat":"22.65","lng":"88.44","radius_meters":100,"is_active":true},
  "pickup_sequence_no": 1,
  "dropoff_sequence_no": 5,
  "amount": "100.00",
  "configured_fare_amount": "100.00",
  "taxable_amount": "95.24",
  "cgst_rate_percent": "2.50",
  "cgst_amount": "2.38",
  "sgst_rate_percent": "2.50",
  "sgst_amount": "2.38",
  "igst_rate_percent": "0.00",
  "igst_amount": "0.00",
  "total_tax_amount": "4.76",
  "gst_enabled": true,
  "gst_applicable": true,
  "gst_inclusive": true
}
```

### `GET /passenger/route-trip-options`

Public query:

```text
?from_stop_id=<1..36>&to_stop_id=<1..36>
&from_time=<optional ISO datetime>&to_time=<optional ISO datetime>
```

Response root:

```ts
interface RouteTripDiscoveryResponse {
  from_stop_id: string;
  to_stop_id: string;
  from_time: ISODateTime | null;
  to_time: ISODateTime | null;
  items: RouteTripOption[];
  count: number;
}

interface RouteTripOption extends LiveTaxBreakdown {
  route: Route; // existing route contract
  pickup_stop: StopBrief;
  dropoff_stop: StopBrief;
  pickup_sequence_no: number;
  dropoff_sequence_no: number;
  fare_amount: DecimalWire; // final gross charge
  upcoming_scheduled_trips: RouteTrip[];
  upcoming_scheduled_trip_count: number;
}
```

`GET /passenger/rfid/route-trip-options` uses identical query parameters but
requires auth. Each item has the same fields plus existing RFID trip arrays.
Each `rfid_in_progress_trips[]` now includes:

```ts
interface RFIDSelectedFareTaxFields {
  selected_fare_amount: DecimalWire;       // gross
  selected_taxable_amount: DecimalWire;
  selected_cgst_amount: DecimalWire;
  selected_sgst_amount: DecimalWire;
  selected_igst_amount: DecimalWire;
  selected_total_tax_amount: DecimalWire;
  required_hold_amount: DecimalWire | null; // gross hold
}
```

## 4. Traveller profile APIs

All routes require passenger auth.

```ts
interface TravellerProfileCreateRequest {
  full_name: string;                    // trimmed, 1..120
  phone: string;                        // 5..20 before normalization
  email?: string | null;                // max 255
  relationship_label?: string | null;   // max 80
  is_self?: boolean;                    // default false
}

interface TravellerProfileUpdateRequest {
  full_name?: string;
  phone?: string;
  email?: string | null;
  relationship_label?: string | null;
  is_self?: boolean;
  is_active?: boolean;
  // At least one field is required.
}

interface TravellerProfile {
  id: string;
  owner_user_id: string;
  full_name: string;
  phone: string;                         // normalized, e.g. +919876543210
  email: string | null;                  // normalized
  relationship_label: string | null;
  is_self: boolean;
  is_active: boolean;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

interface TravellerMutationResponse {
  message: string;
  profile: TravellerProfile;
}
```

| Method/path | Body/query | Response |
| --- | --- | --- |
| `GET /traveller-profiles` | `?active_only=true` by default | `{items: TravellerProfile[], count: number}` |
| `POST /traveller-profiles` | `TravellerProfileCreateRequest` | `TravellerMutationResponse` |
| `PATCH /traveller-profiles/{id}` | `TravellerProfileUpdateRequest` | `TravellerMutationResponse` |
| `DELETE /traveller-profiles/{id}` | none | `TravellerMutationResponse`; profile is deactivated rather than erased from historical snapshots |

Validation failure example (actual `loc` may include the list/seat indices):

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body", "seats", 1, "traveller", "phone"],
      "msg": "Value error, Traveller phone must be a valid Indian mobile number or an international number prefixed with +.",
      "input": "12345"
    }
  ]
}
```

## 5. Booking-session request

### `POST /passenger/booking-sessions`

Authenticated. One to ten unique seat numbers.

```ts
interface GuestTraveller {
  full_name: string;                    // 1..120
  phone: string;                        // 5..20
  email?: string | null;                // max 255
  relationship_label?: string | null;   // max 80
}

type BookingSessionSeatRequest =
  | { seat_number: number }
  | { seat_number: number; traveller_profile_id: string }
  | { seat_number: number; traveller: GuestTraveller };

interface CreateBookingSessionRequest {
  scheduled_trip_id: string;
  pickup_stop_id: string;
  dropoff_stop_id: string;
  seats: BookingSessionSeatRequest[];   // min 1, max 10; unique seat_number
}
```

Example:

```json
{
  "scheduled_trip_id": "trip-id",
  "pickup_stop_id": "pickup-id",
  "dropoff_stop_id": "drop-id",
  "seats": [
    {"seat_number": 1},
    {"seat_number": 2, "traveller_profile_id": "profile-id"},
    {
      "seat_number": 3,
      "traveller": {
        "full_name": "Guest Name",
        "phone": "+91 98765 43210",
        "email": "guest@example.com",
        "relationship_label": "Friend"
      }
    }
  ]
}
```

Create response:

```ts
interface BookingSessionCreateResponse {
  message: string;
  booking_session: BookingSession;
  payment_order: PaymentOrder;
}
```

## 6. Complete booking-session response

```ts
interface SeatRefund {
  id: string;
  booking_session_id: string;
  booking_id: string;
  booking_session_payment_id: string;
  owner_user_id: string;
  amount: DecimalWire;
  status: SeatRefundStatus;
  razorpay_refund_id: string | null;
  failure_reason: string | null;
  attempt_count: number;
  retry_after: ISODateTime | null;
  requested_at: ISODateTime;
  processed_at: ISODateTime | null;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

interface BookingSessionSeat extends BookingTaxSnapshot {
  id: string;                            // booking_id used for per-seat cancel
  booking_session_id: string | null;
  passenger_user_id: string;
  booked_by_user_id: string | null;
  traveller_profile_id: string | null;
  traveller_name_snapshot: string | null;
  traveller_phone_snapshot: string | null;
  traveller_email_snapshot: string | null;
  traveller_relationship_label_snapshot: string | null;
  scheduled_trip_id: string;
  route_id: string;
  pickup_stop_id: string;
  dropoff_stop_id: string;
  seat_number: number;
  otp: string | null;
  booking_status: BookingStatus;
  fare_amount: DecimalWire;               // per-seat gross
  payment_hold_expires_at: ISODateTime | null;
  refund: SeatRefund | null;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

interface BookingSessionPayment extends PaymentTaxSnapshot {
  id: string;
  booking_session_id: string;
  razorpay_order_id: string;
  razorpay_payment_id: string | null;
  razorpay_refund_id: string | null;
  status: RawPaymentStatus;
  effective_status: EffectivePaymentStatus;
  amount: DecimalWire;
  refunded_amount: DecimalWire;
  refund_requested_at: ISODateTime | null;
  refund_processed_at: ISODateTime | null;
  refund_retry_after: ISODateTime | null;
  refund_attempt_count: number | null;
  refund_failure_reason: string | null;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

interface BookingSession extends SessionTaxSnapshot {
  id: string;
  owner_user_id: string;
  scheduled_trip_id: string;
  route_id: string;
  pickup_stop_id: string;
  dropoff_stop_id: string;
  pickup_sequence_no_snapshot: number;
  dropoff_sequence_no_snapshot: number;
  status: BookingSessionStatus;
  total_fare_amount: DecimalWire;         // complete gross charge
  payment_hold_expires_at: ISODateTime | null;
  confirmed_at: ISODateTime | null;
  cancelled_at: ISODateTime | null;
  expired_at: ISODateTime | null;
  bookings: BookingSessionSeat[];
  payments: BookingSessionPayment[];
  created_at: ISODateTime;
  updated_at: ISODateTime;
}
```

## 7. Verify, retry, fetch, and cancel session APIs

### Verify payment

```text
POST /passenger/booking-sessions/{session_id}/verify-payment
```

```ts
interface VerifyPaymentRequest {
  razorpay_order_id: string;  // 1..64
  razorpay_payment_id: string;// 1..64
  razorpay_signature: string; // 1..255
}

interface BookingSessionMutationResponse {
  message: string;
  booking_session: BookingSession;
}
```

### Retry/resume payment

```text
POST /passenger/booking-sessions/{session_id}/retry-payment
body: none
```

```ts
interface BookingSessionRetryPaymentResponse {
  message: string;
  booking_session: BookingSession;
  payment_order: PaymentOrder | null;
}
```

Interpret `null` as already confirmed/reconciled, not as a malformed response.

### Read endpoints

| Endpoint | Query | Response |
| --- | --- | --- |
| `GET /booking-sessions` | optional `status=pending_payment|confirmed|cancelled|expired` | `{items: BookingSession[], count}` |
| `GET /booking-sessions/current` | none | existing current-session shape with session tax totals and booking tax snapshots |
| `GET /booking-sessions/{id}` | none | `BookingSession` |
| `GET /booking-sessions/{id}/current-status` | none | `{booking_session_id, items: CurrentTripStatus[], count}` |
| `GET /booking-sessions/{id}/live-location` | none | `{booking_session_id, items: CurrentTripLiveLocation[], count}` |

### Cancel endpoints

```text
POST /passenger/booking-sessions/{session_id}/cancel
POST /passenger/booking-sessions/{session_id}/bookings/{booking_id}/cancel
```

Both have no body and return `BookingSessionMutationResponse`. `booking_id` is
the seat object's `id`, not its `seat_number` or traveller profile ID.

## 8. Payment and session error action table

| HTTP | `detail.error` | Extra fields | Required UI behavior |
| --- | --- | --- | --- |
| 404 | `booking_session_not_found` | none | Remove stale local checkout/session reference. |
| 409 | `booking_session_empty` | none | Refetch; do not retry checkout. |
| 409 | `booking_session_not_retryable` | `status` | Refetch and render terminal status. |
| 409 | `payment_hold_expired` | `reconciliation_outcome` | Stop checkout; seats released; offer a new booking. |
| 409 | `payment_processing` | `reconciliation_outcome` | Show pending confirmation and bounded polling/refetch. |
| 409 | `booking_session_payment_not_found` | none | Do not create an order client-side; report/restart booking flow. |
| 409 | `booking_session_not_pending_payment` | `status` where applicable | Refetch; do not verify again blindly. |
| 400 | `invalid_payment_signature` | none | Never mark paid; show verification failure/support path. |
| 400 | `payment_order_mismatch` | none | Treat as non-recoverable client/provider mismatch; refetch. |
| 400 | `payment_amount_mismatch` | none | Never retry with a client-calculated amount; refetch/report. |
| 409 | `payment_not_successful` | `provider_status` | Offer retry only if session remains pending/in hold. |
| 409 | `payment_not_captured` | `provider_status` | Show processing and refetch/retry reconciliation. |
| 409 | `pending_session_seat_cancellation_not_supported` | none | Offer whole-session cancel. |
| 409 | `booking_session_not_confirmed` | `status` | Hide per-seat cancel; refetch. |
| 409 | `paid_booking_session_payment_not_found` | none | Do not promise refund; support/error state. |
| 409 | `booking_session_not_cancellable` | `status` | Disable cancellation and refetch. |
| 409 | `trip_already_started` | none | Cancellation has closed; refresh trip/session state. |
| 409 | `booking_session_contains_non_cancellable_seats` | `booking_ids` | Do not cancel the whole session; render affected seats and refetch. |

## 9. Traveller and seat error action table

| HTTP | `detail.error` | Important fields | UI action |
| --- | --- | --- | --- |
| 422 | validation array | `loc`, `msg`, `input` | Attach to phone/email/name/seat input. No session/order was created. |
| 400 | `traveller_matches_account_owner` | `seat_number` | Tell user to choose self mode by omitting traveller fields. |
| 409 | `guest_matches_saved_traveller` | `seat_number`, `traveller_profile_id`, `traveller_profile_is_active` | Select or reactivate saved traveller. |
| 400 | `traveller_profile_inactive` | none | Reload profiles and ask user to reactivate/select another. |
| 409 | `duplicate_traveller_in_booking_session` | `seat_number_groups` | Highlight every duplicate seat group. |
| 409 | `traveller_booking_conflict` | `seat_number`, conflicting booking/trip IDs, `conflict_type`, `transfer_buffer_minutes` | Preserve selections; change traveller/journey. |
| 400 | `duplicate_seat_numbers` | none | Deduplicate local seat state. |
| 400 | `invalid_seat_number` | `seat_number`, `seat_capacity` | Refresh seat map and selection. |
| 409 | `seat_unavailable` | `seat_numbers` | Refresh seat map; highlight lost seats. |
| 409 | `trip_segment_full` | `requested_seat_count`, `available_seat_count` | Reduce seats or choose another trip. |
| 409 | `trip_not_bookable` / `trip_already_started` | none | Return to search and refresh discovery. |

## 10. Existing booking response additions

The following existing passenger endpoints now include GST snapshots on each
booking and payment:

```text
POST /passenger/bookings
POST /passenger/bookings/{id}/verify-payment
POST /passenger/bookings/{id}/cancel
GET  /passenger/bookings
GET  /passenger/bookings/upcoming
GET  /passenger/bookings/current
GET  /passenger/history
GET  /passenger/bookings/{id}
```

Booking additions are `BookingTaxSnapshot`. Payment additions are
`PaymentTaxSnapshot`. Existing `fare_amount` and payment `amount` remain gross.
Traveller snapshot fields also exist on booking/session current status and live
location responses; prefer them over today's saved profile values.

The legacy single-seat create endpoint remains available. New multi-traveller
work should use booking sessions; it gives one order for all seats and supports
the safe retry operation documented here.

## 11. Transactions

```text
GET /passenger/transactions
  ?status=created|paid|failed|refunded
  &month=1..12
  &year=2000..2100
  &limit=1..200 (default 50)
  &offset>=0 (default 0)
```

`month` requires `year`; otherwise HTTP 400
`year_required_for_month_filter`.

```ts
interface PassengerTransaction extends PaymentTaxSnapshot {
  payment_id: string;
  booking_id: string;
  seat_number: number;
  scheduled_trip_id: string;
  route_id: string;
  booking_status: BookingStatus;
  payment_status: RawPaymentStatus;
  effective_status: EffectivePaymentStatus;
  amount: DecimalWire;                   // gross
  razorpay_order_id: string;
  razorpay_payment_id: string | null;
  pickup_stop: StopBrief;
  dropoff_stop: StopBrief;
  route_name: string | null;
  route_code: string | null;
  planned_start_at: ISODateTime | null;
  planned_end_at: ISODateTime | null;
  completed_at: ISODateTime | null;
  cancelled_at: ISODateTime | null;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

interface PassengerTransactionHistoryResponse {
  items: PassengerTransaction[];
  count: number;
}
```

The status filter uses raw status. Display `effective_status` so a paid record
whose booking is cancelled appears as `refund_pending`.

## 12. Invoice

```text
GET /passenger/bookings/{booking_id}/invoice
```

```ts
interface InvoiceBreakdown {
  total_booking_amount: DecimalWire;
  divisor_used: DecimalWire;
  taxable_value: DecimalWire;
  cgst_rate_percent: DecimalWire;
  cgst_amount: DecimalWire;
  sgst_rate_percent: DecimalWire;
  sgst_amount: DecimalWire;
  igst_rate_percent: DecimalWire;
  igst_amount: DecimalWire;
  total_tax_amount: DecimalWire;
  gst_inclusive: boolean;
  recomputed_total_amount: DecimalWire;
  rounding_adjustment: DecimalWire;
}

interface PassengerInvoice {
  invoice_number: string;
  booking_id: string;
  invoice_generated_at: ISODateTime;
  invoice_status: string; // currently "preview"
  passenger: {user_id: string; full_name: string | null; email: string | null};
  trip: {
    scheduled_trip_id: string;
    route_id: string;
    route_name: string | null;
    route_code: string | null;
    is_ac: boolean;
    pickup_stop: StopBrief;
    dropoff_stop: StopBrief;
    planned_start_at: ISODateTime | null;
    planned_end_at: ISODateTime | null;
    actual_start_at: ISODateTime | null;
    actual_end_at: ISODateTime | null;
    completed_at: ISODateTime | null;
  };
  breakdown: InvoiceBreakdown;
  payment: BookingPayment | null;
}
```

Invoice errors:

- HTTP 409 `invoice_not_available` with `booking_status` and `trip_status` if
  the booking is not completed;
- HTTP 409 `paid_payment_not_found` if no paid record exists.

## 13. RFID ride snapshot additions

Every `PassengerRFIDRide` returned by summary, list, or detail now adds:

```ts
interface RFIDRideTaxSnapshot extends BookingTaxSnapshot {
  // Existing ride fields continue to include:
  fare_amount: DecimalWire;       // settled gross
  hold_amount: DecimalWire;       // gross hold
  fare_reversed_amount: DecimalWire;
  fare_net_amount: DecimalWire;
}
```

Affected APIs:

```text
GET /passenger/rfid/summary
GET /passenger/rfid/rides?page=1&page_size=25&status=<optional>
GET /passenger/rfid/rides/{rfid_ride_id}
GET /passenger/rfid/route-trip-options
```

RFID recharge amounts are account funding, not trip fare GST. Do not attach
trip GST fields to the recharge-order UI.

## 14. Razorpay options mapping

Given a backend `PaymentOrder`, map checkout without modifying order identity
or amount:

```ts
const options = {
  key: paymentOrder.razorpay_key_id,
  order_id: paymentOrder.razorpay_order_id,
  amount: paymentOrder.amount_subunits,
  currency: paymentOrder.currency,
  // name, description, prefill, theme are frontend-owned presentation fields.
  handler: async (result: {
    razorpay_order_id: string;
    razorpay_payment_id: string;
    razorpay_signature: string;
  }) => verifySessionPayment(sessionId, result),
  modal: {
    ondismiss: () => showResumePaymentAction(sessionId),
  },
};
```

Do not:

- convert `amount` rupees back into subunits when `amount_subunits` exists;
- replace `order_id` with a newly created client/server order;
- extend the displayed hold after retry;
- mark confirmed inside Razorpay's handler before verify returns;
- call the webhook endpoint from browser code.

## 15. Minimal integration test matrix

| Case | Expected wire assertion |
| --- | --- |
| Inclusive GST | `amount === gross`; tax components sum to `total_tax_amount`; do not add tax to amount. |
| Exclusive GST | backend `amount` already includes computed tax; charge exactly it. |
| GST not applicable | component rates/amounts are zero; `gst_applicable=false`. |
| Three seats | session totals are authoritative and payment `amount_subunits` matches session gross. |
| Valid formatted phone | response phone is normalized `+E.164`. |
| Invalid guest email | HTTP 422 points into `seats[index].traveller.email`. |
| Retry before expiry | same `razorpay_order_id`; unchanged `payment_hold_expires_at`. |
| Already captured retry | HTTP 200; session confirmed; `payment_order=null`. |
| Authorized payment | 409 processing or backend capture/confirmation; no duplicate order. |
| Expired retry | HTTP 409 `payment_hold_expired`; terminal checkout UI. |
| Seat cancellation | cancelled seat gains independent `refund`; other seats remain active. |
| Late capture | session remains closed and refund fields eventually progress; never show booking confirmed solely from provider capture. |
