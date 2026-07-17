from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


A4_WIDTH = 595.28
A4_HEIGHT = 841.89
MARGIN = 36.0
IST = timezone(timedelta(hours=5, minutes=30))

NAVY = (0.090, 0.125, 0.165)
BLUE = (0.145, 0.300, 0.380)
TEAL = (0.150, 0.390, 0.370)
INK = (0.105, 0.120, 0.140)
MUTED = (0.360, 0.390, 0.425)
LINE = (0.760, 0.780, 0.800)
PANEL = (0.975, 0.978, 0.980)
WHITE = (1.0, 1.0, 1.0)
CONTENT_BOTTOM = 68.0


def _text(value: Any, fallback: str = "N/A") -> str:
    if value is None or value == "":
        return fallback
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    return str(value)


def _money(value: Any) -> str:
    try:
        return f"INR {Decimal(str(value or 0)):.2f}"
    except Exception:
        return "INR 0.00"


def _date(value: Any) -> str:
    if value is None or value == "":
        return "N/A"
    parsed = value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if not isinstance(parsed, datetime):
        return str(parsed)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


def _pdf_escape(value: str) -> str:
    safe = value.encode("latin-1", errors="replace").decode("latin-1")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap(value: Any, width: float, font_size: float) -> list[str]:
    text = _text(value)
    max_chars = max(int(width / max(font_size * 0.52, 1)), 1)
    words = [
        chunk
        for word in text.split()
        for chunk in (
            [word]
            if len(word) <= max_chars
            else [
                word[index : index + max_chars]
                for index in range(0, len(word), max_chars)
            ]
        )
    ]
    if not words:
        return [""]

    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + len(word) + 1 <= max_chars:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _color(color: tuple[float, float, float], *, stroke: bool = False) -> str:
    operator = "RG" if stroke else "rg"
    return f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} {operator}"


class _Page:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def rectangle(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        *,
        fill: tuple[float, float, float] | None = None,
        stroke: tuple[float, float, float] | None = None,
        line_width: float = 1,
    ) -> None:
        if fill is not None:
            self.commands.append(_color(fill))
        if stroke is not None:
            self.commands.extend([_color(stroke, stroke=True), f"{line_width:.2f} w"])
        operator = "B" if fill is not None and stroke is not None else "f" if fill else "S"
        self.commands.append(
            f"{x:.2f} {y:.2f} {width:.2f} {height:.2f} re {operator}"
        )

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        *,
        color: tuple[float, float, float] = LINE,
        line_width: float = 1,
    ) -> None:
        self.commands.extend(
            [
                _color(color, stroke=True),
                f"{line_width:.2f} w",
                f"{x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S",
            ]
        )

    def text(
        self,
        x: float,
        y: float,
        value: Any,
        *,
        size: float = 9,
        bold: bool = False,
        color: tuple[float, float, float] = INK,
    ) -> None:
        font = "/F2" if bold else "/F1"
        self.commands.extend(
            [
                "BT",
                _color(color),
                f"{font} {size:.2f} Tf",
                f"1 0 0 1 {x:.2f} {y:.2f} Tm",
                f"({_pdf_escape(_text(value, ''))}) Tj",
                "ET",
            ]
        )

    def right_text(
        self,
        right_x: float,
        y: float,
        value: Any,
        *,
        size: float = 9,
        bold: bool = False,
        color: tuple[float, float, float] = INK,
    ) -> None:
        rendered = _text(value, "")
        approximate_width = len(rendered) * size * 0.52
        self.text(
            right_x - approximate_width,
            y,
            rendered,
            size=size,
            bold=bold,
            color=color,
        )

    def wrapped_text(
        self,
        x: float,
        y: float,
        value: Any,
        width: float,
        *,
        size: float = 9,
        bold: bool = False,
        color: tuple[float, float, float] = INK,
        leading: float | None = None,
    ) -> float:
        line_height = leading or size * 1.3
        lines = _wrap(value, width, size)
        for index, line in enumerate(lines):
            self.text(
                x,
                y - (index * line_height),
                line,
                size=size,
                bold=bold,
                color=color,
            )
        return len(lines) * line_height


