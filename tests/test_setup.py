from pathlib import Path

import pytest

from app.config import Settings
from app.security.core import verify_password
from app.setup import (
    hash_admin_password,
    render_environment,
    validate_aliases,
    validate_domain,
    validate_username,
    write_private_file,
)


def test_setup_environment_contains_valid_independent_secrets():
    template = Path(".env.example").read_text(encoding="utf-8")
    rendered = render_environment(
        template,
        "relay.example.org",
        "operator",
        "correct horse battery",
        ["www.relay.example.org", "private.example.net"],
    )
    values = dict(
        line.split("=", 1)
        for line in rendered.splitlines()
        if line and not line.startswith("#") and "=" in line
    )

    assert values["APP_BASE_URL"] == "https://relay.example.org"
    assert values["CADDY_DOMAIN"] == "relay.example.org"
    assert values["CADDY_DOMAINS"] == "relay.example.org, www.relay.example.org, private.example.net"
    assert values["ADMIN_USERNAME"] == "operator"
    assert verify_password(values["ADMIN_PASSWORD_HASH"], "correct horse battery")
    secrets = {
        values["TOKEN_HMAC_KEY"],
        values["FINGERPRINT_HMAC_KEY"],
        values["SESSION_SECRET"],
        values["CSRF_SECRET"],
    }
    assert len(secrets) == 4
    assert all(len(value) >= 32 for value in secrets)


def test_generated_environment_loads_as_application_settings(tmp_path, monkeypatch):
    for name in (
        "APP_ENV",
        "APP_BASE_URL",
        "FIELD_ENCRYPTION_KEY",
        "TOKEN_HMAC_KEY",
        "FINGERPRINT_HMAC_KEY",
        "SESSION_SECRET",
        "CSRF_SECRET",
        "ADMIN_PASSWORD_HASH",
        "ACCOUNT_REVIEW_REMINDER_DAYS",
    ):
        monkeypatch.delenv(name, raising=False)
    target = tmp_path / ".env"
    target.write_text(
        render_environment(
            Path(".env.example").read_text(encoding="utf-8"),
            "relay.example.org",
            "operator",
            "correct horse battery",
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=target)

    assert settings.app_base_url == "https://relay.example.org"
    assert settings.account_review_reminder_days == [-30, -15, -3, 0, 30]


@pytest.mark.parametrize("domain", ["https://example.org", "localhost", "example.org:8443", "-bad.example"])
def test_setup_rejects_invalid_public_domains(domain):
    with pytest.raises(ValueError):
        validate_domain(domain)


def test_setup_validates_optional_domain_aliases():
    assert validate_aliases("", "example.org") == []
    assert validate_aliases("www.example.org, relay.example.net", "example.org") == [
        "www.example.org",
        "relay.example.net",
    ]
    with pytest.raises(ValueError):
        validate_aliases("example.org", "example.org")
    with pytest.raises(ValueError):
        validate_aliases("www.example.org,www.example.org", "example.org")
    with pytest.raises(ValueError):
        validate_aliases("*.example.org", "example.org")


def test_setup_validates_admin_username():
    assert validate_username("admin.user-1") == "admin.user-1"
    with pytest.raises(ValueError):
        validate_username("not an admin")


def test_setup_password_hashing_does_not_require_application_configuration(monkeypatch):
    for name in (
        "FIELD_ENCRYPTION_KEY",
        "TOKEN_HMAC_KEY",
        "FINGERPRINT_HMAC_KEY",
        "SESSION_SECRET",
        "CSRF_SECRET",
        "ADMIN_PASSWORD_HASH",
    ):
        monkeypatch.delenv(name, raising=False)

    assert verify_password(hash_admin_password("correct horse battery"), "correct horse battery")


def test_setup_private_file_does_not_leave_temporary_file(tmp_path):
    target = tmp_path / ".env"
    write_private_file(target, "VALUE=secret\n")

    assert target.read_text(encoding="utf-8") == "VALUE=secret\n"
    assert list(tmp_path.iterdir()) == [target]
