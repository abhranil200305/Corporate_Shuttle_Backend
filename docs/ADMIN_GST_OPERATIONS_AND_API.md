# Admin GST operations and API guide

This document teaches an admin frontend developer and an operations admin how
GST works in the platform: where the configuration comes from, when it is
consulted, when it is deliberately ignored, what each switch means, and which
APIs change behavior or return tax data.

It is the admin-only companion to the broader
[GST frontend contract](./GST_FRONTEND_INTEGRATION.md). Passenger implementation
details live in
[PASSENGER_FE_LATER_COMMITS_INTEGRATION.md](./PASSENGER_FE_LATER_COMMITS_INTEGRATION.md).

## 1. The one-minute mental model

There is one platform-wide GST policy, stored under the settings key
`"default"`.

The policy answers six questions:

1. Is GST globally enabled?
2. What CGST percentage should be used?
3. What SGST percentage should be used?
4. What IGST percentage should be used?
5. Should GST apply only to AC routes?
6. Is the configured route fare GST-inclusive or GST-exclusive?

The policy is used to price a fare at a specific moment. The resulting gross,
taxable value, component rates, and component amounts are then saved on the
booking/payment/session/RFID ride as a snapshot.

That gives the system two different kinds of values:

- **Current values**: calculated now from the current admin settings. Fare
  previews and route discovery use these.
- **Snapshot values**: frozen when a booking is created or an RFID fare is
  settled. History, invoices, payouts, refunds, and existing booking details
  use these.

Changing GST settings affects future calculations. It does not rewrite an
existing passenger's booking, invoice, payout basis, or refund amount.

## 2. Where the settings come from

### 2.1 Precedence

The effective configuration uses this precedence:

```text
persisted platform_settings row with settings_key="default"
    > GST_* environment values
    > built-in defaults
```

Built-in defaults:

| Setting | Default |
| --- | ---: |
| GST enabled | `true` |
| CGST | `2.50%` |
| SGST | `2.50%` |
| IGST | `0.00%` |
| GST only on AC routes | `true` |
| Configured fares include GST | `true` |

Environment fallback keys:

```dotenv
GST_ENABLED=true
GST_CGST_RATE_PERCENT=2.50
GST_SGST_RATE_PERCENT=2.50
GST_IGST_RATE_PERCENT=0.00
GST_APPLY_ON_AC_ROUTES_ONLY=true
GST_INCLUSIVE_PRICING=true
```

Boolean environment values accept `1/0`, `true/false`, `yes/no`, and `on/off`,
case-insensitively. Each rate must be a decimal from `0` through `100`.
Malformed environment values fail explicitly; they do not silently fall back.

### 2.2 What happens when no database row exists

`GET /admin/gst/settings` returns the environment-backed policy without
creating a database row. In that response, `created_at` and `updated_at` are
`null`.

The first `PATCH /admin/gst/settings` creates the default settings row. Before
applying the submitted fields, the backend seeds every GST field from the
environment/built-in fallback. Omitted fields therefore keep that baseline.

Other platform operations can also create the shared default settings row—for
example payout or commercial settings initialization. They use the same GST
environment seed. The admin UI must therefore trust `GET /admin/gst/settings`
instead of assuming that `created_at` will remain null until the GST screen is
saved.

### 2.3 What happens after a row exists

The persisted row is authoritative. Restarting with different `GST_*`
environment values does not override it. An admin changes the live policy with
the PATCH API.

This is intentional: `.env` is a bootstrap/fallback, while the admin API is the
runtime control plane.

## 3. Exact admin settings API

All `/admin/*` routes require an active admin bearer token.

### 3.1 Read the current policy

```http
GET /admin/gst/settings
Authorization: Bearer <admin-access-token>
```

There is no body and no query string.

```ts
type ApiDecimal = string | number;

interface GSTSettings {
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

Persisted response example:

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
  "updated_at": "2026-07-09T06:00:00+00:00"
}
```

Environment-only response has the same policy fields but:

