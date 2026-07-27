import html
import re

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import engine
from app.config import get_settings
from app.main import app
from app.models import Account, AuditLog, Partner, PublicSiteContent, SmtpConfiguration
from app.providers.base import DeliveryResult
from app.routers import admin, web
from app.security.core import hash_password
from app.security.core import SessionManager


def hidden_value(body: str, name: str) -> str:
    match = re.search(rf'name="{re.escape(name)}" value="([^"]+)"', body)
    assert match, f"hidden field {name!r} missing"
    return html.unescape(match.group(1))


class RecordingEmailProvider:
    messages: list[tuple[str, str, str]] = []

    def __init__(self, settings):
        pass

    def send(self, recipient: str, subject: str, body: str) -> DeliveryResult:
        self.messages.append((recipient, subject, body))
        return DeliveryResult(True, message_id="test-message")


def test_health_and_security_headers():
    with TestClient(app) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_public_html_pages_render():
    with TestClient(app) as client:
        index = client.get("/")
        create = client.get("/account/create")
        admin = client.get("/admin/login")
    assert index.status_code == 200
    assert "Im Ernstfall die richtigen Menschen erreichen" in index.text
    assert create.status_code == 200
    assert 'name="csrf"' in create.text
    assert admin.status_code == 200
    assert "Admin-Anmeldung" in admin.text


def test_browser_language_controls_public_and_admin_pages():
    with TestClient(app) as client:
        index = client.get("/", headers={"Accept-Language": "en-GB,en;q=0.9,de;q=0.5"})
        create = client.get("/account/create", headers={"Accept-Language": "en"})
        admin_login = client.get("/admin/login", headers={"Accept-Language": "en"})
    assert '<html lang="en">' in index.text
    assert "Reach the right people when it matters" in index.text
    assert "Account language" in create.text
    assert "Admin sign-in" in admin_login.text


def test_complete_admin_area_uses_browser_language():
    settings = get_settings()
    settings.admin_password_hash = hash_password("admin demo password")
    headers = {"Accept-Language": "en"}
    with TestClient(app) as client:
        accounts = client.post(
            "/admin/login",
            data={"username": settings.admin_username, "password": "admin demo password"},
            headers=headers,
            follow_redirects=True,
        )
        assert "Technical account overview" in accounts.text
        assert "Configure email delivery" in client.get("/admin/system", headers=headers).text
        public = client.get("/admin/public-content?content_language=en", headers=headers)
        assert "Imprint, privacy, and contact" in public.text
        assert 'name="language_code" value="en"' in public.text


def test_account_language_is_persisted_and_can_be_changed():
    settings = get_settings()
    with Session(engine) as db:
        account = Account(language_code="de")
        db.add(account)
        db.flush()
        token, csrf = SessionManager(settings).create(db, "account_owner", account.id)
        account_id = account.id
        db.commit()

    with TestClient(app) as client:
        client.cookies.set("sr_account_owner", token)
        client.cookies.set("sr_account_owner_csrf", csrf)
        german = client.get("/account/dashboard", headers={"Accept-Language": "en"})
        assert "Kontoverwaltung" in german.text
        changed = client.post(
            "/account/language",
            data={"csrf": csrf, "language_code": "en"},
            headers={"Accept-Language": "de"},
            follow_redirects=True,
        )
        assert changed.status_code == 200
        assert "Account management" in changed.text

    with Session(engine) as db:
        assert db.get(Account, account_id).language_code == "en"


def test_selected_account_language_controls_onboarding():
    with TestClient(app) as client:
        create = client.get("/account/create", headers={"Accept-Language": "de"})
        created = client.post(
            "/account/create",
            data={"csrf": hidden_value(create.text, "csrf"), "language_code": "en"},
        )
        assert "Save your account-owner access now" in created.text
        setup_url = html.unescape(
            re.search(r'href="(http://testserver/account/setup/[^"]+)"', created.text).group(1)
        )
        setup = client.get(setup_url.removeprefix("http://testserver"), headers={"Accept-Language": "de"})
        assert "Secure your account" in setup.text
        assert '<html lang="en">' in setup.text

    with Session(engine) as db:
        account = db.scalar(select(Account))
        assert account.language_code == "en"


def test_docs_are_not_exposed():
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/superadmin/login").status_code == 404


def test_invalid_notify_token_is_neutral():
    with TestClient(app) as client:
        response = client.get("/notify/not-a-real-token")
    assert response.status_code == 404
    assert "nicht verfügbar" in response.text
    assert "token" not in response.text.casefold()


