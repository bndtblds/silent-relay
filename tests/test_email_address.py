from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.email_address import normalize_email_address
from app.models import ContactMethod
from app.services import AccountService, ManagementService
from app.smtp_config import load_email_config, save_email_config


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" User.Name+tag@EXAMPLE.COM ", "User.Name+tag@example.com"),
        ('"quoted local"@Example.COM', '"quoted local"@example.com'),
        ("δοκιμή@παράδειγμα.δοκιμή", "δοκιμή@παράδειγμα.δοκιμή"),
        ("user@[127.0.0.1]", "user@[127.0.0.1]"),
    ],
)
def test_normalize_email_address_accepts_supported_unusual_addresses(value, expected):
    assert normalize_email_address(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "plain-address",
        "two@@example.org",
        ".leading-dot@example.org",
        "trailing-dot.@example.org",
        "user name@example.org",
        "user@example.org\r\nBcc: victim@example.org",
        "user@example.org\nCc: victim@example.org",
        "user@-example.org",
    ],
)
def test_normalize_email_address_rejects_clearly_invalid_and_injected_values(value):
    with pytest.raises(ValueError, match="Invalid email address"):
        normalize_email_address(value)


def test_normalize_email_address_enforces_smtp_length_boundary():
    domain_189 = f"{'b' * 63}.{'c' * 63}.{'d' * 61}"
    assert len(f"{'a' * 64}@{domain_189}") == 254
    assert normalize_email_address(f"{'a' * 64}@{domain_189}")

    domain_190 = f"{'b' * 63}.{'c' * 63}.{'d' * 62}"
    with pytest.raises(ValueError):
        normalize_email_address(f"{'a' * 64}@{domain_190}")


def test_account_setup_uses_shared_normalization(db, settings, cipher):
    service = AccountService(settings, cipher)
    account, _, setup_token = service.create(db)

    service.setup(
        db,
        setup_token,
        "correct horse battery staple",
        " Owner.Name@EXAMPLE.ORG ",
    )

    contact = db.scalar(select(ContactMethod).where(ContactMethod.account_id == account.id))
    assert cipher.decrypt(contact.encrypted_value) == "Owner.Name@example.org"


def test_account_setup_rejects_header_injection(db, settings, cipher):
    service = AccountService(settings, cipher)
    _, _, setup_token = service.create(db)

    with pytest.raises(ValueError, match="Ungültige E-Mail-Adresse"):
        service.setup(
            db,
            setup_token,
            "correct horse battery staple",
            "owner@example.org\r\nBcc: victim@example.org",
        )

    assert db.scalar(select(func.count()).select_from(ContactMethod)) == 0


def test_contact_management_uses_shared_validation(db, settings, cipher):
    account, _, _ = AccountService(settings, cipher).create(db)
    service = ManagementService(settings, cipher)

    service.add_contact(db, account.id, "account", account.id, ' "local part"@EXAMPLE.ORG ')
    contact = db.scalar(select(ContactMethod))
    assert cipher.decrypt(contact.encrypted_value) == '"local part"@example.org'

    with pytest.raises(ValueError):
        service.add_contact(db, account.id, "account", account.id, "broken@@example.org")
    assert db.scalar(select(func.count()).select_from(ContactMethod)) == 1


def test_smtp_sender_uses_shared_validation(db, settings, cipher):
    save_email_config(
        db,
        cipher,
        host="smtp.example.org",
        port=587,
        username="mailer",
        password="secret",
        from_address=" Relay.Name@EXAMPLE.ORG ",
        from_name="SilentRelay",
    )
    assert load_email_config(db, cipher).from_address == "Relay.Name@example.org"

    with pytest.raises(ValueError):
        save_email_config(
            db,
            cipher,
            host="smtp.example.org",
            port=587,
            username="mailer",
            password=None,
            from_address="relay@example.org\nBcc: victim@example.org",
            from_name="SilentRelay",
        )
