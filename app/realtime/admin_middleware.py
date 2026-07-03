from __future__ import annotations

import logging

from fastapi import Request, Response
from starlette.middleware.base import (
    BaseHTTPMiddleware,
    RequestResponseEndpoint,
)

from app.realtime.events import get_api_refresh_hub, publish_admin_event

logger = logging.getLogger(__name__)

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def admin_event_for_mutation(method: str, path: str) -> str | None:
    if (
        method.upper() not in MUTATING_METHODS
        or not path.startswith("/admin/")
    ):
        return None

    if path.startswith("/admin/rfid/"):
        return "admin.rfid_changed"
    if path.startswith("/admin/payouts/") or (
        path.startswith("/admin/drivers/")
        and path.endswith("/setup-payout-account")
    ):
        return "admin.payouts_changed"
    if path == "/admin/device-settings" or path.startswith(
        "/admin/commercial-rules"
    ):
        return "admin.settings_changed"
    if path.startswith("/admin/users/") and "/devices" in path:
        return "admin.users_changed"
    if path.startswith("/admin/driver/"):
        return "admin.drivers_changed"
    if path.startswith("/admin/vehicle/"):
        return "admin.vehicles_changed"
    if path.startswith("/admin/tickets/") or path == "/admin/support/create":
        return "admin.support_changed"
    if path.startswith("/admin/resolve-trip/"):
        return "admin.incidents_changed"

    # Routes/stops, trips, bookings, and manual trip actions already publish
    # richer domain events from their endpoint/service implementations.
    return None


class AdminRefreshMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        response = await call_next(request)
        event = admin_event_for_mutation(request.method, request.url.path)
        if event is None or not 200 <= response.status_code < 400:
            return response

        try:
            await publish_admin_event(
                get_api_refresh_hub(request.app),
                event=event,
                data={
                    "reason": "admin_mutation_completed",
                    "method": request.method.upper(),
                    "path": request.url.path,
                    "status_code": response.status_code,
                },
            )
        except Exception:
            # A post-commit refresh failure must not turn a successful admin
            # mutation into an HTTP failure.
            logger.exception(
                "admin_refresh_middleware_publish_failed method=%s path=%s",
                request.method,
                request.url.path,
            )

        return response