def _label_value(
    page: _Page,
    x: float,
    y: float,
    label: str,
    value: Any,
    width: float,
) -> float:
    page.text(x, y, label.upper(), size=6.8, bold=True, color=MUTED)
    used = page.wrapped_text(x, y - 13, value, width, size=8.7, leading=11)
    return 15 + used


def _section_title(page: _Page, y: float, title: str) -> None:
    page.text(MARGIN, y, title.upper(), size=8.2, bold=True, color=NAVY)
    page.line(MARGIN, y - 7, A4_WIDTH - MARGIN, y - 7, color=TEAL, line_width=1.2)


def _party_panel_height(
    width: float,
    fields: list[tuple[str, Any]],
) -> float:
    inner_width = width - 24
    field_lines = [
        _wrap(value, inner_width, 8.3) for _, value in fields
    ]
    # Title area + every label/value group + reliable bottom padding.
    return max(40 + sum(12 + (len(lines) * 10) for lines in field_lines) + 10, 112)


def _party_panel(
    page: _Page,
    *,
    x: float,
    y: float,
    width: float,
    title: str,
    fields: list[tuple[str, Any]],
) -> float:
    inner_width = width - 24
    field_lines = [
        (label, _wrap(value, inner_width, 8.3)) for label, value in fields
    ]
    height = _party_panel_height(width, fields)
    page.rectangle(x, y - height, width, height, fill=WHITE, stroke=LINE)
    page.rectangle(x, y - 29, width, 29, fill=PANEL)
    page.text(x + 12, y - 19, title.upper(), size=7.5, bold=True, color=BLUE)
    cursor = y - 44
    for label, lines in field_lines:
        page.text(x + 12, cursor, label.upper(), size=6.3, bold=True, color=MUTED)
        cursor -= 11
        for line in lines:
            page.text(x + 12, cursor, line, size=8.1, color=INK)
            cursor -= 10
        cursor -= 1
    return height


def _draw_header(page: _Page, invoice: dict[str, Any]) -> float:
    supplier = invoice.get("supplier") or {}
    supplier_name = (
        supplier.get("trade_name")
        or supplier.get("legal_name")
        or "Shuttle Service"
    )
    page.wrapped_text(
        MARGIN,
        A4_HEIGHT - 35,
        supplier_name,
        330,
        size=12,
        bold=True,
        color=NAVY,
        leading=14,
    )
    page.text(
        MARGIN,
        A4_HEIGHT - 55,
        f"GSTIN  {_text(supplier.get('gstin'))}",
        size=7.2,
        color=MUTED,
    )

    page.right_text(
        A4_WIDTH - MARGIN,
        A4_HEIGHT - 35,
        "GST INVOICE",
        size=15,
        bold=True,
        color=NAVY,
    )
    page.right_text(
        A4_WIDTH - MARGIN,
        A4_HEIGHT - 53,
        f"{_text(invoice.get('invoice_status'), 'preview').upper()} PAYMENT RECEIPT",
        size=7,
        bold=True,
        color=MUTED,
    )
    page.line(
        MARGIN,
        A4_HEIGHT - 70,
        A4_WIDTH - MARGIN,
        A4_HEIGHT - 70,
        color=TEAL,
        line_width=1.5,
    )
    return A4_HEIGHT - 90


def _draw_meta(page: _Page, invoice: dict[str, Any], y: float) -> float:
    width = A4_WIDTH - (2 * MARGIN)
    page.rectangle(MARGIN, y - 58, width, 58, fill=WHITE, stroke=LINE)
    column_width = width / 4
    items = [
        ("Invoice number", invoice.get("invoice_number")),
        ("Issue date", _date(invoice.get("invoice_generated_at"))),
        ("Booking ID", invoice.get("booking_id")),
        ("Currency", invoice.get("currency") or "INR"),
    ]
    for index, (label, value) in enumerate(items):
        x = MARGIN + (index * column_width)
        if index:
            page.line(x, y - 48, x, y - 10, color=LINE)
        _label_value(page, x + 11, y - 18, label, value, column_width - 22)
    return y - 78