```json
{
  "created_at": null,
  "updated_at": null
}
```

### 3.2 Partially update the policy

```http
PATCH /admin/gst/settings
Authorization: Bearer <admin-access-token>
Content-Type: application/json
```

```ts
interface GSTSettingsPatch {
  gst_enabled?: boolean | null;
  gst_cgst_rate_percent?: ApiDecimal | null;
  gst_sgst_rate_percent?: ApiDecimal | null;
  gst_igst_rate_percent?: ApiDecimal | null;
  gst_apply_on_ac_routes_only?: boolean | null;
  gst_inclusive_pricing?: boolean | null;
}
```

Every field is optional. Omitted fields remain unchanged. An explicit `null`
is also ignored, so use a concrete value to clear/toggle a field; there is no
nullable GST setting in persistence.

Do not send `{}` as a harmless no-op when no row exists. It is accepted, but it
creates and persists the environment-backed baseline, after which future `.env`
changes no longer override GST.

Example enabling 5% inclusive GST only on AC routes:

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

Success:

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

Each component rate is independently validated from `0` through `100`. A rate
outside that range returns HTTP 422 using FastAPI's validation-array envelope.
There is no server rule that CGST + SGST + IGST must total at most 100, and the
backend does not automatically choose IGST instead of CGST/SGST. It sums every
configured component. The admin/product must set the legally intended
combination.

Example 422 shape:

```json
{
  "detail": [
    {
      "type": "less_than_equal",
      "loc": ["body", "gst_cgst_rate_percent"],
      "msg": "Input should be less than or equal to 100",
      "input": 101,
      "ctx": {"le": 100}
    }
  ]
}
```

## 4. What every setting actually does

### `gst_enabled`

- `false`: GST never applies, regardless of route or rates.
- `true`: GST is allowed to apply, but the route rule and total rate are still
  checked.

This flag alone does not mean a particular fare contains tax.

### Component rates

The backend calculates:

```text
total_rate = CGST rate + SGST rate + IGST rate
```

If `total_rate == 0`, GST is not applicable even when enabled. There is no
automatic interstate/intrastate decision and no mutual exclusion between IGST
and CGST/SGST.

### `gst_apply_on_ac_routes_only`

- `true`: tax applies only when the route's `has_ac` value is true. A false,
  null, or unknown AC flag is treated as non-AC for GST.
- `false`: both AC and non-AC routes can be taxed.

The route's `has_ac` flag is the deciding input. Vehicle air-conditioning data
or a frontend label does not override it.

### `gst_inclusive_pricing`

- `true`: the route fare already includes GST. The passenger gross stays equal
  to the configured fare; the backend extracts taxable value and tax from it.
- `false`: the route fare is the taxable base. The backend adds tax, so the
  passenger gross is higher than the configured fare.

This flag is global, not per-route.

## 5. The applicability decision

For every live calculation, the backend effectively applies this decision:

```ts
const totalRate = cgst + sgst + igst;
const gstApplicable =
  gstEnabled &&
  totalRate > 0 &&
  (!applyOnACOnly || route.has_ac === true);
```

| Enabled | Total rate | AC-only | Route AC | Result |
| --- | ---: | --- | --- | --- |
| false | any | any | any | No GST |
| true | 0 | any | any | No GST |
| true | > 0 | true | true | GST applies |
| true | > 0 | true | false/null | No GST |
| true | > 0 | false | true/false/null | GST applies |

When GST does not apply:

- gross equals configured fare;
- taxable amount equals configured fare;
- all returned component rates and amounts are `0.00`;
- total tax is `0.00`;
- live `gst_applicable` is false;
- `gst_enabled` may still be true;
- inclusive/exclusive flag still reports the configured mode, even though no
  tax was applied.

Persisted records do not have a `gst_applicable_snapshot` field. Determine what
was charged from the stored component rates/amounts and total tax, not from
`gst_enabled_snapshot` alone.

## 6. Calculation examples

Money and percentages are rounded half-up. Money has two decimal places;
percentage settings/snapshots have two decimal places.