def test_complete_account_owner_and_notify_flow(monkeypatch):
    RecordingEmailProvider.messages = []
    provider = RecordingEmailProvider(None)
    monkeypatch.setattr(web, "load_email_provider", lambda db, settings, cipher: provider)

    with TestClient(app) as client:
        create_form = client.get("/account/create")
        created = client.post("/account/create", data={"csrf": hidden_value(create_form.text, "csrf")})
        assert created.status_code == 200
        assert "Zugangsdaten drucken" in created.text
        assert 'data-copy-target="account-owner-link"' in created.text
        assert 'aria-label="Kontoinhaber-Link kopieren"' in created.text
        assert "/static/print.js" in created.text
        setup_url = html.unescape(re.search(r'href="(http://testserver/account/setup/[^"]+)"', created.text).group(1))
        account_owner_url = html.unescape(
            re.search(
                r'<p class="secret" id="account-owner-link">(http://testserver/account/[^<]+)</p>',
                created.text,
            ).group(1)
        )

        setup_path = setup_url.removeprefix("http://testserver")
        setup_form = client.get(setup_path)
        setup_done = client.post(setup_path, data={
            "csrf": hidden_value(setup_form.text, "csrf"),
            "password": "correct horse battery staple",
            "email": "owner@example.org",
        })
        assert setup_done.status_code == 200
        verification_url = RecordingEmailProvider.messages[-1][2].splitlines()[-1]
        verified = client.get(verification_url.removeprefix("http://testserver"))
        assert verified.status_code == 200
        assert "Konto ist jetzt aktiv" in verified.text
        assert "So geht es weiter" in verified.text

        account_owner_path = account_owner_url.removeprefix("http://testserver")
        login_form = client.get(account_owner_path)
        assert login_form.status_code == 200
        login = client.post(
            f"{account_owner_path}/login",
            data={"password": "correct horse battery staple"},
            follow_redirects=True,
        )
        assert login.status_code == 200
        assert "Kontoverwaltung" in login.text
        assert "Neuen Kontoinhaber-Zugang erstellen" in login.text
        assert "sr_account_owner" in client.cookies
        assert "sr_admin" not in client.cookies
        csrf = hidden_value(login.text, "csrf")

        partner_response = client.post(
            "/account/partners", data={"csrf": csrf, "name": "Ausgeschlossener Partner"},
            follow_redirects=True,
        )
        assert partner_response.status_code == 200
        assert "partner-card" in partner_response.text
        assert "Weiteren Partner hinzufügen" in partner_response.text
        with Session(engine) as db:
            partner_id = db.scalar(select(Partner.id))
        partner_contact = client.post(
            "/account/contacts",
            data={
                "csrf": csrf,
                "owner_type": "partner",
                "owner_id": partner_id,
                "value": "partner@example.org",
            },
            follow_redirects=True,
        )
        assert partner_contact.status_code == 200
        partner_verification_url = RecordingEmailProvider.messages[-1][2].splitlines()[-1]
        assert client.get(partner_verification_url.removeprefix("http://testserver")).status_code == 200

        owner_token_page = client.post(
            "/account/trusted-persons",
            data={"csrf": csrf, "name": "Vertrauensperson des Kontoinhabers"},
        )
        assert owner_token_page.status_code == 200
        assert "QR-Code Ihrer Vertrauensperson" in owner_token_page.text

        token_page = client.post(
            f"/account/partners/{partner_id}/trusted-persons",
            data={"csrf": csrf, "name": "Vertrauensperson"},
        )
        assert token_page.status_code == 200
        assert "QR-Code drucken" in token_page.text
        assert 'data-copy-target="personal-access-link"' in token_page.text
        assert 'aria-label="Persönlichen Zugangslink kopieren"' in token_page.text
        assert "/static/print.js" in token_page.text
        assert "So geht es weiter" in token_page.text
        notify_url = html.unescape(
            re.search(
                r'<p class="secret" id="personal-access-link">(http://testserver/notify/[^<]+)</p>',
                token_page.text,
            ).group(1)
        )

        notify_path = notify_url.removeprefix("http://testserver")
        notify_form = client.get(notify_path)
        assert notify_form.status_code == 200
        notify_csrf = hidden_value(notify_form.text, "csrf")
        confirmation = client.post(notify_path, data={
            "csrf": notify_csrf,
            "message": "Dies ist eine vertrauliche Testnachricht.",
        })
        assert confirmation.status_code == 200
        assert "owner@example.org" not in confirmation.text
        submission = hidden_value(confirmation.text, "submission")
        success = client.post(
            f"{notify_path}/confirm",
            data={"csrf": notify_csrf, "submission": submission},
            follow_redirects=True,
        )
        assert success.status_code == 200
        assert "Die vertrauliche Nachricht wurde zur Zustellung angenommen." in success.text
        assert "Sie müssen nichts weiter tun" in success.text
        assert RecordingEmailProvider.messages[-1][0] == "owner@example.org"


