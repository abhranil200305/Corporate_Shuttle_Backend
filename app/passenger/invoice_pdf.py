from __future__ import annotations

from datetime import datetime
from decimal import Decimal
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


CONTENT_WIDTH = 88
LABEL_WIDTH = 25
VALUE_WIDTH = CONTENT_WIDTH - LABEL_WIDTH - 5


def _rule(character: str = "-") -> str:
    return character * CONTENT_WIDTH


def _section(title: str) -> list[str]:
    return ["", _rule("-"), f"  {title.upper()}", _rule("-")]


def _wrap_words(value: str, width: int) -> list[str]:
    words = [
        chunk
        for word in value.split()
        for chunk in (
            [word]
            if len(word) <= width
            else [word[index : index + width] for index in range(0, len(word), width)]
        )
    ]
    if not words:
        return [""]

    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + len(word) + 1 <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _field(label: str, value: Any, fallback: str = "N/A") -> list[str]:
    value_lines = _wrap_words(_text(value, fallback), VALUE_WIDTH)
    prefix = f"  {label:<{LABEL_WIDTH}} "
    continuation = " " * len(prefix)
    return [
        f"{prefix if index == 0 else continuation}{line}"
        for index, line in enumerate(value_lines)
    ]


def _named_code(name: Any, code: Any, *, brackets: str = "parentheses") -> str:
    rendered_name = _text(name)
    rendered_code = _text(code, "")
    if not rendered_code:
        return rendered_name
    if brackets == "square":
        return f"{rendered_name} [{rendered_code}]"
    return f"{rendered_name} ({rendered_code})"


