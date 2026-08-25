from starlette.requests import Request

from app.config import Settings
from app.i18n import browser_language, normalize_language, translate


def request_with_language(value: str) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [(b"accept-language", value.encode("ascii"))],
    })


def test_browser_language_uses_quality_and_fallback():
    assert browser_language(request_with_language("fr-FR, en;q=0.9, de;q=0.7")) == "en"
    assert browser_language(request_with_language("fr-FR"), "de") == "de"
    assert browser_language(request_with_language("de-CH, en;q=0.5")) == "de"
    assert browser_language(request_with_language("es-ES")) == "en"


def test_product_default_language_is_english():
    assert Settings.model_fields["default_language"].default == "en"


def test_language_and_missing_translation_fallback_are_deterministic():
    assert normalize_language("EN-us") == "en"
    assert normalize_language("fr", "en") == "en"
    assert translate("en", "home.create") == "Create an account"
    assert translate("de", "inbox.intro") == (
        "Hier lesen Sie Ihre vertraulichen Nachrichten und bestätigen sie ausdrücklich als gelesen."
    )
    assert translate("en", "inbox.intro") == (
        "Here you can read your confidential messages and explicitly confirm them as read."
    )
    assert translate("en", "missing.translation.key") == "missing.translation.key"