def test_admin_can_configure_and_test_smtp(monkeypatch):
    settings = get_settings()
    settings.admin_password_hash = hash_password("admin demo password")
    provider = RecordingEmailProvider(None)
    monkeypatch.setattr(admin, "test_smtp_connection", lambda config: None)
    monkeypatch.setattr(admin, "load_email_provider", lambda db, settings, cipher: provider)

    with TestClient(app) as client:
        login = client.post(
            "/admin/login",
            data={"username": settings.admin_username, "password": "admin demo password"},
            follow_redirects=True,
        )
        assert login.status_code == 200
        assert "Technische Kontenübersicht" in login.text
        system = client.get("/admin/system")
        assert system.status_code == 200
        assert "E-Mail-Versand einrichten" in system.text
        csrf = hidden_value(system.text, "csrf")

        saved = client.post("/admin/system/smtp", data={
            "csrf": csrf,
            "host": "smtp.example.org",
            "port": "587",
            "username": "mailer",
            "password": "secret-password",
            "starttls": "yes",
            "from_address": "relay@example.org",
            "from_name": "SilentRelay",
        }, follow_redirects=True)
        assert saved.status_code == 200
        assert "sicher gespeichert" in saved.text
        assert "secret-password" not in saved.text

        connection = client.post(
            "/admin/system/smtp/test-connection", data={"csrf": csrf},
            follow_redirects=True,
        )
        assert "Verbindung zum SMTP-Server war erfolgreich" in connection.text
        email = client.post(
            "/admin/system/smtp/test-email",
            data={"csrf": csrf, "recipient": "test@example.org"},
            follow_redirects=True,
        )
        assert "Test-E-Mail wurde vom SMTP-Server angenommen" in email.text
        assert provider.messages[-1][0] == "test@example.org"
        logout = client.post(
            "/admin/logout", data={"csrf": csrf}, follow_redirects=True,
        )
        assert "Admin-Anmeldung" in logout.text
        assert client.get("/admin/accounts", follow_redirects=False).status_code == 303

    with Session(engine) as db:
        stored = db.get(SmtpConfiguration, "default")
        assert b"secret-password" not in stored.encrypted_password


def test_admin_can_publish_escaped_operator_information():
    settings = get_settings()
    settings.admin_password_hash = hash_password("admin demo password")

    with TestClient(app) as client:
        imprint_before = client.get("/imprint")
        assert imprint_before.status_code == 200
        assert "noch nicht hinterlegt" in imprint_before.text
        assert 'href="/privacy"' in imprint_before.text
        assert client.get("/admin/public-content", follow_redirects=False).status_code == 303

        login = client.post(
            "/admin/login",
            data={"username": settings.admin_username, "password": "admin demo password"},
            follow_redirects=True,
        )
        assert login.status_code == 200
        csrf = hidden_value(login.text, "csrf")

        public_content_form = client.get("/admin/public-content")
        assert "Einrichtung noch nicht abgeschlossen" in public_content_form.text
        assert client.post(
            "/admin/public-content",
            data={
                "csrf": "wrong",
                "imprint_text": "Impressum",
                "privacy_text": "Datenschutz",
                "contact_email": "support@example.org",
                "contact_text": "",
            },
        ).status_code == 403

        invalid = client.post(
            "/admin/public-content",
            data={
                "csrf": csrf,
                "imprint_text": "",
                "privacy_text": "Datenschutz",
                "contact_email": "not-an-email",
                "contact_text": "",
            },
        )
        assert invalid.status_code == 400
        assert "Impressum muss" in invalid.text

        saved = client.post(
            "/admin/public-content",
            data={
                "csrf": csrf,
                "imprint_text": "Beispielbetrieb\n<script>alert('x')</script>",
                "privacy_text": "Keine externen Tracker.\n<b>Nicht als HTML</b>",
                "contact_email": "support@example.org",
                "contact_text": "Technische Fragen\nbitte per E-Mail.",
            },
            follow_redirects=True,
        )
        assert saved.status_code == 200
        assert "öffentlichen Angaben wurden gespeichert" in saved.text

        imprint = client.get("/imprint")
        privacy = client.get("/privacy")
        contact = client.get("/contact")
        assert "<script>" not in imprint.text
        assert "&lt;script&gt;" in imprint.text
        assert "<b>Nicht als HTML</b>" not in privacy.text
        assert "&lt;b&gt;Nicht als HTML&lt;/b&gt;" in privacy.text
        assert 'href="mailto:support@example.org"' in contact.text
        assert "Technische Fragen" in contact.text

    with Session(engine) as db:
        stored = db.get(PublicSiteContent, "de")
        assert stored is not None
        assert stored.contact_email == "support@example.org"
        event = db.scalar(
            select(AuditLog).where(AuditLog.event_type == "public_site_content_updated")
        )
        assert event is not None
        assert "support@example.org" not in event.technical_metadata
        assert "Beispielbetrieb" not in event.technical_metadata
