#app/auth/mailer.py
from __future__ import annotations

import mimetypes
import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from typing import Iterable, Sequence

from app.auth.constants import (
    DEFAULT_ATTACHMENT_CONTENT_TYPE,
    MAIL_FROM_NAME,
    MAIL_TEMPLATE_SUBJECTS,
)
from app.auth.schemas import MailAttachmentSchema
from app.auth.template_renderer import render_template


def _get_env(name: str, *, required: bool = True, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if required and (value is None or not str(value).strip()):
        raise RuntimeError(f"{name} is not set.")
    return "" if value is None else str(value).strip()


def _get_bool_env(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _build_message(
    *,
    to_email: str,
    subject: str,
    body: str,
    subtype: str,
    attachments: Sequence[MailAttachmentSchema] | None = None,
) -> EmailMessage:
    from_email = _get_env("MAIL_FROM_EMAIL")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((MAIL_FROM_NAME, from_email))
    message["To"] = to_email
    message.set_content(body, subtype=subtype)

    for attachment in attachments or ():
        content_type = attachment.content_type or DEFAULT_ATTACHMENT_CONTENT_TYPE
        guessed_type, _ = mimetypes.guess_type(attachment.filename)
        final_content_type = guessed_type or content_type or DEFAULT_ATTACHMENT_CONTENT_TYPE

        if "/" in final_content_type:
            maintype, subtype_name = final_content_type.split("/", 1)
        else:
            maintype, subtype_name = "application", "octet-stream"

        message.add_attachment(
            attachment.content,
            maintype=maintype,
            subtype=subtype_name,
            filename=attachment.filename,
        )

    return message


def send_mail(
    *,
    to_email: str,
    subject: str,
    body: str,
    subtype: str = "plain",
    attachments: Sequence[MailAttachmentSchema] | None = None,
) -> None:
    smtp_host = _get_env("SMTP_HOST")
    smtp_port = int(_get_env("SMTP_PORT"))
    smtp_username = _get_env("SMTP_USERNAME", required=False, default="")
    smtp_password = _get_env("SMTP_PASSWORD", required=False, default="")
    smtp_timeout_seconds = float(
        _get_env("SMTP_TIMEOUT_SECONDS", required=False, default="20")
    )
    use_tls = _get_bool_env("SMTP_USE_TLS", default=True)
    use_ssl = _get_bool_env("SMTP_USE_SSL", default=False)

    if use_tls and use_ssl:
        raise RuntimeError("SMTP_USE_TLS and SMTP_USE_SSL cannot both be enabled.")

    message = _build_message(
        to_email=to_email,
        subject=subject,
        body=body,
        subtype=subtype,
        attachments=attachments,
    )

    if use_ssl:
        with smtplib.SMTP_SSL(
            smtp_host, smtp_port, timeout=smtp_timeout_seconds
        ) as server:
            if smtp_username:
                server.login(smtp_username, smtp_password)
            server.send_message(message)
        return

    with smtplib.SMTP(
        smtp_host, smtp_port, timeout=smtp_timeout_seconds
    ) as server:
        server.ehlo()
        if use_tls:
            server.starttls()
            server.ehlo()

        if smtp_username:
            server.login(smtp_username, smtp_password)

        server.send_message(message)


def send_templated_mail(
    *,
    to_email: str,
    template_filename: str,
    replacements: dict[str, object] | None = None,
    subject: str | None = None,
    attachments: Sequence[MailAttachmentSchema] | None = None,
) -> None:
    rendered = render_template(
        template_filename=template_filename,
        replacements=replacements or {},
    )

    final_subject = subject or MAIL_TEMPLATE_SUBJECTS.get(
        template_filename,
        "Notification",
    )

    send_mail(
        to_email=to_email,
        subject=final_subject,
        body=rendered.content,
        subtype=rendered.subtype,
        attachments=attachments,
    )
