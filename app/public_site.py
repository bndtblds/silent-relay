from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.models import PublicSiteContent

DEFAULT_LANGUAGE = "de"
_LANGUAGE_PATTERN = re.compile(r"[a-z]{2}(?:-[A-Z]{2})?")


def load_public_site_content(
    db: Session, language_code: str = DEFAULT_LANGUAGE
) -> PublicSiteContent | None:
    return db.get(PublicSiteContent, language_code)


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
) -> PublicSiteContent:
    language_code = language_code.strip()
    imprint_text = _normalize_text(imprint_text)
    privacy_text = _normalize_text(privacy_text)
    contact_email = contact_email.strip()
    contact_text = _normalize_text(contact_text)

    if not _LANGUAGE_PATTERN.fullmatch(language_code):
        raise ValueError("Der Sprachcode ist ungültig.")
    if not 1 <= len(imprint_text) <= 20_000:
        raise ValueError("Das Impressum muss 1 bis 20.000 Zeichen enthalten.")
    if not 1 <= len(privacy_text) <= 50_000:
        raise ValueError("Der Datenschutzhinweis muss 1 bis 50.000 Zeichen enthalten.")
    if len(contact_text) > 10_000:
        raise ValueError("Der Kontakthinweis darf höchstens 10.000 Zeichen enthalten.")
    if not _valid_email(contact_email):
        raise ValueError("Die Kontaktadresse ist ungültig.")

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


def _normalize_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if any(ord(character) < 32 and character not in "\n\t" for character in normalized):
        raise ValueError("Der Text enthält ungültige Steuerzeichen.")
    return normalized


def _valid_email(value: str) -> bool:
    if not 3 <= len(value) <= 320 or value.count("@") != 1:
        return False
    if any(character.isspace() or ord(character) < 32 for character in value):
        return False
    local_part, domain = value.rsplit("@", 1)
    return bool(local_part and domain and not domain.startswith(".") and not domain.endswith("."))