def _tax_table_row(label: str, amount: str) -> str:
    return f"  | {label:<36} | {amount:>20} |"


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

    status = _text(invoice.get("invoice_status"), "preview").upper()
    currency = _text(invoice.get("currency"), "INR")
    lines = [
        _rule("="),
        "GST INVOICE / PAYMENT RECEIPT".center(CONTENT_WIDTH),
        _rule("="),
        f"STATUS: {status}".center(CONTENT_WIDTH),
    ]

    lines.extend(_section("Invoice information"))
    lines.extend(_field("Invoice number", invoice.get("invoice_number")))
    lines.extend(_field("Generated at", invoice.get("invoice_generated_at")))
    lines.extend(_field("Booking ID", invoice.get("booking_id")))
    lines.extend(_field("Booking created", invoice.get("booking_created_at")))
    lines.extend(_field("Currency", currency))

    lines.extend(_section("Supplier"))
    lines.extend(_field("Legal name", supplier.get("legal_name")))
    lines.extend(_field("Trade name", supplier.get("trade_name")))
    lines.extend(_field("GSTIN", supplier.get("gstin")))
    lines.extend(_field("Registered address", supplier.get("registered_address")))
    lines.extend(
        _field(
            "State",
            _named_code(supplier.get("state_name"), supplier.get("state_code")),
        )
    )
    lines.extend(_field("Postal code", supplier.get("postal_code")))

    lines.extend(_section("Recipient / traveller"))
    lines.extend(_field("Account name", passenger.get("full_name")))
    lines.extend(_field("Account email", passenger.get("email")))
    lines.extend(_field("Traveller name", passenger.get("traveller_name")))
    lines.extend(_field("Traveller phone", passenger.get("traveller_phone")))
    lines.extend(_field("Traveller email", passenger.get("traveller_email")))
    lines.extend(
        _field(
            "Relationship",
            passenger.get("traveller_relationship_label"),
        )
    )

    lines.extend(_section("Service details"))
    lines.extend(_field("Description", service.get("description")))
    lines.extend(_field("SAC code", service.get("sac_code")))
    lines.extend(
        _field(
            "Quantity / unit",
            f"{_text(service.get('quantity'), '1')} "
            f"{_text(service.get('unit'), 'ride')}",
        )
    )
    lines.extend(
        _field(
            "Place of supply",
            _named_code(
                place_of_supply.get("name"),
                place_of_supply.get("state_code"),
            ),
        )
    )
    lines.extend(
        _field(
            "Reverse charge",
            "Yes" if compliance.get("reverse_charge_applicable") else "No",
        )
    )

    lines.extend(_section("Trip details"))
    lines.extend(
        _field(
            "Route",
            _named_code(
                trip.get("route_name"),
                trip.get("route_code"),
                brackets="square",
            ),
        )
    )
    lines.extend(_field("Seat number", trip.get("seat_number")))
    lines.extend(_field("Pickup", pickup.get("name")))
    lines.extend(_field("Dropoff", dropoff.get("name")))
    lines.extend(_field("Planned start", trip.get("planned_start_at")))
    lines.extend(_field("Completed at", trip.get("completed_at")))

    lines.extend(_section("Amount and tax"))
    lines.extend(
        _field("Total booking amount", _money(breakdown.get("total_booking_amount")))
    )
    lines.extend(_field("Taxable value", _money(breakdown.get("taxable_value"))))
    lines.extend(
        [
            "",
            "  +--------------------------------------+----------------------+",
            "  | TAX COMPONENT                        | AMOUNT               |",
            "  +--------------------------------------+----------------------+",
            _tax_table_row(
                f"CGST ({_text(breakdown.get('cgst_rate_percent'), '0.00')}%)",
                _money(breakdown.get("cgst_amount")),
            ),
            _tax_table_row(
                f"SGST ({_text(breakdown.get('sgst_rate_percent'), '0.00')}%)",
                _money(breakdown.get("sgst_amount")),
            ),
            _tax_table_row(
                f"IGST ({_text(breakdown.get('igst_rate_percent'), '0.00')}%)",
                _money(breakdown.get("igst_amount")),
            ),
            "  +--------------------------------------+----------------------+",
            _tax_table_row(
                "TOTAL TAX",
                _money(breakdown.get("total_tax_amount")),
            ),
            "  +--------------------------------------+----------------------+",
        ]
    )
    lines.extend(
        _field(
            "Rounding adjustment",
            _money(breakdown.get("rounding_adjustment")),
        )
    )
    lines.extend(
        _field(
            "GST inclusive",
            "Yes" if breakdown.get("gst_inclusive") else "No",
        )
    )

    lines.extend(_section("Payment details"))
    lines.extend(_field("Payment status", payment.get("status")))
    lines.extend(_field("Razorpay order ID", payment.get("razorpay_order_id")))
    lines.extend(_field("Razorpay payment ID", payment.get("razorpay_payment_id")))
    lines.extend(_field("Paid amount", _money(payment.get("amount"))))

    lines.extend(
        [
            "",
            _rule("="),
            "This document is generated electronically and is currently a preview.",
            "Digital signature, IRN and signed QR code are not presently available.",
            "This is a system-generated invoice and requires no physical signature.",
        ]
    )
    return lines


def _pdf_escape(value: str) -> str:
    safe = value.encode("latin-1", errors="replace").decode("latin-1")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _paginate_lines(lines: list[str], lines_per_page: int) -> list[list[str]]:
    pages: list[list[str]] = []
    cursor = 0
    section_rule = _rule("-")

    while cursor < len(lines):
        end = min(cursor + lines_per_page, len(lines))
        if end < len(lines):
            # Keep a section's blank spacer, rule, title, and lower rule from
            # being orphaned at the bottom of a page.
            for candidate in range(max(cursor + 1, end - 4), end):
                if (
                    lines[candidate] == ""
                    and candidate + 3 < len(lines)
                    and lines[candidate + 1] == section_rule
                    and lines[candidate + 3] == section_rule
                ):
                    end = candidate
                    break

        pages.append(lines[cursor:end])
        cursor = end
        while cursor < len(lines) and lines[cursor] == "":
            cursor += 1

    return pages or [["Invoice"]]


def generate_invoice_pdf(invoice: dict[str, Any]) -> bytes:
    """Generate a dependency-free, valid PDF containing the invoice details."""
    lines = _invoice_lines(invoice)
    lines_per_page = 45
    pages = _paginate_lines(lines, lines_per_page)

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
        content_lines = ["BT", "/F1 9 Tf", "40 770 Td", "14 TL"]
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