def _party_fields(
    invoice: dict[str, Any],
) -> tuple[list[tuple[str, Any]], list[tuple[str, Any]]]:
    supplier = invoice.get("supplier") or {}
    passenger = invoice.get("passenger") or {}
    supplier_fields = [
        ("Legal name", supplier.get("legal_name")),
        ("Registered address", supplier.get("registered_address")),
        (
            "State / postal code",
            f"{_text(supplier.get('state_name'))} "
            f"({_text(supplier.get('state_code'))}) - "
            f"{_text(supplier.get('postal_code'))}",
        ),
    ]
    traveller_name = passenger.get("traveller_name") or passenger.get("full_name")
    traveller_email = passenger.get("traveller_email") or passenger.get("email")
    recipient_fields = [
        ("Name", traveller_name),
        ("Email", traveller_email),
        ("Phone", passenger.get("traveller_phone")),
        ("Relationship", passenger.get("traveller_relationship_label")),
    ]
    account_name = passenger.get("full_name")
    account_email = passenger.get("email")
    if account_name and account_name != traveller_name:
        recipient_fields.append(("Booked by", account_name))
    if account_email and account_email != traveller_email:
        recipient_fields.append(("Account email", account_email))
    return supplier_fields, recipient_fields


def _parties_required_height(invoice: dict[str, Any]) -> float:
    gap = 14
    width = (A4_WIDTH - (2 * MARGIN) - gap) / 2
    supplier_fields, recipient_fields = _party_fields(invoice)
    return max(
        _party_panel_height(width, supplier_fields),
        _party_panel_height(width, recipient_fields),
    ) + 20


def _draw_parties(page: _Page, invoice: dict[str, Any], y: float) -> float:
    gap = 14
    width = (A4_WIDTH - (2 * MARGIN) - gap) / 2
    supplier_fields, recipient_fields = _party_fields(invoice)
    left_height = _party_panel(
        page,
        x=MARGIN,
        y=y,
        width=width,
        title="Supplier",
        fields=supplier_fields,
    )
    right_height = _party_panel(
        page,
        x=MARGIN + width + gap,
        y=y,
        width=width,
        title="Bill to / traveller",
        fields=recipient_fields,
    )
    return y - max(left_height, right_height) - 20


def _draw_journey(page: _Page, invoice: dict[str, Any], y: float) -> float:
    trip = invoice.get("trip") or {}
    service = invoice.get("service") or {}
    place = invoice.get("place_of_supply") or {}
    pickup = trip.get("pickup_stop") or {}
    dropoff = trip.get("dropoff_stop") or {}
    width = A4_WIDTH - (2 * MARGIN)
    height = 100
    _section_title(page, y, "Journey and service")
    y -= 19
    page.rectangle(MARGIN, y - height, width, height, fill=WHITE, stroke=LINE)

    page.text(MARGIN + 14, y - 22, "FROM", size=6.5, bold=True, color=MUTED)
    page.wrapped_text(MARGIN + 14, y - 38, pickup.get("name"), 140, size=10, bold=True)
    page.text(MARGIN + 183, y - 22, "TO", size=6.5, bold=True, color=MUTED)
    page.wrapped_text(MARGIN + 183, y - 38, dropoff.get("name"), 135, size=10, bold=True)
    page.text(MARGIN + 348, y - 22, "ROUTE / SEAT", size=6.5, bold=True, color=MUTED)
    route = f"{_text(trip.get('route_name'))} [{_text(trip.get('route_code'))}]"
    page.wrapped_text(MARGIN + 348, y - 38, route, 145, size=9, bold=True)
    page.text(MARGIN + 348, y - 64, f"Seat {_text(trip.get('seat_number'))}", size=8.5)

    page.line(MARGIN + 14, y - 67, MARGIN + width - 14, y - 67, color=LINE)
    service_line = (
        f"{_text(service.get('description'))}  |  SAC {_text(service.get('sac_code'))}  |  "
        f"Place of supply: {_text(place.get('name'))} ({_text(place.get('state_code'))})"
    )
    page.wrapped_text(MARGIN + 14, y - 82, service_line, 315, size=7.6, color=MUTED)
    page.text(MARGIN + 348, y - 91, _date(trip.get("planned_start_at")), size=7.2, color=MUTED)
    return y - height - 20


