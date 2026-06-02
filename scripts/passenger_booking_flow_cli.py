from __future__ import annotations

import argparse
import getpass
import json
import sys
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    timeout_seconds: float = 30.0


class ApiError(RuntimeError):
    pass


class PassengerBookingCli:
    def __init__(self, config: ApiConfig) -> None:
        self.config = config
        self.token: str | None = None
        self.client = httpx.Client(
            base_url=config.base_url.rstrip("/"),
            timeout=config.timeout_seconds,
            headers={"User-Agent": "passenger-booking-flow-cli/1.0"},
        )

    def close(self) -> None:
        self.client.close()

    # ----------------------------
    # HTTP boundary
    # ----------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        auth: bool = False,
    ) -> dict[str, Any] | list[Any] | None:
        headers: dict[str, str] = {}

        if auth:
            if not self.token:
                raise ApiError("Auth token is missing.")
            headers["Authorization"] = f"Bearer {self.token}"

        response = self.client.request(
            method,
            path,
            json=json_body,
            params=params,
            headers=headers,
        )

        if response.status_code == 204:
            return None

        try:
            payload = response.json()
        except ValueError:
            payload = {"raw_response": response.text}

        if response.status_code >= 400:
            pretty = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
            raise ApiError(
                f"{method} {path} failed with HTTP {response.status_code}\n{pretty}"
            )

        return payload

    def print_json(self, title: str, payload: Any) -> None:
        print(f"\n=== {title} ===")
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))

    # ----------------------------
    # Prompt helpers
    # ----------------------------

    def ask(self, label: str, *, default: str | None = None, required: bool = True) -> str:
        while True:
            suffix = f" [{default}]" if default is not None else ""
            value = input(f"{label}{suffix}: ").strip()
            if not value and default is not None:
                return default
            if value or not required:
                return value
            print("Required. Enter a value.")

    def ask_int(
        self,
        label: str,
        *,
        default: int | None = None,
        min_value: int | None = None,
        max_value: int | None = None,
    ) -> int:
        while True:
            raw_default = None if default is None else str(default)
            raw = self.ask(label, default=raw_default)
            try:
                value = int(raw)
            except ValueError:
                print("Enter a number.")
                continue

            if min_value is not None and value < min_value:
                print(f"Minimum allowed: {min_value}")
                continue

            if max_value is not None and value > max_value:
                print(f"Maximum allowed: {max_value}")
                continue

            return value

    def ask_yes_no(self, label: str, *, default: bool = False) -> bool:
        default_text = "y" if default else "n"
        while True:
            value = self.ask(f"{label} (y/n)", default=default_text).lower()
            if value in {"y", "yes"}:
                return True
            if value in {"n", "no"}:
                return False
            print("Enter y or n.")

    def choose_one(self, title: str, items: list[Any], render_item) -> Any:
        if not items:
            raise ApiError(f"No items available for: {title}")

        print(f"\n=== {title} ===")
        for index, item in enumerate(items, start=1):
            print(f"{index}. {render_item(item)}")

        selected = self.ask_int(
            "Select number",
            min_value=1,
            max_value=len(items),
        )
        return items[selected - 1]

    def choose_many_ints(
        self,
        title: str,
        values: list[int],
        *,
        max_count: int,
    ) -> list[int]:
        if not values:
            raise ApiError(f"No values available for: {title}")

        print(f"\n=== {title} ===")
        print(", ".join(str(value) for value in values))

        while True:
            raw = self.ask(
                f"Enter comma-separated seat numbers, max {max_count}",
            )
            try:
                selected = [int(part.strip()) for part in raw.split(",") if part.strip()]
            except ValueError:
                print("Use only numbers separated by commas.")
                continue

            if not selected:
                print("Select at least one seat.")
                continue

            if len(selected) > max_count:
                print(f"Maximum {max_count} seats allowed.")
                continue

            if len(selected) != len(set(selected)):
                print("Duplicate seat numbers are not allowed.")
                continue

            missing = [value for value in selected if value not in values]
            if missing:
                print(f"These seats are not available: {missing}")
                continue

            return selected

    # ----------------------------
    # Auth
    # ----------------------------

    def authenticate(self) -> None:
        print("\n=== Auth ===")
        if self.ask_yes_no("Already have bearer token?", default=False):
            self.token = getpass.getpass("Bearer token: ").strip()
            self.print_json("Auth /me", self.request("GET", "/auth/me", auth=True))
            return

        mode = self.choose_one(
            "Auth mode",
            ["login existing passenger", "signup new passenger"],
            lambda item: item,
        )

        email = self.ask("Passenger email")
        role = "passenger"

        if mode.startswith("signup"):
            self.request(
                "POST",
                "/auth/signup/send-otp",
                json_body={"email": email, "role": role},
            )
            print("Signup OTP sent. Enter the OTP from mail/SMS/log.")
            otp = self.ask("OTP")
            self.request(
                "POST",
                "/auth/signup/verify-otp",
                json_body={"email": email, "otp": otp, "role": role},
            )
            auth_payload = self.request(
                "POST",
                "/auth/signup",
                json_body={
                    "email": email,
                    "otp": otp,
                    "role": role,
                    "device": {
                        "device_name": "Passenger Booking CLI",
                        "device_family": "CLI",
                        "platform": "local",
                        "browser": None,
                    },
                },
            )
        else:
            self.request(
                "POST",
                "/auth/login/send-otp",
                json_body={"email": email, "role": role},
            )
            print("Login OTP sent. Enter the OTP from mail/SMS/log.")
            otp = self.ask("OTP")
            self.request(
                "POST",
                "/auth/login/verify-otp",
                json_body={"email": email, "otp": otp, "role": role},
            )
            auth_payload = self.request(
                "POST",
                "/auth/login",
                json_body={
                    "email": email,
                    "otp": otp,
                    "role": role,
                    "device": {
                        "device_name": "Passenger Booking CLI",
                        "device_family": "CLI",
                        "platform": "local",
                        "browser": None,
                    },
                },
            )

        if not isinstance(auth_payload, dict) or not auth_payload.get("access_token"):
            raise ApiError("Auth response did not include access_token.")

        self.token = str(auth_payload["access_token"])
        self.print_json("Authenticated user", auth_payload.get("user"))

    # ----------------------------
    # Passenger profile
    # ----------------------------

    def ensure_passenger_profile(self) -> None:
        print("\n=== Passenger profile ===")

        try:
            profile = self.request("GET", "/passenger/profile", auth=True)
            self.print_json("Existing passenger profile", profile)
            if self.ask_yes_no("Keep this passenger profile?", default=True):
                return
        except ApiError as exc:
            print(f"No usable passenger profile yet.\n{exc}")

        full_name = self.ask("Passenger full name")
        try:
            payload = self.request(
                "POST",
                "/passenger/profile",
                json_body={"full_name": full_name, "profile_picture_path": None},
                auth=True,
            )
        except ApiError:
            payload = self.request(
                "PATCH",
                "/passenger/profile",
                json_body={"full_name": full_name, "profile_picture_path": None},
                auth=True,
            )

        self.print_json("Passenger profile saved", payload)

    # ----------------------------
    # Traveller profiles
    # ----------------------------

    def list_traveller_profiles(self) -> list[dict[str, Any]]:
        payload = self.request(
            "GET",
            "/passenger/traveller-profiles",
            params={"active_only": True},
            auth=True,
        )
        if not isinstance(payload, dict):
            return []
        items = payload.get("items", [])
        return items if isinstance(items, list) else []

    def maybe_create_traveller_profiles(self) -> list[dict[str, Any]]:
        profiles = self.list_traveller_profiles()
        self.print_json("Existing traveller profiles", {"items": profiles, "count": len(profiles)})

        while self.ask_yes_no("Create a traveller profile now?", default=not profiles):
            full_name = self.ask("Traveller full name")
            phone = self.ask("Traveller phone")
            email = self.ask("Traveller email", required=False)
            relationship = self.ask("Relationship label", required=False)
            is_self = self.ask_yes_no("Is this the passenger self profile?", default=False)

            payload = self.request(
                "POST",
                "/passenger/traveller-profiles",
                json_body={
                    "full_name": full_name,
                    "phone": phone,
                    "email": email or None,
                    "relationship_label": relationship or None,
                    "is_self": is_self,
                },
                auth=True,
            )
            self.print_json("Traveller profile created", payload)
            profiles = self.list_traveller_profiles()

        return profiles

    # ----------------------------
    # Route/trip/seat selection
    # ----------------------------

    def select_route_trip_and_stops(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        routes_payload = self.request("GET", "/passenger/routes", auth=False)
        if not isinstance(routes_payload, dict):
            raise ApiError("Invalid routes response.")

        routes = routes_payload.get("items", [])
        if not isinstance(routes, list):
            raise ApiError("Routes response did not contain items.")

        route = self.choose_one(
            "Routes",
            routes,
            lambda route_item: (
                f"{route_item.get('name')} "
                f"({route_item.get('code')}) "
                f"AC={route_item.get('has_ac')} "
                f"stops={len(route_item.get('stops') or [])}"
            ),
        )

        stops = sorted(route.get("stops") or [], key=lambda item: item.get("sequence_no") or 0)
        boarding_stops = [item for item in stops if item.get("boarding_allowed")]
        pickup_route_stop = self.choose_one(
            "Pickup stop",
            boarding_stops,
            lambda item: (
                f"#{item.get('sequence_no')} "
                f"{(item.get('stop') or {}).get('name')} "
                f"[{(item.get('stop') or {}).get('id')}]"
            ),
        )

        pickup_sequence = int(pickup_route_stop.get("sequence_no") or 0)
        deboarding_stops = [
            item
            for item in stops
            if item.get("deboarding_allowed") and int(item.get("sequence_no") or 0) > pickup_sequence
        ]
        dropoff_route_stop = self.choose_one(
            "Dropoff stop",
            deboarding_stops,
            lambda item: (
                f"#{item.get('sequence_no')} "
                f"{(item.get('stop') or {}).get('name')} "
                f"[{(item.get('stop') or {}).get('id')}]"
            ),
        )

        trips_payload = self.request(
            "GET",
            "/passenger/scheduled-trips",
            params={"route_id": route["id"], "only_future": True},
        )
        if not isinstance(trips_payload, dict):
            raise ApiError("Invalid scheduled trips response.")

        trips = trips_payload.get("items", [])
        if not isinstance(trips, list):
            raise ApiError("Scheduled trips response did not contain items.")

        trip = self.choose_one(
            "Future scheduled trips",
            trips,
            lambda item: (
                f"{item.get('planned_start_at')} → {item.get('planned_end_at')} "
                f"status={item.get('status')} "
                f"trip={item.get('id')}"
            ),
        )

        return route, trip, pickup_route_stop, dropoff_route_stop

    def preview_fare_and_select_seats(
        self,
        *,
        route: dict[str, Any],
        trip: dict[str, Any],
        pickup_route_stop: dict[str, Any],
        dropoff_route_stop: dict[str, Any],
    ) -> list[int]:
        pickup_stop = pickup_route_stop["stop"]
        dropoff_stop = dropoff_route_stop["stop"]

        fare_payload = self.request(
            "POST",
            "/passenger/fare/preview",
            json_body={
                "route_id": route["id"],
                "pickup_stop_id": pickup_stop["id"],
                "dropoff_stop_id": dropoff_stop["id"],
            },
        )
        self.print_json("Fare preview", fare_payload)

        seats_payload = self.request(
            "POST",
            f"/passenger/scheduled-trips/{trip['id']}/available-seats",
            json_body={
                "route_id": route["id"],
                "pickup_stop_id": pickup_stop["id"],
                "dropoff_stop_id": dropoff_stop["id"],
                "seat_number": None,
            },
            auth=True,
        )
        self.print_json("Available seats", seats_payload)

        if not isinstance(seats_payload, dict):
            raise ApiError("Invalid available seats response.")

        available_seat_numbers = seats_payload.get("available_seat_numbers", [])
        if not isinstance(available_seat_numbers, list):
            raise ApiError("Available seats response did not contain available_seat_numbers.")

        return self.choose_many_ints(
            "Available seat numbers",
            [int(value) for value in available_seat_numbers],
            max_count=10,
        )

    # ----------------------------
    # Booking session
    # ----------------------------

    def build_seat_payloads(
        self,
        *,
        selected_seat_numbers: list[int],
        traveller_profiles: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        seats: list[dict[str, Any]] = []

        for seat_number in selected_seat_numbers:
            print(f"\n=== Traveller for seat {seat_number} ===")

            source = self.choose_one(
                "Traveller source",
                ["existing traveller profile", "inline guest traveller"],
                lambda item: item,
            )

            if source.startswith("existing"):
                if not traveller_profiles:
                    print("No existing traveller profiles. Creating inline guest instead.")
                    source = "inline guest traveller"
                else:
                    profile = self.choose_one(
                        "Traveller profiles",
                        traveller_profiles,
                        lambda item: (
                            f"{item.get('full_name')} "
                            f"phone={item.get('phone')} "
                            f"relationship={item.get('relationship_label')} "
                            f"id={item.get('id')}"
                        ),
                    )
                    seats.append(
                        {
                            "seat_number": seat_number,
                            "traveller_profile_id": profile["id"],
                        }
                    )
                    continue

            full_name = self.ask("Guest traveller full name")
            phone = self.ask("Guest traveller phone")
            email = self.ask("Guest traveller email", required=False)
            relationship = self.ask("Guest relationship label", required=False)

            seats.append(
                {
                    "seat_number": seat_number,
                    "traveller": {
                        "full_name": full_name,
                        "phone": phone,
                        "email": email or None,
                        "relationship_label": relationship or None,
                    },
                }
            )

        return seats

    def create_booking_session(
        self,
        *,
        route: dict[str, Any],
        trip: dict[str, Any],
        pickup_route_stop: dict[str, Any],
        dropoff_route_stop: dict[str, Any],
        seats: list[dict[str, Any]],
    ) -> dict[str, Any]:
        pickup_stop = pickup_route_stop["stop"]
        dropoff_stop = dropoff_route_stop["stop"]

        payload = {
            "scheduled_trip_id": trip["id"],
            "pickup_stop_id": pickup_stop["id"],
            "dropoff_stop_id": dropoff_stop["id"],
            "seats": seats,
        }

        self.print_json("Create booking session request", payload)

        response = self.request(
            "POST",
            "/passenger/booking-sessions",
            json_body=payload,
            auth=True,
        )

        self.print_json("Booking session created", response)

        if not isinstance(response, dict):
            raise ApiError("Invalid booking session response.")

        return response

    def maybe_verify_payment(self, booking_session_response: dict[str, Any]) -> None:
        booking_session = booking_session_response.get("booking_session") or {}
        payment_order = booking_session_response.get("payment_order") or {}

        booking_session_id = booking_session.get("id")
        razorpay_order_id = payment_order.get("razorpay_order_id")

        if not booking_session_id or not razorpay_order_id:
            print("Cannot verify payment: missing booking_session.id or payment_order.razorpay_order_id.")
            return

        print("\n=== Payment ===")
        print("The backend has created a Razorpay order.")
        self.print_json("Payment order", payment_order)

        if not self.ask_yes_no("Do you have Razorpay payment_id + signature to verify now?", default=False):
            print("Skipping payment verification. Booking session remains pending payment.")
            return

        razorpay_payment_id = self.ask("razorpay_payment_id")
        razorpay_signature = self.ask("razorpay_signature")

        response = self.request(
            "POST",
            f"/passenger/booking-sessions/{booking_session_id}/verify-payment",
            json_body={
                "razorpay_order_id": razorpay_order_id,
                "razorpay_payment_id": razorpay_payment_id,
                "razorpay_signature": razorpay_signature,
            },
            auth=True,
        )

        self.print_json("Payment verification result", response)

    def maybe_show_or_cancel_session(self, booking_session_response: dict[str, Any]) -> None:
        booking_session = booking_session_response.get("booking_session") or {}
        booking_session_id = booking_session.get("id")

        if not booking_session_id:
            return

        detail = self.request(
            "GET",
            f"/passenger/booking-sessions/{booking_session_id}",
            auth=True,
        )
        self.print_json("Booking session detail", detail)

        if self.ask_yes_no("Cancel this booking session?", default=False):
            cancelled = self.request(
                "POST",
                f"/passenger/booking-sessions/{booking_session_id}/cancel",
                auth=True,
            )
            self.print_json("Booking session cancelled", cancelled)

    # ----------------------------
    # Main flow
    # ----------------------------

    def run(self) -> None:
        self.authenticate()
        self.ensure_passenger_profile()

        traveller_profiles = self.maybe_create_traveller_profiles()

        route, trip, pickup_route_stop, dropoff_route_stop = self.select_route_trip_and_stops()

        selected_seat_numbers = self.preview_fare_and_select_seats(
            route=route,
            trip=trip,
            pickup_route_stop=pickup_route_stop,
            dropoff_route_stop=dropoff_route_stop,
        )

        seats = self.build_seat_payloads(
            selected_seat_numbers=selected_seat_numbers,
            traveller_profiles=traveller_profiles,
        )

        booking_session_response = self.create_booking_session(
            route=route,
            trip=trip,
            pickup_route_stop=pickup_route_stop,
            dropoff_route_stop=dropoff_route_stop,
            seats=seats,
        )

        self.maybe_verify_payment(booking_session_response)
        self.maybe_show_or_cancel_session(booking_session_response)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CLI tester for passenger multi-seat booking-session flow."
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Backend base URL. Default: http://127.0.0.1:8000",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    cli = PassengerBookingCli(
        ApiConfig(
            base_url=args.base_url,
        )
    )

    try:
        cli.run()
        print("\nDone.")
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except ApiError as exc:
        print(f"\nERROR:\n{exc}", file=sys.stderr)
        return 1
    finally:
        cli.close()


if __name__ == "__main__":
    raise SystemExit(main())