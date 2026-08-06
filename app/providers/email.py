from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from app.providers.base import DeliveryResult


@dataclass(frozen=True)
class EmailProviderConfig:
    host: str
    port: int
    username: str
    password: str
    starttls: bool
    from_address: str
    from_name: str

class EmailNotificationProvider:
    channel = "email"

    def __init__(self, config: EmailProviderConfig | None):
        self.config = config

    def send(
        self,
        recipient: str,
        subject: str,
        body: str,
        *,
        envelope_token: str | None = None,
    ) -> DeliveryResult:
        if self.config is None:
            return DeliveryResult(False, error_class="email_not_configured")
        message = EmailMessage()
        message["From"] = formataddr((self.config.from_name, self.config.from_address))
        message["To"] = recipient
        message["Subject"] = subject
        message["Message-ID"] = make_msgid(domain=self.config.from_address.partition("@")[2] or None)
        message.set_content(body)
        try:
            with smtplib.SMTP(self.config.host, self.config.port, timeout=15) as smtp:
                if self.config.starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if self.config.username:
                    smtp.login(self.config.username, self.config.password)
                smtp.send_message(
                    message,
                    from_addr=_envelope_sender(self.config.from_address, envelope_token),
                    to_addrs=[recipient],
                )
            return DeliveryResult(True, message_id=message["Message-ID"])
        except smtplib.SMTPRecipientsRefused:
            return DeliveryResult(False, permanent_failure=True, error_class="recipient_rejected")
        except (smtplib.SMTPException, OSError):
            return DeliveryResult(False, error_class="temporary_smtp_error")


def _envelope_sender(from_address: str, token: str | None) -> str:
    if not token:
        return from_address
    local, separator, domain = from_address.partition("@")
    if not separator or not local or not domain:
        return from_address
    return f"{local}+{token}@{domain}"
