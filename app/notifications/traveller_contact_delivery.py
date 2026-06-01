from __future__ import annotations

import asyncio
import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import AsyncSessionLocal
from app.db.schema import (
    TravellerContactNotification,
    TravellerContactNotificationStatus,
)

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
        message.set_content(body)

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
            provider_message_id = await self.email_sender.send_email(
                to_email=recipient,
                subject=notification.title,
                body=notification.message,
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