from __future__ import annotations

import smtplib
import ssl
import re
from dataclasses import dataclass
from imaplib import IMAP4_SSL

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import SmtpConfiguration
from app.providers.email import EmailNotificationProvider, EmailProviderConfig
from app.security.core import FieldCipher, fingerprint


@dataclass(frozen=True)
class ImapNdrConfig:
    host: str
    port: int
    username: str
    password: str
    from_address: str


def load_email_config(db: Session, cipher: FieldCipher) -> EmailProviderConfig | None:
    stored = db.get(SmtpConfiguration, "default")
    if not stored:
        return None
    return EmailProviderConfig(
        host=cipher.decrypt(stored.encrypted_host),
        port=stored.port,
        username=cipher.decrypt(stored.encrypted_username),
        password=cipher.decrypt(stored.encrypted_password),
        from_address=cipher.decrypt(stored.encrypted_from_address),
        from_name=cipher.decrypt(stored.encrypted_from_name),
    )


def load_email_provider(db: Session, settings: Settings, cipher: FieldCipher) -> EmailNotificationProvider:
    return EmailNotificationProvider(load_email_config(db, cipher))


def save_email_config(
    db: Session,
    cipher: FieldCipher,
    *,
    host: str,
    port: int,
    username: str,
    password: str | None,
    from_address: str,
    from_name: str,
) -> SmtpConfiguration:
    host = host.strip()
    username = username.strip()
    from_address = from_address.strip()
    from_name = from_name.strip()
    if not host or not 1 <= port <= 65535:
        raise ValueError("SMTP-Host und Port sind ungültig.")
    if "@" not in from_address or len(from_address) > 320:
        raise ValueError("Die Absenderadresse ist ungültig.")
    stored = db.get(SmtpConfiguration, "default")
    if not stored:
        if password is None:
            password = ""
        stored = SmtpConfiguration(
            id="default",
            encrypted_host=cipher.encrypt(host),
            port=port,
            encrypted_username=cipher.encrypt(username) if username else None,
            encrypted_password=cipher.encrypt(password) if password else None,
            starttls=True,
            encrypted_from_address=cipher.encrypt(from_address),
            encrypted_from_name=cipher.encrypt(from_name or "SilentRelay"),
        )
        db.add(stored)
    else:
        previous_from_address = cipher.decrypt(stored.encrypted_from_address)
        if previous_from_address.strip().casefold() != from_address.casefold():
            stored.ndr_enabled = False
            stored.ndr_acknowledged_address_fingerprint = None
        stored.encrypted_host = cipher.encrypt(host)
        stored.port = port
        stored.encrypted_username = cipher.encrypt(username) if username else None
        if password is not None and password != "":
            stored.encrypted_password = cipher.encrypt(password)
        stored.starttls = True
        stored.encrypted_from_address = cipher.encrypt(from_address)
        stored.encrypted_from_name = cipher.encrypt(from_name or "SilentRelay")
    db.commit()
    return stored


def load_ndr_config(
    db: Session, settings: Settings, cipher: FieldCipher
) -> ImapNdrConfig | None:
    stored = db.get(SmtpConfiguration, "default")
    if not stored or not stored.ndr_enabled:
        return None
    email_config = load_email_config(db, cipher)
    if email_config is None:
        return None
    expected_fingerprint = fingerprint(
        email_config.from_address.strip().casefold(), settings.fingerprint_hmac_key
    )
    if (
        stored.ndr_acknowledged_address_fingerprint != expected_fingerprint
        or not stored.encrypted_imap_host
        or not stored.imap_port
        or not stored.encrypted_imap_username
        or not stored.encrypted_imap_password
    ):
        return None
    return ImapNdrConfig(
        host=cipher.decrypt(stored.encrypted_imap_host),
        port=stored.imap_port,
        username=cipher.decrypt(stored.encrypted_imap_username),
        password=cipher.decrypt(stored.encrypted_imap_password),
        from_address=email_config.from_address,
    )


def save_ndr_config(
    db: Session,
    settings: Settings,
    cipher: FieldCipher,
    *,
    host: str,
    port: int,
    username: str,
    password: str | None,
    acknowledged_address: str,
) -> SmtpConfiguration:
    stored = db.get(SmtpConfiguration, "default")
    if not stored:
        raise ValueError("Configure SMTP first.")
    email_config = load_email_config(db, cipher)
    if email_config is None:
        raise ValueError("Configure SMTP first.")
    host = host.strip()
    username = username.strip()
    acknowledged_address = acknowledged_address.strip().casefold()
    if not host or not 1 <= port <= 65535 or not username:
        raise ValueError("Invalid IMAP configuration.")
    if acknowledged_address != email_config.from_address.strip().casefold():
        raise ValueError("The acknowledged mailbox does not match the sender address.")
    local_part, separator, _ = email_config.from_address.partition("@")
    if (
        not separator
        or len(local_part.encode("utf-8")) > 20
        or not re.fullmatch(r"[A-Za-z0-9._-]+", local_part)
    ):
        raise ValueError("The sender address is too long for correlation tokens.")
    if not password and not stored.encrypted_imap_password:
        raise ValueError("An IMAP password is required.")
    stored.encrypted_imap_host = cipher.encrypt(host)
    stored.imap_port = port
    stored.encrypted_imap_username = cipher.encrypt(username)
    if password:
        stored.encrypted_imap_password = cipher.encrypt(password)
    stored.ndr_acknowledged_address_fingerprint = fingerprint(
        acknowledged_address, settings.fingerprint_hmac_key
    )
    stored.ndr_enabled = True
    db.commit()
    return stored


def disable_ndr_config(db: Session) -> None:
    stored = db.get(SmtpConfiguration, "default")
    if stored:
        stored.ndr_enabled = False
        stored.ndr_acknowledged_address_fingerprint = None
        db.commit()


def test_imap_connection(config: ImapNdrConfig) -> int:
    with IMAP4_SSL(config.host, config.port, timeout=15) as imap:
        imap.login(config.username, config.password)
        status, data = imap.select("INBOX", readonly=True)
        if status != "OK":
            raise OSError("Unable to select the technical mailbox.")
        return int(data[0]) if data and data[0] else 0


def test_smtp_connection(config: EmailProviderConfig) -> None:
    with smtplib.SMTP(config.host, config.port, timeout=15) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        if config.username:
            smtp.login(config.username, config.password)
