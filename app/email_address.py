"""Shared email-address normalization and syntax validation."""

from email_validator import EmailNotValidError, validate_email


def normalize_email_address(value: str) -> str:
    """Return a normalized address after offline-only syntax validation."""
    if "\r" in value or "\n" in value:
        raise ValueError("Invalid email address.")
    try:
        result = validate_email(
            value.strip(),
            allow_domain_literal=True,
            allow_quoted_local=True,
            check_deliverability=False,
        )
    except EmailNotValidError as exc:
        raise ValueError("Invalid email address.") from exc
    return result.normalized
