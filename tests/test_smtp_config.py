from app.models import SmtpConfiguration
from app.smtp_config import (
    load_email_config,
    load_ndr_config,
    save_email_config,
    save_ndr_config,
    test_imap_connection as check_imap_connection,
)


def test_smtp_configuration_is_encrypted_and_loadable(db, settings, cipher):
    save_email_config(
        db,
        cipher,
        host="smtp.example.org",
        port=587,
        username="mailer",
        password="smtp-secret",
        starttls=True,
        from_address="relay@example.org",
        from_name="SilentRelay",
    )
    stored = db.get(SmtpConfiguration, "default")
    raw_values = b"".join(filter(None, [
        stored.encrypted_host,
        stored.encrypted_username,
        stored.encrypted_password,
        stored.encrypted_from_address,
        stored.encrypted_from_name,
    ]))
    assert b"smtp.example.org" not in raw_values
    assert b"smtp-secret" not in raw_values
    assert b"relay@example.org" not in raw_values

    loaded = load_email_config(db, settings, cipher)
    assert loaded.host == "smtp.example.org"
    assert loaded.password == "smtp-secret"
    assert loaded.from_address == "relay@example.org"


def test_blank_password_preserves_existing_secret(db, settings, cipher):
    save_email_config(
        db, cipher, host="smtp.example.org", port=587, username="mailer",
        password="existing-secret", starttls=True,
        from_address="relay@example.org", from_name="SilentRelay",
    )
    save_email_config(
        db, cipher, host="smtp2.example.org", port=465, username="mailer",
        password=None, starttls=False,
        from_address="relay@example.org", from_name="SilentRelay",
    )
    loaded = load_email_config(db, settings, cipher)
    assert loaded.host == "smtp2.example.org"
    assert loaded.password == "existing-secret"


def test_ndr_configuration_requires_exact_sender_acknowledgement(
    db, settings, cipher
):
    save_email_config(
        db, cipher, host="smtp.example.org", port=587, username="mailer",
        password="smtp-secret", starttls=True,
        from_address="notifications@example.org", from_name="SilentRelay",
    )
    try:
        save_ndr_config(
            db, settings, cipher, host="imap.example.org", port=993,
            username="notifications@example.org", password="imap-secret",
            acknowledged_address="other@example.org",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("A different mailbox address was accepted.")

    save_ndr_config(
        db, settings, cipher, host="imap.example.org", port=993,
        username="notifications@example.org", password="imap-secret",
        acknowledged_address="notifications@example.org",
    )
    loaded = load_ndr_config(db, settings, cipher)
    assert loaded.host == "imap.example.org"
    assert loaded.password == "imap-secret"

    save_email_config(
        db, cipher, host="smtp.example.org", port=587, username="mailer",
        password=None, starttls=True,
        from_address="changed@example.org", from_name="SilentRelay",
    )
    assert load_ndr_config(db, settings, cipher) is None


def test_imap_connection_check_opens_inbox_read_only(monkeypatch):
    calls = []

    class FakeImap:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def login(self, username, password):
            calls.append(("login", username, password))

        def select(self, mailbox, readonly):
            calls.append(("select", mailbox, readonly))
            return "OK", [b"7"]

    monkeypatch.setattr("app.smtp_config.IMAP4_SSL", FakeImap)
    from app.smtp_config import ImapNdrConfig

    count = check_imap_connection(
        ImapNdrConfig(
            "imap.example.org", 993, "notifications@example.org",
            "secret", "notifications@example.org",
        )
    )
    assert count == 7
    assert calls[-1] == ("select", "INBOX", True)
