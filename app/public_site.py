from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.i18n import DEFAULT_LANGUAGE, normalize_language, translate
from app.models import PublicSiteContent

_LANGUAGE_PATTERN = re.compile(r"[a-z]{2}(?:-[A-Z]{2})?")


def load_public_site_content(
    db: Session, language_code: str = DEFAULT_LANGUAGE
) -> PublicSiteContent | None:
    return db.get(PublicSiteContent, language_code)


def load_public_site_content_with_fallback(
    db: Session, language_code: str, default_language: str = DEFAULT_LANGUAGE
) -> tuple[PublicSiteContent | None, bool]:
    content = load_public_site_content(db, language_code)
    if content is not None:
        return content, False
    fallback = load_public_site_content(db, default_language)
    return fallback, fallback is not None and language_code != default_language


def public_site_content_is_complete(content: PublicSiteContent | None) -> bool:
    return bool(
        content
        and content.imprint_text
        and content.privacy_text
        and content.contact_email
    )


def save_public_site_content(
    db: Session,
    *,
    language_code: str,
    imprint_text: str,
    privacy_text: str,
    contact_email: str,
    contact_text: str,
    validation_language: str | None = None,
) -> PublicSiteContent:
    language_code = language_code.strip()
    message_language = normalize_language(validation_language or language_code)
    imprint_text = _normalize_text(imprint_text, message_language)
    privacy_text = _normalize_text(privacy_text, message_language)
    contact_email = contact_email.strip()
    contact_text = _normalize_text(contact_text, message_language)

    if not _LANGUAGE_PATTERN.fullmatch(language_code):
        raise ValueError(translate(message_language, "error.language"))
    if not 1 <= len(imprint_text) <= 20_000:
        raise ValueError(translate(message_language, "error.imprint_length"))
    if not 1 <= len(privacy_text) <= 50_000:
        raise ValueError(translate(message_language, "error.privacy_length"))
    if len(contact_text) > 10_000:
        raise ValueError(translate(message_language, "error.contact_length"))
    if not _valid_email(contact_email):
        raise ValueError(translate(message_language, "error.contact_email"))

    content = db.get(PublicSiteContent, language_code)
    if content is None:
        content = PublicSiteContent(
            language_code=language_code,
            imprint_text=imprint_text,
            privacy_text=privacy_text,
            contact_email=contact_email,
            contact_text=contact_text,
        )
        db.add(content)
    else:
        content.imprint_text = imprint_text
        content.privacy_text = privacy_text
        content.contact_email = contact_email
        content.contact_text = contact_text
    db.flush()
    return content


def _normalize_text(value: str, language: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if any(ord(character) < 32 and character not in "\n\t" for character in normalized):
        raise ValueError(translate(language, "error.control_characters"))
    return normalized


def _valid_email(value: str) -> bool:
    if not 3 <= len(value) <= 320 or value.count("@") != 1:
        return False
    if any(character.isspace() or ord(character) < 32 for character in value):
        return False
    local_part, domain = value.rsplit("@", 1)
    return bool(local_part and domain and not domain.startswith(".") and not domain.endswith("."))