### 6.1 Inclusive 5% GST

Policy: CGST 2.5%, SGST 2.5%, inclusive. Configured route fare: `100.00`.

```text
divisor       = 1 + 5 / 100 = 1.05
gross         = 100.00
taxable       = round(100 / 1.05) = 95.24
CGST          = round(95.24 * 2.5%) = 2.38
SGST          = round(95.24 * 2.5%) = 2.38
total tax     = 4.76
```

The passenger pays `100.00`, not `104.76`.

### 6.2 Exclusive 5% GST

Policy: CGST 2.5%, SGST 2.5%, exclusive. Configured route fare: `100.00`.

```text
taxable       = 100.00
gross         = round(100 + 5%) = 105.00
CGST          = 2.50
SGST          = 2.50
total tax     = 5.00
```

The passenger pays `105.00`. The backend's returned gross/payment order is
authoritative; a client must not charge the configured `100.00`.

### 6.3 No GST

Configured fare `100.00`, but GST is disabled, rates total zero, or the route
is non-AC under AC-only policy:

```text
gross         = 100.00
taxable       = 100.00
all tax       = 0.00
```

### 6.4 Rounding

Component amounts are independently rounded, then summed. Therefore:

```text
taxable + component taxes
```

can differ from gross by a small rounding adjustment. The passenger invoice
returns `rounding_adjustment`; other screens should use the returned gross and
tax fields instead of trying to force an equality.

### 6.5 Route-fare admin APIs store the configured fare, not a GST quote

```text
POST /admin/routes/fares/bulk-set
GET  /admin/routes/{route_id}/fares
```

The bulk-set request remains:

```ts
interface RouteFareBulkSetRequest {
  route_id: string;
  fares: Array<{
    pickup_stop_id: string;
    dropoff_stop_id: string;
    amount: ApiDecimal; // >= 0; stored configured fare
  }>;
}
```

Neither route-fare admin endpoint calculates or returns a GST breakdown. The
stored `amount` is interpreted later by passenger pricing:

- inclusive mode: stored amount is the passenger gross;
- exclusive mode: stored amount is taxable base and GST is added to it;
- GST not applicable: stored amount is both taxable and gross.

Changing GST settings does not modify route-fare rows. It changes how future
live pricing interprets them. For that reason, an admin settings screen should
offer a fare-preview check before/after changing inclusive mode.

## 7. When the current GST policy is used

### Every request/refetch

These APIs calculate with the current policy every time they run:

| API | Behavior |
| --- | --- |
| `POST /passenger/fare/preview` | Calculates a live breakdown for route + pickup + drop. |
| `GET /passenger/route-trip-options` | Calculates live GST fields for every route option. |
| `GET /passenger/rfid/route-trip-options` | Calculates live route GST, selected fare tax, and gross hold requirements. |

If an admin patches GST between two requests, these responses can change.

### Once, when a record is created or settled

| Operation | Moment current settings are captured |
| --- | --- |
| Single booking creation | When the booking is created/priced. |
| Multi-seat booking session creation | Once for each seat and the session total. |
| Booking/session payment record | Copied from the booking/session tax snapshot. |
| RFID ride | Final fare/tax is captured when the ride is dropped or otherwise settled. |

The Razorpay payment order uses the resulting gross amount. Payment retry
reuses the same order and snapshot; it does not recalculate GST.

## 8. When current settings are not used

Current GST settings are deliberately not reapplied to:

- existing booking/session list or detail records;
- existing payment records;
- transaction history;
- completed booking invoices;
- cancellation/refund amounts;
- payout booking and refund queue records;
- settled RFID rides and their reversals;
- a pending booking-session payment retry;
- driver commission and payout already snapshotted for a booking/ride.

Admin screens must not fetch current GST settings and use them to relabel or
recalculate those historical records. Use their own snapshot fields.

RFID recharge is wallet/account funding rather than a settled trip fare. GST
trip breakdown fields do not change the recharge order amount.

## 9. Complete downstream API behavior matrix

