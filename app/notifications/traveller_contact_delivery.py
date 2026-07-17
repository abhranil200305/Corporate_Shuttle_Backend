from __future__ import annotations

import asyncio
import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import html
import json
from email.message import EmailMessage
from typing import Any, Sequence
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.schemas import MailAttachmentSchema
from app.db.database import AsyncSessionLocal
from app.db.schema import (
    BookingSession,
    Route,
    RouteStop,
    ScheduledTrip,
    TripBooking,
    TravellerContactNotification,
    TravellerContactNotificationStatus,
    User,
)
from app.passenger.booking_qr import generate_booking_qr_png
from app.passenger.invoice_pdf import generate_invoice_pdf
from app.passenger.service import PassengerService

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SMTPConfig:
    host: str
    port: int
    username: str | None
    password: str | None
    from_email: str
    from_name: str
    use_tls: bool
    use_ssl: bool
    timeout_seconds: float


class SMTPEmailSender:
    @staticmethod
    def _truthy_env(name: str, default: str = "") -> bool:
        return os.getenv(name, default).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @classmethod
    def load_config(cls) -> SMTPConfig | None:
        host = os.getenv("SMTP_HOST", "").strip()
        from_email = (
            os.getenv("SMTP_FROM_EMAIL", "").strip()
            or os.getenv("MAIL_FROM_EMAIL", "").strip()
        )

        if not host or not from_email:
            return None

        raw_port = os.getenv("SMTP_PORT", "").strip()
        if raw_port:
            try:
                port = int(raw_port)
            except ValueError:
                port = 465 if cls._truthy_env("SMTP_USE_SSL") else 587
        else:
            port = 465 if cls._truthy_env("SMTP_USE_SSL") else 587

        raw_timeout = os.getenv("SMTP_TIMEOUT_SECONDS", "20").strip()
        try:
            timeout_seconds = float(raw_timeout)
        except ValueError:
            timeout_seconds = 20.0

        return SMTPConfig(
            host=host,
            port=port,
            username=os.getenv("SMTP_USERNAME", "").strip() or None,
            password=os.getenv("SMTP_PASSWORD", "").strip() or None,
            from_email=from_email,
            from_name=os.getenv("SMTP_FROM_NAME", "Shuttle").strip() or "Shuttle",
            use_tls=cls._truthy_env("SMTP_USE_TLS", "true"),
            use_ssl=cls._truthy_env("SMTP_USE_SSL", "false"),
            timeout_seconds=timeout_seconds,
        )

    @classmethod
    def is_configured(cls) -> bool:
        return cls.load_config() is not None

    async def send_email(
        self,
        *,
        to_email: str,
        subject: str,
        body: str,
        html_body: str | None = None,
        attachments: Sequence[MailAttachmentSchema] | None = None,
    ) -> str:
        config = self.load_config()
        if config is None:
            raise RuntimeError(
                "SMTP is not configured. Set SMTP_HOST and SMTP_FROM_EMAIL."
            )

        recipient = (to_email or "").strip()
        if not recipient:
            raise RuntimeError("Recipient email is empty.")

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = f"{config.from_name} <{config.from_email}>"
        message["To"] = recipient

        # Plain-text fallback for clients that block HTML.
        message.set_content(body)

        # Rich HTML version for normal email clients.
        if html_body:
            message.add_alternative(html_body, subtype="html")

        for attachment in attachments or ():
            content_type = attachment.content_type or "application/octet-stream"
            if "/" in content_type:
                maintype, subtype = content_type.split("/", 1)
            else:
                maintype, subtype = "application", "octet-stream"

            if (
                attachment.inline
                and attachment.content_id
                and html_body
            ):
                html_part = message.get_payload()[-1]
                content_id = attachment.content_id.strip("<>")
                html_part.add_related(
                    attachment.content,
                    maintype=maintype,
                    subtype=subtype,
                    cid=f"<{content_id}>",
                    disposition="inline",
                    filename=attachment.filename,
                )
                continue

            message.add_attachment(
                attachment.content,
                maintype=maintype,
                subtype=subtype,
                filename=attachment.filename,
            )

        provider_message_id = f"email:{uuid4().hex}"

        await asyncio.to_thread(
            self._send_email_sync,
            config,
            message,
        )

        return provider_message_id

    @staticmethod
    def _send_email_sync(
        config: SMTPConfig,
        message: EmailMessage,
    ) -> None:
        if config.use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                config.host,
                config.port,
                timeout=config.timeout_seconds,
                context=context,
            ) as smtp:
                if config.username and config.password:
                    smtp.login(config.username, config.password)
                smtp.send_message(message)
            return

        with smtplib.SMTP(
            config.host,
            config.port,
            timeout=config.timeout_seconds,
        ) as smtp:
            smtp.ehlo()
            if config.use_tls:
                context = ssl.create_default_context()
                smtp.starttls(context=context)
                smtp.ehlo()
            if config.username and config.password:
                smtp.login(config.username, config.password)
            smtp.send_message(message)


