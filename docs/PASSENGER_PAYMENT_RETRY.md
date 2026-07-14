# Passenger payment retry integration

Passenger booking sessions support retrying an unsuccessful Razorpay payment
without creating another booking session or changing the original fare and GST
snapshots.

## Backend configuration

```env
PASSENGER_PAYMENT_HOLD_MINUTES=5
PAYMENT_RECONCILE_CLOSED_SESSION_LOOKBACK_HOURS=24
RAZORPAY_WEBHOOK_SECRET=<secret configured in the Razorpay dashboard>
```

The webhook is recommended but optional. When it is enabled, configure
Razorpay to send `order.paid`, `payment.authorized`,
`payment.captured`, and `payment.failed` events to:

```text
POST /passenger/payments/razorpay/webhook
```

The endpoint verifies `X-Razorpay-Signature` against the exact raw request
body. It must not be placed behind passenger authentication.

## Retry endpoint

```http
POST /passenger/booking-sessions/{booking_session_id}/retry-payment
Authorization: Bearer <passenger access token>
```

There is no request body.

Successful retry response:

```json
{
  "message": "Payment can be retried using the existing Razorpay order.",
  "booking_session": {
    "id": "booking-session-id",
    "status": "pending_payment",
    "payment_hold_expires_at": "2026-07-14T10:30:00Z",
    "bookings": [],
    "payments": []
  },
  "payment_order": {
    "provider": "razorpay",
    "razorpay_key_id": "rzp_test_or_live_key",
    "razorpay_order_id": "order_...",
    "amount": "500.00",
    "amount_subunits": 50000,
    "currency": "INR",
    "receipt": null
  }
}
```

Pass `payment_order.razorpay_order_id`, `amount_subunits`, `currency`, and
`razorpay_key_id` to Razorpay Checkout exactly as returned. The retry uses the
same Razorpay order, so multiple failed attempts cannot produce multiple
successful payments for the booking session.

The retry endpoint first reconciles the order with Razorpay:

- If payment already succeeded, the response has a confirmed booking session
  and `payment_order: null`.
- If payment is authorized or capture is still processing, the API returns
  HTTP `409` with `detail.error = "payment_processing"`. Do not open a second
  checkout; refresh the booking-session detail.
- If the fixed seat hold expired, the API returns HTTP `409` with
  `detail.error = "payment_hold_expired"`. The seats have been released.
- Cancelled, expired, and otherwise closed sessions return HTTP `409` with
  `detail.error = "booking_session_not_retryable"`.

Retrying never extends `payment_hold_expires_at`. The frontend should display a
countdown from the server timestamp, disable Retry while the request is in
flight, and stop offering Retry when the session is no longer
`pending_payment`.

## Verification after Checkout

The existing endpoint remains unchanged:

```http
POST /passenger/booking-sessions/{booking_session_id}/verify-payment
Authorization: Bearer <passenger access token>
Content-Type: application/json

{
  "razorpay_order_id": "order_...",
  "razorpay_payment_id": "pay_...",
  "razorpay_signature": "..."
}
```

Client verification provides the immediate UX response. The signed Razorpay
webhook and the background payment reconciler are the recovery paths when the
client closes, loses connectivity, or receives a delayed provider result.

If a captured payment arrives only after the fixed hold expired, the booking
session stays expired, the seats remain released, and per-seat refund requests
are queued automatically.

### Operation without webhooks

The background payment reconciler also scans recently expired and cancelled
booking sessions. For the configured lookback period, it fetches Razorpay
payments for locally `created` or `failed` orders and detects a payment that
became captured after the session closed. It then marks the session payment as
paid and creates one refund request per cancelled seat. The existing booking
seat refund job submits those refunds to Razorpay.

`PAYMENT_RECONCILE_CLOSED_SESSION_LOOKBACK_HOURS` defaults to `24`. Increasing
it gives more time to discover unusually late provider changes but also causes
more Razorpay status requests. Once the payment is detected and refund requests
are created, the closed-session scanner stops polling that order.
