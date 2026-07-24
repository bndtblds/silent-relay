from __future__ import annotations

import getpass
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from cryptography.fernet import Fernet

_password_hasher = PasswordHasher()


def hash_admin_password(password: str) -> str:
    if not 12 <= len(password) <= 256:
        raise ValueError("The password must be 12 to 256 characters long.")
    return _password_hasher.hash(password)


def validate_domain(value: str) -> str:
    domain = value.strip().lower().removesuffix(".")
    parsed = urlsplit(f"https://{domain}")
    if (
        not domain
        or parsed.hostname != domain
        or parsed.port is not None
        or not re.fullmatch(r"[a-z0-9.-]+", domain)
        or "." not in domain
    ):
        raise ValueError("Enter a public domain such as relay.example.org.")
    labels = domain.split(".")
    if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in labels):
        raise ValueError("Enter a valid public domain.")
    return domain


def validate_username(value: str) -> str:
    username = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", username):
        raise ValueError("Use 1 to 64 letters, digits, dots, underscores, or hyphens.")
    return username


def validate_aliases(value: str, canonical_domain: str) -> list[str]:
    if not value.strip():
        return []
    aliases = [validate_domain(part) for part in value.split(",")]
    if canonical_domain in aliases:
        raise ValueError("Do not repeat the primary domain as an additional domain.")
    if len(set(aliases)) != len(aliases):
        raise ValueError("Each additional domain may only be entered once.")
    return aliases


def render_environment(
    template: str,
    domain: str,
    username: str,
    password: str,
    aliases: list[str] | None = None,
) -> str:
    caddy_domains = ", ".join([domain, *(aliases or [])])
    replacements = {
        "APP_BASE_URL": f"https://{domain}",
        "CADDY_DOMAIN": domain,
        "CADDY_DOMAINS": caddy_domains,
        "FIELD_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "TOKEN_HMAC_KEY": secrets.token_urlsafe(48),
        "FINGERPRINT_HMAC_KEY": secrets.token_urlsafe(48),
        "SESSION_SECRET": secrets.token_urlsafe(48),
        "CSRF_SECRET": secrets.token_urlsafe(48),
        "ADMIN_USERNAME": username,
        "ADMIN_PASSWORD_HASH": hash_admin_password(password),
    }
    found: set[str] = set()
    lines: list[str] = []
    for line in template.splitlines():
        key, separator, _ = line.partition("=")
        if separator and key in replacements:
            value = replacements[key]
            if key == "ADMIN_PASSWORD_HASH":
                value = f"'{value}'"
            lines.append(f"{key}={value}")
            found.add(key)
        else:
            lines.append(line)
    missing = replacements.keys() - found
    if missing:
        raise ValueError(f"Missing settings in .env.example: {', '.join(sorted(missing))}")
    return "\n".join(lines) + "\n"


def prompt_value(label: str, validator, default: str | None = None) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        value = input(f"{label}{suffix}: ").strip() or default or ""
        try:
            return validator(value)
        except ValueError as exc:
            print(exc)


def prompt_password() -> str:
    while True:
        password = getpass.getpass("Administrator password (12 to 256 characters): ")
        confirmation = getpass.getpass("Repeat administrator password: ")
        if password != confirmation:
            print("The passwords do not match.")
            continue
        try:
            hash_admin_password(password)
        except ValueError as exc:
            print(exc)
            continue
        return password


def prompt_aliases(canonical_domain: str) -> list[str]:
    while True:
        value = input(
            f"Additional domains (optional, comma-separated, for example www.{canonical_domain}): "
        )
        try:
            return validate_aliases(value, canonical_domain)
        except ValueError as exc:
            print(exc)


def write_private_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".env.", dir=path.parent, text=True)
    temporary_path = Path(temporary_name)
    try:
        os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        temporary_path.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def main() -> int:
    config_directory = Path(os.environ.get("SILENTRELAY_CONFIG_DIR", "/config"))
    template_path = config_directory / ".env.example"
    target_path = config_directory / ".env"
    if target_path.exists():
        print(f"Setup stopped: {target_path} already exists. It was not changed.", file=sys.stderr)
        return 2
    if not template_path.is_file():
        print(f"Setup stopped: {template_path} is missing.", file=sys.stderr)
        return 2

    print("SilentRelay setup")
    print("Before continuing, point the public domain to this server and open ports 80 and 443.")
    domain = prompt_value("Public domain", validate_domain)
    aliases = prompt_aliases(domain)
    username = prompt_value("Technical administrator username", validate_username, "admin")
    password = prompt_password()
    environment = render_environment(
        template_path.read_text(encoding="utf-8"),
        domain,
        username,
        password,
        aliases,
    )
    write_private_file(target_path, environment)
    print(f"Configuration written to {target_path}.")
    print("The administrator password is not stored in clear text.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