def _draw_tax_table(page: _Page, invoice: dict[str, Any], y: float) -> float:
    breakdown = invoice.get("breakdown") or {}
    compliance = invoice.get("compliance") or {}
    width = A4_WIDTH - (2 * MARGIN)
    _section_title(page, y, "Fare and tax summary")
    y -= 18
    header_height = 24
    row_height = 22
    rows = [
        ("Taxable fare", "-", _money(breakdown.get("taxable_value"))),
        (
            "Central GST (CGST)",
            f"{_text(breakdown.get('cgst_rate_percent'), '0.00')}%",
            _money(breakdown.get("cgst_amount")),
        ),
        (
            "State GST (SGST)",
            f"{_text(breakdown.get('sgst_rate_percent'), '0.00')}%",
            _money(breakdown.get("sgst_amount")),
        ),
        # (
        #     "Integrated GST (IGST)",
        #     f"{_text(breakdown.get('igst_rate_percent'), '0.00')}%",
        #     _money(breakdown.get("igst_amount")),
        # ),
    ]
    total_height = header_height + (len(rows) * row_height) + 38
    page.rectangle(MARGIN, y - total_height, width, total_height, fill=WHITE, stroke=LINE)
    page.rectangle(MARGIN, y - header_height, width, header_height, fill=PANEL)
    page.text(MARGIN + 12, y - 16, "DESCRIPTION", size=7, bold=True, color=NAVY)
    page.text(MARGIN + 335, y - 16, "RATE", size=7, bold=True, color=NAVY)
    page.right_text(
        MARGIN + width - 12,
        y - 16,
        "AMOUNT",
        size=7,
        bold=True,
        color=NAVY,
    )

    cursor = y - header_height
    for description, rate, amount in rows:
        cursor -= row_height
        page.text(MARGIN + 12, cursor + 7, description, size=8.2)
        page.text(MARGIN + 335, cursor + 7, rate, size=8.2, color=MUTED)
        page.right_text(
            MARGIN + width - 12,
            cursor + 7,
            amount,
            size=8.2,
        )
        page.line(MARGIN, cursor, MARGIN + width, cursor, color=LINE, line_width=0.6)

    page.line(
        MARGIN,
        y - total_height + 38,
        MARGIN + width,
        y - total_height + 38,
        color=LINE,
    )
    page.text(MARGIN + 12, y - total_height + 14, "GST INCLUSIVE", size=7, bold=True, color=MUTED)
    page.text(
        MARGIN + 100,
        y - total_height + 14,
        "YES" if breakdown.get("gst_inclusive") else "NO",
        size=8,
        bold=True,
        color=TEAL,
    )
    page.text(MARGIN + 315, y - total_height + 14, "TOTAL PAID", size=8, bold=True, color=NAVY)
    page.right_text(
        MARGIN + width - 12,
        y - total_height + 12,
        _money(breakdown.get("total_booking_amount")),
        size=12,
        bold=True,
        color=NAVY,
    )
    reverse_charge = "YES" if compliance.get("reverse_charge_applicable") else "NO"
    page.text(MARGIN + 150, y - total_height + 14, f"REVERSE CHARGE: {reverse_charge}", size=7, color=MUTED)
    return y - total_height - 20


def _draw_payment(page: _Page, invoice: dict[str, Any], y: float) -> float:
    payment = invoice.get("payment") or {}
    width = A4_WIDTH - (2 * MARGIN)
    _section_title(page, y, "Payment confirmation")
    y -= 18
    page.rectangle(MARGIN, y - 54, width, 54, fill=WHITE, stroke=LINE)
    items = [
        ("Status", _text(payment.get("status")).upper()),
        ("Razorpay order ID", payment.get("razorpay_order_id")),
        ("Razorpay payment ID", payment.get("razorpay_payment_id")),
        ("Paid amount", _money(payment.get("amount"))),
    ]
    column_width = width / 4
    for index, (label, value) in enumerate(items):
        x = MARGIN + (index * column_width)
        _label_value(page, x + 10, y - 16, label, value, column_width - 20)
    return y - 68


