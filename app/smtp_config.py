from __future__ import annotations

import smtplib
import ssl

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import SmtpConfiguration
from app.providers.email import EmailNotificationProvider, EmailProviderConfig
from app.security.core import FieldCipher


def load_email_config(db: Session, settings: Settings, cipher: FieldCipher) -> EmailProviderConfig:
    stored = db.get(SmtpConfiguration, "default")
    if not stored:
        return EmailProviderConfig.from_settings(settings)
    return EmailProviderConfig(
        host=cipher.decrypt(stored.encrypted_host),
        port=stored.port,
        username=cipher.decrypt(stored.encrypted_username),
        password=cipher.decrypt(stored.encrypted_password),
        starttls=stored.starttls,
        from_address=cipher.decrypt(stored.encrypted_from_address),
        from_name=cipher.decrypt(stored.encrypted_from_name),
    )


def load_email_provider(db: Session, settings: Settings, cipher: FieldCipher) -> EmailNotificationProvider:
    return EmailNotificationProvider(settings, load_email_config(db, settings, cipher))


def save_email_config(
    db: Session,
    cipher: FieldCipher,
    *,
    host: str,
    port: int,
    username: str,
    password: str | None,
    starttls: bool,
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
            starttls=starttls,
            encrypted_from_address=cipher.encrypt(from_address),
            encrypted_from_name=cipher.encrypt(from_name or "SilentRelay"),
        )
        db.add(stored)
    else:
        stored.encrypted_host = cipher.encrypt(host)
        stored.port = port
        stored.encrypted_username = cipher.encrypt(username) if username else None
        if password is not None and password != "":
            stored.encrypted_password = cipher.encrypt(password)
        stored.starttls = starttls
        stored.encrypted_from_address = cipher.encrypt(from_address)
        stored.encrypted_from_name = cipher.encrypt(from_name or "SilentRelay")
    db.commit()
    return stored


def test_smtp_connection(config: EmailProviderConfig) -> None:
    with smtplib.SMTP(config.host, config.port, timeout=15) as smtp:
        if config.starttls:
            smtp.starttls(context=ssl.create_default_context())
        if config.username:
            smtp.login(config.username, config.password)
