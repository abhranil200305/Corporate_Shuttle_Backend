from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from textwrap import wrap
from typing import Any


def _text(value: Any, fallback: str = "N/A") -> str:
    if value is None or value == "":
        return fallback
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    return str(value)


def _money(value: Any) -> str:
    try:
        return f"INR {Decimal(str(value or 0)):.2f}"
    except Exception:
        return "INR 0.00"


def _invoice_lines(invoice: dict[str, Any]) -> list[str]:
    supplier = invoice.get("supplier") or {}
    passenger = invoice.get("passenger") or {}
    service = invoice.get("service") or {}
    place_of_supply = invoice.get("place_of_supply") or {}
    trip = invoice.get("trip") or {}
    breakdown = invoice.get("breakdown") or {}
    payment = invoice.get("payment") or {}
    compliance = invoice.get("compliance") or {}
    pickup = trip.get("pickup_stop") or {}
    dropoff = trip.get("dropoff_stop") or {}

    lines = [
        "GST INVOICE / PAYMENT RECEIPT",
        f"Status: {_text(invoice.get('invoice_status'), 'preview').upper()}",
        "",
        f"Invoice number: {_text(invoice.get('invoice_number'))}",
        f"Invoice generated at: {_text(invoice.get('invoice_generated_at'))}",
        f"Booking ID: {_text(invoice.get('booking_id'))}",
        f"Booking created at: {_text(invoice.get('booking_created_at'))}",
        f"Currency: {_text(invoice.get('currency'), 'INR')}",
        "",
        "SUPPLIER",
        f"Legal name: {_text(supplier.get('legal_name'))}",
        f"Trade name: {_text(supplier.get('trade_name'))}",
        f"GSTIN: {_text(supplier.get('gstin'))}",
        f"Registered address: {_text(supplier.get('registered_address'))}",
        (
            "State: "
            f"{_text(supplier.get('state_name'))} "
            f"({_text(supplier.get('state_code'))})"
        ),
        f"Postal code: {_text(supplier.get('postal_code'))}",
        "",
        "RECIPIENT / TRAVELLER",
        f"Account name: {_text(passenger.get('full_name'))}",
        f"Account email: {_text(passenger.get('email'))}",
        f"Traveller name: {_text(passenger.get('traveller_name'))}",
        f"Traveller phone: {_text(passenger.get('traveller_phone'))}",
        f"Traveller email: {_text(passenger.get('traveller_email'))}",
        "",
        "SERVICE",
        f"Description: {_text(service.get('description'))}",
        f"SAC: {_text(service.get('sac_code'))}",
        f"Quantity / unit: {_text(service.get('quantity'), '1')} {_text(service.get('unit'), 'ride')}",
        (
            "Place of supply: "
            f"{_text(place_of_supply.get('name'))} "
            f"({_text(place_of_supply.get('state_code'))})"
        ),
        f"Reverse charge applicable: {'Yes' if compliance.get('reverse_charge_applicable') else 'No'}",
        "",
        "TRIP",
        f"Route: {_text(trip.get('route_name'))} [{_text(trip.get('route_code'))}]",
        f"Seat number: {_text(trip.get('seat_number'))}",
        f"Pickup: {_text(pickup.get('name'))}",
        f"Dropoff: {_text(dropoff.get('name'))}",
        f"Planned start: {_text(trip.get('planned_start_at'))}",
        f"Completed at: {_text(trip.get('completed_at'))}",
        "",
        "AMOUNT AND TAX",
        f"Total booking amount: {_money(breakdown.get('total_booking_amount'))}",
        f"Taxable value: {_money(breakdown.get('taxable_value'))}",
        (
            f"CGST ({_text(breakdown.get('cgst_rate_percent'), '0.00')}%): "
            f"{_money(breakdown.get('cgst_amount'))}"
        ),
        (
            f"SGST ({_text(breakdown.get('sgst_rate_percent'), '0.00')}%): "
            f"{_money(breakdown.get('sgst_amount'))}"
        ),
        (
            f"IGST ({_text(breakdown.get('igst_rate_percent'), '0.00')}%): "
            f"{_money(breakdown.get('igst_amount'))}"
        ),
        f"Total tax: {_money(breakdown.get('total_tax_amount'))}",
        f"Rounding adjustment: {_money(breakdown.get('rounding_adjustment'))}",
        f"GST inclusive: {'Yes' if breakdown.get('gst_inclusive') else 'No'}",
        "",
        "PAYMENT",
        f"Payment status: {_text(payment.get('status'))}",
        f"Razorpay order ID: {_text(payment.get('razorpay_order_id'))}",
        f"Razorpay payment ID: {_text(payment.get('razorpay_payment_id'))}",
        f"Paid amount: {_money(payment.get('amount'))}",
        "",
        "This document is generated electronically and is currently a preview.",
        "Digital signature, IRN and signed QR code are not presently available.",
    ]

    wrapped_lines: list[str] = []
    for line in lines:
        if not line:
            wrapped_lines.append("")
            continue
        wrapped_lines.extend(wrap(line, width=88) or [""])
    return wrapped_lines


def _pdf_escape(value: str) -> str:
    safe = value.encode("latin-1", errors="replace").decode("latin-1")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def generate_invoice_pdf(invoice: dict[str, Any]) -> bytes:
    """Generate a dependency-free, valid PDF containing the invoice details."""
    lines = _invoice_lines(invoice)
    lines_per_page = 47
    pages = [
        lines[index : index + lines_per_page]
        for index in range(0, len(lines), lines_per_page)
    ] or [["Invoice"]]

    page_count = len(pages)
    font_object_number = 3 + (page_count * 2)
    objects: dict[int, bytes] = {}
    page_object_numbers = [3 + (index * 2) for index in range(page_count)]

    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{number} 0 R" for number in page_object_numbers)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode()

    for index, page_lines in enumerate(pages):
        page_object_number = page_object_numbers[index]
        content_object_number = page_object_number + 1
        content_lines = ["BT", "/F1 10 Tf", "48 754 Td", "14 TL"]
        for line in page_lines:
            content_lines.append(f"({_pdf_escape(line)}) Tj")
            content_lines.append("T*")
        content_lines.append("ET")
        content = "\n".join(content_lines).encode("latin-1")

        objects[page_object_number] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_object_number} 0 R >> >> "
            f"/Contents {content_object_number} 0 R >>"
        ).encode()
        objects[content_object_number] = (
            f"<< /Length {len(content)} >>\nstream\n".encode()
            + content
            + b"\nendstream"
        )

    objects[font_object_number] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (font_object_number + 1)
    for object_number in range(1, font_object_number + 1):
        offsets[object_number] = len(output)
        output.extend(f"{object_number} 0 obj\n".encode())
        output.extend(objects[object_number])
        output.extend(b"\nendobj\n")

    xref_offset = len(output)
    output.extend(f"xref\n0 {font_object_number + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for object_number in range(1, font_object_number + 1):
        output.extend(f"{offsets[object_number]:010d} 00000 n \n".encode())
    output.extend(
        (
            f"trailer\n<< /Size {font_object_number + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    return bytes(output)
