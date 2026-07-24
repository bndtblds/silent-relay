from app.models import SmtpConfiguration
from app.smtp_config import load_email_config, save_email_config


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
