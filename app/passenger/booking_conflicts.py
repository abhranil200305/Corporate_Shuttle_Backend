from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

DEFAULT_TRANSFER_BUFFER_MINUTES = 15


def build_self_traveller_identity(owner_user_id: str) -> str:
    return f"self:{owner_user_id}"


def build_profile_traveller_identity(profile_id: str) -> str:
    return f"profile:{profile_id}"


def normalize_phone_for_identity(phone: str) -> str:
    normalized = re.sub(r"\D", "", phone)
    if not normalized:
        raise ValueError("A traveller phone must contain at least one digit.")
    return normalized


def build_guest_traveller_identity(owner_user_id: str, phone: str) -> str:
    normalized_phone = normalize_phone_for_identity(phone)
    digest = hashlib.sha256(normalized_phone.encode("utf-8")).hexdigest()
    return f"guest:{owner_user_id}:{digest}"


def route_segments_overlap(
    *,
    existing_pickup_sequence_no: int,
    existing_dropoff_sequence_no: int,
    requested_pickup_sequence_no: int,
    requested_dropoff_sequence_no: int,
) -> bool:
    """Return whether two half-open route legs [pickup, dropoff) overlap."""
    return (
        existing_pickup_sequence_no < requested_dropoff_sequence_no
        and existing_dropoff_sequence_no > requested_pickup_sequence_no
    )


def journey_windows_conflict(
    *,
    existing_start: datetime,
    existing_end: datetime,
    existing_pickup_stop_id: str,
    existing_dropoff_stop_id: str,
    requested_start: datetime,
    requested_end: datetime,
    requested_pickup_stop_id: str,
    requested_dropoff_stop_id: str,
    transfer_buffer_minutes: int = DEFAULT_TRANSFER_BUFFER_MINUTES,
) -> bool:
    """Check journey overlap and the minimum transfer time between trips.

    Touching windows are allowed only when the first journey's dropoff stop is
    the second journey's pickup stop. Other transfers need the configured
    buffer. The comparison is symmetric so it also handles booking an earlier
    journey after a later journey already exists.
    """
    if existing_end <= existing_start or requested_end <= requested_start:
        raise ValueError("Journey end time must be after its start time.")

    buffer = timedelta(minutes=max(transfer_buffer_minutes, 0))

    if requested_start >= existing_end:
        required_buffer = (
            timedelta(0)
            if existing_dropoff_stop_id == requested_pickup_stop_id
            else buffer
        )
        return requested_start < existing_end + required_buffer

    if existing_start >= requested_end:
        required_buffer = (
            timedelta(0)
            if requested_dropoff_stop_id == existing_pickup_stop_id
            else buffer
        )
        return existing_start < requested_end + required_buffer

    return True