class TravellerContactDeliveryService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        email_sender: SMTPEmailSender | None = None,
    ) -> None:
        self.db = db
        self.email_sender = email_sender or SMTPEmailSender()

    @staticmethod
    def _get_batch_size() -> int:
        raw_value = os.getenv("TRAVELLER_CONTACT_DELIVERY_BATCH_SIZE", "25").strip()
        try:
            value = int(raw_value)
        except ValueError:
            value = 25
        return min(max(value, 1), 100)

    @staticmethod
    def _get_retry_delay_seconds(attempt_count: int) -> int:
        # 1m, 2m, 4m, 8m, 16m, capped at 1h
        exponent = min(max(attempt_count - 1, 0), 6)
        return min(60 * (2**exponent), 3600)

    async def process_pending_batch(self) -> int:
        now = utcnow()

        stmt = (
            select(TravellerContactNotification)
            .where(
                TravellerContactNotification.status.in_(
                    [
                        TravellerContactNotificationStatus.PENDING,
                        TravellerContactNotificationStatus.FAILED,
                    ]
                ),
                or_(
                    TravellerContactNotification.delivery_retry_after.is_(None),
                    TravellerContactNotification.delivery_retry_after <= now,
                ),
            )
            .order_by(TravellerContactNotification.created_at.asc())
            .limit(self._get_batch_size())
            .with_for_update(skip_locked=True)
        )

        result = await self.db.execute(stmt)
        notifications = list(result.scalars().all())

        processed_count = 0

        for notification in notifications:
            await self._process_one(notification)
            processed_count += 1

        await self.db.commit()
        return processed_count

    async def _process_one(
        self,
        notification: TravellerContactNotification,
    ) -> None:
        channel = (notification.channel or "").strip().lower()

        notification.delivery_attempt_count = (
            int(notification.delivery_attempt_count or 0) + 1
        )

        if channel == "sms":
            await self._mark_skipped(
                notification,
                delivered_channel=None,
                reason="SMS provider is not configured yet. Delivery intentionally skipped.",
            )
            return

        if channel == "email":
            await self._process_email(notification)
            return

        if channel == "none":
            await self._mark_skipped(
                notification,
                delivered_channel=None,
                reason="No traveller phone or email snapshot is available.",
            )
            return

        await self._mark_skipped(
            notification,
            delivered_channel=None,
            reason=f"Unsupported traveller contact channel: {channel or 'empty'}.",
        )

    @staticmethod
    def _parse_message_lines(message: str) -> dict[str, str]:
        parsed: dict[str, str] = {
            "intro": "",
            "route": "",
            "pickup": "",
            "drop": "",
            "seat": "",
            "otp": "",
            "vehicle": "",
            "footer": "",
        }

        lines = [
            line.strip()
            for line in (message or "").splitlines()
            if line.strip()
        ]

        if lines:
            parsed["intro"] = lines[0]

        for line in lines[1:]:
            lowered = line.lower()

            if lowered.startswith("route:"):
                parsed["route"] = line.split(":", 1)[1].strip()
            elif lowered.startswith("pickup:"):
                parsed["pickup"] = line.split(":", 1)[1].strip()
            elif lowered.startswith("drop:"):
                parsed["drop"] = line.split(":", 1)[1].strip()
            elif lowered.startswith("seat:"):
                parsed["seat"] = line.split(":", 1)[1].strip()
            elif lowered.startswith("boarding otp:"):
                parsed["otp"] = line.split(":", 1)[1].strip()
            elif lowered.startswith("vehicle:"):
                parsed["vehicle"] = line.split(":", 1)[1].strip()
            else:
                parsed["footer"] = line

        return parsed

    @classmethod
    def _build_traveller_email_html(
        cls,
        notification: TravellerContactNotification,
    ) -> str:
        parsed = cls._parse_message_lines(notification.message or "")

        intro = html.escape(parsed["intro"] or "Your shuttle booking has been updated.")
        route = html.escape(parsed["route"] or "Your shuttle route")
        pickup = html.escape(parsed["pickup"] or "TBA")
        drop = html.escape(parsed["drop"] or "TBA")
        seat = html.escape(parsed["seat"] or "TBA")
        vehicle = html.escape(parsed["vehicle"] or "TBA")
        otp = html.escape(parsed["otp"])
        footer = html.escape(
            parsed["footer"]
            or "For changes or cancellation, contact the person who booked this ride."
        )

        otp_block = ""
        if otp:
            otp_block = f"""
              <tr>
                <td style="padding: 18px 0 4px;">
                  <div style="font-size: 13px; color: #64748b; margin-bottom: 8px;">
                    Boarding OTP
                  </div>
                  <div style="
                    display: inline-block;
                    letter-spacing: 6px;
                    font-size: 32px;
                    font-weight: 800;
                    color: #0f172a;
                    background: #f8fafc;
                    border: 1px solid #e2e8f0;
                    border-radius: 14px;
                    padding: 16px 22px;
                  ">
                    {otp}
                  </div>
                  <div style="font-size: 12px; color: #64748b; margin-top: 8px;">
                    Show this OTP to the driver while boarding.
                  </div>
                </td>
              </tr>
            """

        confirmation_assets_block = ""
        if notification.event_type == "traveller_seat_confirmed":
            confirmation_assets_block = """
                <div style="margin-top: 18px; padding: 18px; border: 1px solid #cbd5e1; border-radius: 10px; color:#334155; font-size: 13px; line-height: 19px; text-align:center;">
                  <div style="font-size:16px; font-weight:700; color:#0f172a; margin-bottom:10px;">
                    Boarding QR
                  </div>
                  <img src="cid:traveller-booking-qr" alt="Boarding QR code" width="240" height="240" style="display:block; width:240px; height:240px; max-width:100%; margin:0 auto; background:#ffffff;" />
                  <div style="margin-top:10px;">
                    Show this QR to the driver while boarding. The PNG is also attached in case your email app blocks inline images.
                  </div>
                  <div style="margin-top:8px;">
                    Your GST invoice/payment receipt for this seat is attached as a PDF.
                  </div>
                </div>
            """

        return f"""<!doctype html>
<html>
  <body style="margin:0; padding:0; background:#f1f5f9; font-family: Arial, Helvetica, sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9; padding: 28px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width: 560px; background:#ffffff; border-radius: 18px; overflow:hidden; border:1px solid #e2e8f0;">
            <tr>
              <td style="background:#0f172a; padding: 24px 28px;">
                <div style="font-size: 13px; color:#cbd5e1; text-transform: uppercase; letter-spacing: 1px;">
                  Shuttle Booking
                </div>
                <div style="font-size: 24px; line-height: 32px; color:#ffffff; font-weight: 800; margin-top: 6px;">
                  Seat confirmed
                </div>
              </td>
            </tr>

            <tr>
              <td style="padding: 26px 28px;">
                <div style="font-size: 17px; line-height: 26px; color:#0f172a; font-weight: 700;">
                  {intro}
                </div>

                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top: 22px;">
                  <tr>
                    <td style="padding: 12px 0; border-top:1px solid #e2e8f0;">
                      <div style="font-size: 12px; color:#64748b;">Route</div>
                      <div style="font-size: 15px; color:#0f172a; font-weight: 700;">{route}</div>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding: 12px 0; border-top:1px solid #e2e8f0;">
                      <div style="font-size: 12px; color:#64748b;">Pickup</div>
                      <div style="font-size: 15px; color:#0f172a;">{pickup}</div>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding: 12px 0; border-top:1px solid #e2e8f0;">
                      <div style="font-size: 12px; color:#64748b;">Drop</div>
                      <div style="font-size: 15px; color:#0f172a;">{drop}</div>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding: 12px 0; border-top:1px solid #e2e8f0;">
                      <div style="font-size: 12px; color:#64748b;">Seat</div>
                      <div style="font-size: 15px; color:#0f172a; font-weight: 700;">{seat}</div>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding: 12px 0; border-top:1px solid #e2e8f0;">
                      <div style="font-size: 12px; color:#64748b;">Vehicle</div>
                      <div style="font-size: 15px; color:#0f172a;">{vehicle}</div>
                    </td>
                  </tr>
                  {otp_block}
                </table>

                <div style="margin-top: 24px; padding: 14px 16px; background:#f8fafc; border-radius: 12px; color:#475569; font-size: 13px; line-height: 20px;">
                  {footer}
                </div>
                {confirmation_assets_block}
              </td>
            </tr>

            <tr>
              <td style="padding: 18px 28px; background:#f8fafc; color:#94a3b8; font-size: 12px; line-height: 18px;">
                This is an automated message from Shuttle. Please do not reply to this email.
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""

    async def _build_confirmation_attachments(
        self,
        notification: TravellerContactNotification,
    ) -> list[MailAttachmentSchema]:
        if notification.event_type != "traveller_seat_confirmed":
            return []

        result = await self.db.execute(
            select(TripBooking)
            .where(TripBooking.id == notification.booking_id)
            .options(
                selectinload(TripBooking.passenger).selectinload(
                    User.passenger_profile
                ),
                selectinload(TripBooking.payments),
                selectinload(TripBooking.booking_session).selectinload(
                    BookingSession.payments
                ),
                selectinload(TripBooking.pickup_stop),
                selectinload(TripBooking.dropoff_stop),
                selectinload(TripBooking.scheduled_trip)
                .selectinload(ScheduledTrip.route)
                .selectinload(Route.route_stops)
                .selectinload(RouteStop.stop),
            )
            .limit(1)
        )
        booking = result.scalar_one_or_none()
        if booking is None:
            raise RuntimeError(
                "Traveller invoice booking could not be found."
            )

        passenger_service = PassengerService(self.db)
        invoice = await passenger_service._build_booking_invoice_payload(
            booking=booking,
            passenger_user=booking.passenger,
            passenger_profile=booking.passenger.passenger_profile,
        )
        qr_token, _payload = passenger_service._build_qr_token(booking)
        return [
            MailAttachmentSchema(
                filename=f"{invoice['invoice_number']}.pdf",
                content=generate_invoice_pdf(invoice),
                content_type="application/pdf",
            ),
            MailAttachmentSchema(
                filename=f"boarding-qr-{booking.id}.png",
                content=generate_booking_qr_png(qr_token),
                content_type="image/png",
                content_id="traveller-booking-qr",
                inline=True,
            ),
        ]

    async def _process_email(
        self,
        notification: TravellerContactNotification,
    ) -> None:
        recipient = (notification.traveller_email_snapshot or "").strip()

        if not recipient:
            await self._mark_skipped(
                notification,
                delivered_channel=None,
                reason="Traveller email snapshot is empty.",
            )
            return

        try:
            attachments = await self._build_confirmation_attachments(
                notification
            )
            body = notification.message
            if attachments:
                body += (
                    "\n\nYour boarding QR image and GST invoice/payment "
                    "receipt are attached."
                )
            provider_message_id = await self.email_sender.send_email(
                to_email=recipient,
                subject=notification.title,
                body=body,
                html_body=self._build_traveller_email_html(notification),
                attachments=attachments or None,
            )
        except Exception as exc:
            await self._mark_failed(notification, exc)
            return

        await self._mark_sent(
            notification,
            delivered_channel="email",
            provider_message_id=provider_message_id,
        )

    async def _mark_sent(
        self,
        notification: TravellerContactNotification,
        *,
        delivered_channel: str,
        provider_message_id: str,
    ) -> None:
        notification.status = TravellerContactNotificationStatus.SENT
        notification.delivered_channel = delivered_channel
        notification.provider_message_id = provider_message_id
        notification.failure_reason = None
        notification.delivery_retry_after = None
        notification.sent_at = utcnow()
        self.db.add(notification)
        await self.db.flush()

    async def _mark_skipped(
        self,
        notification: TravellerContactNotification,
        *,
        delivered_channel: str | None,
        reason: str,
    ) -> None:
        notification.status = TravellerContactNotificationStatus.SKIPPED
        notification.delivered_channel = delivered_channel
        notification.failure_reason = reason
        notification.delivery_retry_after = None
        self.db.add(notification)
        await self.db.flush()

    async def _mark_failed(
        self,
        notification: TravellerContactNotification,
        exc: Exception,
    ) -> None:
        attempt_count = int(notification.delivery_attempt_count or 1)
        retry_delay_seconds = self._get_retry_delay_seconds(attempt_count)

        notification.status = TravellerContactNotificationStatus.FAILED
        notification.failure_reason = str(exc)
        notification.delivery_retry_after = utcnow() + timedelta(
            seconds=retry_delay_seconds
        )
        self.db.add(notification)
        await self.db.flush()


def _delivery_enabled() -> bool:
    return os.getenv(
        "TRAVELLER_CONTACT_DELIVERY_ENABLED",
        "true",
    ).strip().lower() in {"1", "true", "yes", "on"}


def _delivery_interval_seconds() -> int:
    raw_value = os.getenv("TRAVELLER_CONTACT_DELIVERY_INTERVAL_SECONDS", "30").strip()
    try:
        value = int(raw_value)
    except ValueError:
        value = 30
    return min(max(value, 5), 3600)


async def run_traveller_contact_delivery_loop() -> None:
    if not _delivery_enabled():
        logger.info("traveller_contact_delivery_disabled")
        return

    interval_seconds = _delivery_interval_seconds()

    while True:
        try:
            async with AsyncSessionLocal() as db:
                service = TravellerContactDeliveryService(db)
                processed_count = await service.process_pending_batch()

                if processed_count > 0:
                    logger.info(
                        "traveller_contact_delivery_batch_processed count=%s",
                        processed_count,
                    )

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("traveller_contact_delivery_loop_failed")

        await asyncio.sleep(interval_seconds)