def _draw_footer(
    page: _Page,
    invoice: dict[str, Any],
    *,
    page_number: int,
    page_count: int,
) -> None:
    page.line(MARGIN, 50, A4_WIDTH - MARGIN, 50, color=LINE, line_width=0.7)
    page.text(
        MARGIN,
        36,
        "System-generated payment receipt. No physical signature is required.",
        size=6.4,
        color=MUTED,
    )
    page.text(
        MARGIN,
        24,
        "Digital signature, IRN and signed QR code are not presently available.",
        size=6.4,
        color=MUTED,
    )
    page.right_text(
        A4_WIDTH - MARGIN,
        36,
        f"Invoice {_text(invoice.get('invoice_number'))}",
        size=6.4,
        color=MUTED,
    )
    page.right_text(
        A4_WIDTH - MARGIN,
        24,
        f"Page {page_number} of {page_count}",
        size=6.4,
        color=MUTED,
    )


def _new_page(invoice: dict[str, Any]) -> tuple[_Page, float]:
    page = _Page()
    return page, _draw_header(page, invoice)


def _ensure_space(
    pages: list[_Page],
    invoice: dict[str, Any],
    y: float,
    required_height: float,
) -> tuple[_Page, float]:
    if y - required_height >= CONTENT_BOTTOM:
        return pages[-1], y

    page, y = _new_page(invoice)
    pages.append(page)
    return page, y


def _build_pages(invoice: dict[str, Any]) -> list[_Page]:
    page, y = _new_page(invoice)
    pages = [page]

    page, y = _ensure_space(pages, invoice, y, 78)
    y = _draw_meta(page, invoice, y)

    party_height = _parties_required_height(invoice)
    page, y = _ensure_space(pages, invoice, y, party_height)
    y = _draw_parties(page, invoice, y)

    page, y = _ensure_space(pages, invoice, y, 140)
    y = _draw_journey(page, invoice, y)

    page, y = _ensure_space(pages, invoice, y, 184)
    y = _draw_tax_table(page, invoice, y)

    page, y = _ensure_space(pages, invoice, y, 90)
    _draw_payment(page, invoice, y)

    page_count = len(pages)
    for index, rendered_page in enumerate(pages, start=1):
        _draw_footer(
            rendered_page,
            invoice,
            page_number=index,
            page_count=page_count,
        )
    return pages


def generate_invoice_pdf(invoice: dict[str, Any]) -> bytes:
    """Generate a dependency-free professional A4 invoice PDF."""
    pages = _build_pages(invoice)
    page_count = len(pages)
    regular_font_object = 3 + (page_count * 2)
    bold_font_object = regular_font_object + 1
    objects: dict[int, bytes] = {}
    page_object_numbers = [3 + (index * 2) for index in range(page_count)]

    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{number} 0 R" for number in page_object_numbers)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode()

    for index, page in enumerate(pages):
        page_object_number = page_object_numbers[index]
        content_object_number = page_object_number + 1
        content = "\n".join(page.commands).encode("latin-1")
        objects[page_object_number] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 "
            f"{A4_WIDTH:.2f} {A4_HEIGHT:.2f}] "
            "/Resources << /Font << "
            f"/F1 {regular_font_object} 0 R /F2 {bold_font_object} 0 R"
            " >> >> "
            f"/Contents {content_object_number} 0 R >>"
        ).encode()
        objects[content_object_number] = (
            f"<< /Length {len(content)} >>\nstream\n".encode()
            + content
            + b"\nendstream"
        )

    objects[regular_font_object] = (
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    )
    objects[bold_font_object] = (
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"
    )

    last_object = bold_font_object
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (last_object + 1)
    for object_number in range(1, last_object + 1):
        offsets[object_number] = len(output)
        output.extend(f"{object_number} 0 obj\n".encode())
        output.extend(objects[object_number])
        output.extend(b"\nendobj\n")

    xref_offset = len(output)
    output.extend(f"xref\n0 {last_object + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for object_number in range(1, last_object + 1):
        output.extend(f"{offsets[object_number]:010d} 00000 n \n".encode())
    output.extend(
        (
            f"trailer\n<< /Size {last_object + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    return bytes(output)