### 9.1 Live passenger pricing APIs an admin may verify

| API | Gross field | Tax fields returned | Current or snapshot? |
| --- | --- | --- | --- |
| `POST /passenger/fare/preview` | `amount` | configured fare, taxable, 3 rates, 3 amounts, total, enabled/applicable/inclusive | Current |
| `GET /passenger/route-trip-options` | item `fare_amount` | same live fields | Current |
| `GET /passenger/rfid/route-trip-options` | item `fare_amount`; in-progress `selected_fare_amount`; `required_hold_amount` | option live fields and selected component amounts | Current |

Live breakdown shape:

```ts
interface LiveGSTBreakdown {
  configured_fare_amount: ApiDecimal;
  fare_amount: ApiDecimal; // called `amount` by fare preview
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

### 9.2 Persisted booking/session/payment APIs

The following passenger API families return saved snapshots:

- booking create, verify, cancel, list, upcoming, current, history, and detail;
- booking-session create, verify, retry, cancel, per-seat cancel, list, current,
  status, location, and detail;
- transaction history;
- booking invoice;
- passenger RFID summary, ride list, and ride detail.

Booking/RFID ride snapshot:

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

Payment snapshots contain taxable and component amounts but not component rate
fields. A booking session additionally returns total taxable/component values
and rate snapshots for the whole session.

### 9.3 Admin payout booking APIs

Affected endpoints:

```text
GET /admin/payouts/bookings
GET /admin/payouts/bookings/{booking_id}
POST /admin/payouts/refunds/{booking_id}/reconcile
```

`GET /admin/payouts/bookings` keeps its existing optional filters:

```text
driver_user_id
passenger_user_id
booking_status
transfer_status
month=1..12
year=2000..2100
```

List response is `{items, count}`. Detail and refund reconciliation return a
nested `booking`. Each serialized payout booking now includes `TaxSnapshot` in
addition to:

```ts
interface PayoutBookingMoney extends TaxSnapshot {
  fare_amount: ApiDecimal;                 // passenger gross
  commission_percent_snapshot: ApiDecimal;
  commission_amount: ApiDecimal;
  driver_payout_amount: ApiDecimal;
  applied_adjustment_amount: ApiDecimal;
  net_payout_amount: ApiDecimal;
}
```

GST changes the payout relationship:

```text
commission_amount = taxable_amount * commission_percent_snapshot / 100
driver_payout_amount = taxable_amount - commission_amount
```

Do not expect `commission + driver payout == gross fare`. Tax is excluded from
distributable revenue. The useful admin column order is:

```text
Gross fare | Tax | Taxable fare | Commission | Driver payout | Transfer state
```

Payout trigger, transfer, and dashboard endpoints keep their existing request
and response envelopes. Their monetary results are derived from the stored
taxable/payout snapshots; changing today's GST settings does not recalculate a
ready or completed historical payout.

### 9.4 Admin refund queue

```http
GET /admin/payouts/refunds
Authorization: Bearer <admin-access-token>
```

Response remains `{items, count}`. Each queued cancelled/paid booking adds the
same tax snapshot alongside booking, passenger, driver, refund state, payment
state, retry, and timestamp fields.

`fare_amount` is the original gross amount. Tax fields explain that gross; the
admin frontend must not split the refund into separate GST and fare refund
requests.

### 9.5 Admin RFID money detail

```http
GET /admin/rfid/rides/{rfid_ride_id}/money-detail
Authorization: Bearer <admin-access-token>
```

The response contains:

```ts
interface RFIDRideMoneyDetailResponse {
  ride: RFIDTripRide;
  ledger_entries: unknown[];
  funding_allocations: unknown[];
  payout_transfers: unknown[];
  payout_transfer_reversals: unknown[];
  ledger_entry_count: number;
  funding_allocation_count: number;
  payout_transfer_count: number;
  payout_transfer_reversal_count: number;
}
```

The nested ride now includes `TaxSnapshot`, gross `fare_amount`, hold,
reversed/net fare, commission, driver payout, and platform amount fields.

RFID commission is based on taxable fare. Reversal APIs accept the existing
gross reversal request and the backend allocates driver/platform reversal
components proportionally from stored snapshots. The frontend must not assume
that driver payout + platform commission equals the gross passenger fare.

The relevant reversal routes are unchanged:

```text
POST /admin/rfid/rides/{rfid_ride_id}/reverse-deduction
POST /admin/rfid/payout-transfers/{transfer_id}/reverse
```

### 9.6 Admin APIs that are not GST-breakdown surfaces

Do not assume every admin response containing a fare now contains tax fields:

| API family | GST behavior |
| --- | --- |
| `GET /admin/routes/{route_id}/fares` | Returns stored configured `amount`, not live gross/tax. |
| `POST /admin/routes/fares/bulk-set` | Stores configured amount; response contains only update counts. |
| `GET /admin/trips/{trip_id}` | Existing occupancy/RFID passenger entries expose fare totals but not the complete GST snapshot. |
| `GET/PATCH /admin/payouts/settings` | Controls commission percentage, not GST. |
| Payout transfer list/detail | Transfer amounts reflect stored payout calculations but do not repeat the booking tax breakdown. |
| RFID payout transfer list/detail/summary | Shows driver-payout transfer money; use RFID ride money detail for the ride tax snapshot. |
| RFID recharge APIs | Wallet funding, not a trip GST calculation. |

For a tax-aware booking audit, use `/admin/payouts/bookings/{booking_id}`. For a
tax-aware RFID audit, use `/admin/rfid/rides/{rfid_ride_id}/money-detail`.

## 10. Current versus snapshot reference

| Surface | Source | Changes after GST PATCH? |
| --- | --- | --- |
| Admin GST settings | Current persisted/env policy | Yes |
| Fare preview | Current policy | Yes, after refetch |
| Standard/RFID discovery | Current policy | Yes, after refetch |
| Newly created booking/session | Current policy captured at creation | New record only |
| Existing booking/session detail | Stored snapshot | No |
| Existing payment/transaction | Stored snapshot | No |
| Invoice | Stored booking snapshot | No |
| Open RFID ride before settlement | Final tax not known; can show zero snapshot | Settlement uses then-current policy |
| Settled RFID ride | Stored settlement snapshot | No |
| Payout/refund/reversal | Stored booking/ride snapshot | No |

## 11. Recommended admin screen

### Load

1. Call `GET /admin/gst/settings`.
2. Populate all six controls from the response.
3. If timestamps are null, show a subtle “Using environment defaults; not yet
   persisted” status.
4. Treat decimals as strings in the form to avoid floating-point drift.

### Controls and help text

| Control | Recommended help text |
| --- | --- |
| Enable GST | Master switch. Turning this off makes tax zero for new calculations only. |
| CGST rate | Applied together with every other non-zero component; 0–100%. |
| SGST rate | Applied together with every other non-zero component; 0–100%. |
| IGST rate | Not auto-selected by geography. Set CGST/SGST appropriately when using IGST. |
| AC routes only | When on, non-AC or unknown-AC routes receive zero GST. |
| Inclusive pricing | On: configured fare already contains GST. Off: GST is added to configured fare. |

### Save

1. Validate each rate locally as a decimal from 0 to 100.
2. Warn if IGST and CGST/SGST are simultaneously non-zero unless that is a
   deliberate business rule.
3. Show a confirmation explaining that only new/live pricing changes.
4. PATCH changed fields or the complete concrete form. Do not send nulls.
5. Replace local state with the returned `settings` object.
6. Invalidate live pricing/discovery caches; do not rewrite historical rows.

Suggested confirmation:

> This changes GST for new fare calculations and future bookings. Existing
> bookings, invoices, refunds, payouts, and settled RFID rides keep their saved
> tax values.

## 12. Realtime and cache behavior

A successful `PATCH /admin/gst/settings` publishes an admin refresh event:

```json
{
  "type": "api.refresh",
  "event": "admin.settings_changed",
  "audience": "admin",
  "resources": [
    "device_settings",
    "commercial_rules",
    "gst_settings",
    "rfid_settings"
  ],
  "endpoints": [
    "/admin/device-settings",
    "/admin/gst/settings",
    "/admin/commercial-rules",
    "/admin/commercial-rules/{rule_id}",
    "/admin/rfid/seat-policy"
  ],
  "data": {
    "reason": "admin_mutation_completed",
    "method": "PATCH",
    "path": "/admin/gst/settings",
    "status_code": 200
  },
  "occurred_at": "2026-07-14T12:00:00+00:00"
}
```

Other admin tabs should invalidate `GET /admin/gst/settings` when
`gst_settings` appears. The initiating tab should use the PATCH response
immediately.

The event is admin-only. Passenger tabs do not receive a dedicated GST settings
event. They receive the new policy on their next live fare/discovery request.
Do not expect already-open historical pages to mutate.

Transport and reconnect details are in
[ADMIN_API_REFRESH_WEBSOCKET.md](./ADMIN_API_REFRESH_WEBSOCKET.md).

## 13. Failure and edge-case handling

- HTTP 401/403: admin session is missing, expired, inactive, or not an admin.
- HTTP 422: one or more PATCH fields have the wrong type/range. Keep the form
  values and map `detail[].loc` to controls.
- HTTP 500 while no row exists can indicate malformed `GST_*` environment
  configuration. This requires backend/operations correction.
- A successful PATCH is committed before the refresh event. If WebSocket
  delivery fails, the HTTP response is still successful and authoritative.
- Two admins saving concurrently use last-commit-wins behavior. Refetch on a
  refresh event and show the returned `updated_at`.
- Decimal JSON can be a string or number depending on serialization. Accept
  both and display a fixed percentage format.
- `gst_enabled=true` plus zero rates is valid and produces no tax.
- AC-only plus non-AC route is valid and produces no tax.
- Explicit null does not reset a setting to its default; it is ignored.

## 14. Deployment and historical data

Migration `8d7f4c2a9b31_add_gst_settings_and_snapshots.py` must be applied before
this feature is used. It adds GST policy columns and snapshots to:

- `platform_settings`;
- `trip_bookings`;
- `booking_sessions`;
- `booking_payments`;
- `booking_session_payments`;
- `rfid_trip_rides`.

The migration backfills legacy booking/RFID data using the prior effective
policy: inclusive 2.5% CGST + 2.5% SGST for AC routes, zero tax for non-AC
routes. Those backfilled records remain snapshots and are not changed by a
later admin PATCH.

## 15. Admin acceptance checklist

### Settings API

- [ ] GET without a database row returns env/built-in values and null timestamps.
- [ ] A one-field PATCH creates the row and preserves env-backed omitted values.
- [ ] A later one-field PATCH preserves all other persisted values.
- [ ] Explicit null leaves the existing field unchanged.
- [ ] Values below 0 or above 100 return 422.
- [ ] Another admin tab refetches after `admin.settings_changed`.

### Pricing behavior

- [ ] Disabled GST produces zero tax for AC and non-AC routes.
- [ ] Enabled with all rates zero produces zero tax.
- [ ] AC-only taxes AC and does not tax non-AC/unknown-AC routes.
- [ ] AC-only off applies the configured components to both route types.
- [ ] Inclusive mode leaves gross equal to configured fare.
- [ ] Exclusive mode increases gross by the configured total rate.
- [ ] IGST-only displays IGST and zero CGST/SGST.
- [ ] The payment order amount always equals returned gross.

### Historical and financial behavior

- [ ] A GST change modifies a fresh preview but not an old booking detail.
- [ ] An old invoice uses its booking snapshot.
- [ ] Payment retry retains the original gross and tax snapshot.
- [ ] Refund queue displays original gross and tax without recalculation.
- [ ] Payout screens use taxable fare as the commission/driver-payout basis.
- [ ] A settled RFID ride remains unchanged after GST settings change.
