import html
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import engine
from app.config import get_settings
from app.main import app
from app.models import (
    Account,
    AccountReview,
    AuditLog,
    ContactMethod,
    ContactReview,
    ContactReviewToken,
    Partner,
    PublicSiteContent,
    SmtpConfiguration,
    SystemConfiguration,
    TrustedPerson,
    TrustedPersonToken,
)
from app.providers.base import DeliveryResult
from app.routers import admin, web
from app.security.core import FieldCipher, hash_password, keyed_hash
from app.security.core import SessionManager
from app.services import AccountService, ManagementService


def hidden_value(body: str, name: str) -> str:
    match = re.search(rf'name="{re.escape(name)}" value="([^"]+)"', body)
    assert match, f"hidden field {name!r} missing"
    return html.unescape(match.group(1))


def confirm_contact(client: TestClient, path: str):
    prompt = client.get(path)
    assert prompt.status_code == 200
    return client.post(path, data={"csrf": hidden_value(prompt.text, "csrf")})


def create_initial_confirmation(settings, email: str = "owner@example.org"):
    with Session(engine, expire_on_commit=False) as db:
        service = AccountService(settings, FieldCipher(settings.field_encryption_key))
        account, _, setup = service.create(db)
        _, token = service.setup(
            db, setup, "correct horse battery staple", email
        )
        return account.id, token


class RecordingEmailProvider:
    messages: list[tuple[str, str, str]] = []

    def __init__(self, settings):
        pass

    def send(
        self,
        recipient: str,
        subject: str,
        body: str,
        *,
        envelope_token: str | None = None,
    ) -> DeliveryResult:
        self.messages.append((recipient, subject, body))
        return DeliveryResult(True, message_id="test-message")


class PermanentlyRejectingEmailProvider:
    channel = "email"

    def send(
        self,
        recipient: str,
        subject: str,
        body: str,
        *,
        envelope_token: str | None = None,
    ) -> DeliveryResult:
        return DeliveryResult(
            False,
            permanent_failure=True,
            error_class="recipient_rejected",
        )


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
        help_page = client.get("/help")
        create = client.get("/account/create")
        admin = client.get("/admin/login")
    assert index.status_code == 200
    assert "Im richtigen Moment die richtigen Menschen erreichen" in index.text
    assert "Zur einfachen Erklärung" in index.text
    assert "Bei akuter Gefahr:" in index.text
    assert "Wählen Sie zuerst die örtliche Notrufnummer" in index.text
    assert "die hinterlegte Benachrichtigungsgruppe zu informieren" not in index.text
    assert 'href="/help?lang=de"' in index.text
    assert 'href="/help"' in index.text
    assert help_page.status_code == 200
    assert "Was ist SilentRelay?" in help_page.text
    assert "Die Rollen schließen einander nicht aus" in help_page.text
    assert "SilentRelay prüft weder die reale Identität" in help_page.text
    assert "Vor der PIN-Einrichtung" in help_page.text
    assert "E-Mail-Anbieter und die empfangenden Mailserver" in help_page.text
    assert "standardmäßig zehn Minuten" in help_page.text
    assert "noch nicht versendet wurde" in help_page.text
    assert create.status_code == 200
    assert 'name="csrf"' in create.text
    assert admin.status_code == 200
    assert "Admin-Anmeldung" in admin.text


def test_browser_language_controls_public_and_admin_pages():
    with TestClient(app) as client:
        index = client.get("/", headers={"Accept-Language": "en-GB,en;q=0.9,de;q=0.5"})
        help_page = client.get("/help", headers={"Accept-Language": "en"})
        create = client.get("/account/create", headers={"Accept-Language": "en"})
        admin_login = client.get("/admin/login", headers={"Accept-Language": "en"})
    assert '<html lang="en">' in index.text
    assert "Reach the right people at the right time" in index.text
    assert 'href="/help?lang=en"' in index.text
    assert '<html lang="en">' in help_page.text
    assert "What is SilentRelay?" in help_page.text
    assert "Before the PIN is set up" in help_page.text
    assert "does not provide legal recognition" in help_page.text
    assert "ten minutes by default" in help_page.text
    assert "it has not been sent yet" in help_page.text
    assert "Account language" in create.text
    assert "Admin sign-in" in admin_login.text


def test_unsupported_browser_language_uses_english_fallback():
    settings = get_settings()
    previous_language = settings.default_language
    settings.default_language = "en"
    try:
        with TestClient(app) as client:
            index = client.get("/", headers={"Accept-Language": "es-ES,es;q=0.9"})
            admin_login = client.get(
                "/admin/login", headers={"Accept-Language": "es-ES,es;q=0.9"}
            )
    finally:
        settings.default_language = previous_language
    assert '<html lang="en">' in index.text
    assert "Reach the right people at the right time" in index.text
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
        assert "System settings" in accounts.text
        assert 'href="/admin/accounts" aria-current="page"' in accounts.text
        system = client.get("/admin/system", headers=headers)
        assert "System settings" in system.text
        assert 'href="/admin/system" aria-current="page"' in system.text
        retention = client.get(
            "/admin/system/retention", headers=headers
        )
        assert "Periods and retention" in retention.text
        assert 'href="/admin/system" aria-current="page"' in retention.text
        email = client.get(
            "/admin/system/email", headers=headers
        )
        assert "SMTP server and sender" in email.text
        assert 'href="/admin/system" aria-current="page"' in email.text
        public = client.get("/admin/public-content?content_language=en", headers=headers)
        assert "Legal notice, privacy and contact details" in public.text
        assert "System settings" in public.text
        assert 'href="/admin/public-content" aria-current="page"' in public.text
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


@pytest.mark.parametrize("credential_change", ["password", "owner_link"])
def test_owner_credential_change_revokes_all_browser_sessions(credential_change):
    settings = get_settings()
    with Session(engine, expire_on_commit=False) as db:
        accounts = AccountService(
            settings, FieldCipher(settings.field_encryption_key)
        )
        account, owner_token, setup_token = accounts.create(db)
        _, verification = accounts.setup(
            db,
            setup_token,
            "correct horse battery staple",
            "owner@example.org",
        )
        accounts.verify_contact(db, verification)

    owner_path = f"/account/{owner_token}"
    with TestClient(app) as first, TestClient(app) as second:
        for client in (first, second):
            login = client.post(
                f"{owner_path}/login",
                data={"password": "correct horse battery staple"},
                follow_redirects=True,
            )
            assert login.status_code == 200
            assert "sr_account_owner" in client.cookies

        csrf = hidden_value(first.get("/account/dashboard").text, "csrf")
        if credential_change == "password":
            changed = first.post(
                "/account/password/change",
                data={
                    "csrf": csrf,
                    "current_password": "correct horse battery staple",
                    "new_password": "new correct horse battery staple",
                },
                follow_redirects=False,
            )
            assert changed.status_code == 303
            assert changed.headers["location"] == "/"
            new_owner_path = owner_path
            new_password = "new correct horse battery staple"
        else:
            changed = first.post(
                "/account/token/rotate",
                data={"csrf": csrf},
                follow_redirects=False,
            )
            assert changed.status_code == 200
            new_owner_url = html.unescape(
                re.search(
                    r'<p class="secret" id="personal-access-link">([^<]+)</p>',
                    changed.text,
                ).group(1)
            )
            new_owner_path = new_owner_url.removeprefix("http://testserver")
            new_password = "correct horse battery staple"
            assert "alle aktiven Kontoinhaber-Sitzungen" in changed.text
            assert "Mit neuem Zugang anmelden" in changed.text
            assert first.get(owner_path).status_code == 404

        assert "sr_account_owner" not in first.cookies
        assert "sr_account_owner_csrf" not in first.cookies
        assert second.get(
            "/account/dashboard", follow_redirects=False
        ).status_code == 303

        signed_in_again = first.post(
            f"{new_owner_path}/login",
            data={"password": new_password},
            follow_redirects=True,
        )
        assert signed_in_again.status_code == 200
        assert "Kontoverwaltung" in signed_in_again.text


def test_selected_account_language_controls_onboarding():
    with TestClient(app) as client:
        create = client.get("/account/create", headers={"Accept-Language": "de"})
        created = client.post(
            "/account/create",
            data={"csrf": hidden_value(create.text, "csrf"), "language_code": "en"},
        )
        assert "Save your account access now" in created.text
        setup_url = html.unescape(
            re.search(r'href="(http://testserver/account/setup/[^"]+)"', created.text).group(1)
        )
        setup = client.get(setup_url.removeprefix("http://testserver"), headers={"Accept-Language": "de"})
        assert "Secure your account" in setup.text
        assert '<html lang="en">' in setup.text

    with Session(engine) as db:
        account = db.scalar(select(Account))
        assert account.language_code == "en"


def test_production_setup_rejection_does_not_expose_verification_link(monkeypatch):
    settings = get_settings()
    previous_environment = settings.app_env
    settings.app_env = "production"
    monkeypatch.setattr(
        web,
        "load_email_provider",
        lambda db, settings, cipher: PermanentlyRejectingEmailProvider(),
    )
    try:
        with TestClient(app) as client:
            create_form = client.get("/account/create")
            created = client.post(
                "/account/create",
                data={"csrf": hidden_value(create_form.text, "csrf")},
            )
            setup_url = html.unescape(
                re.search(
                    r'href="(http://testserver/account/setup/[^"]+)"',
                    created.text,
                ).group(1)
            )
            setup_form = client.get(setup_url.removeprefix("http://testserver"))
            result = client.post(
                setup_url.removeprefix("http://testserver"),
                data={
                    "csrf": hidden_value(setup_form.text, "csrf"),
                    "password": "correct horse battery staple",
                    "email": "missing@example.org",
                },
            )
    finally:
        settings.app_env = previous_environment

    assert result.status_code == 200
    assert "als unbekannt abgelehnt" in result.text
    assert "/verify-contact/" not in result.text
    with Session(engine) as db:
        contact = db.scalar(select(ContactMethod))
        assert contact is not None
        assert not contact.is_verified
        assert contact.permanent_failure_count == 1
        assert contact.last_permanent_failure_at is not None


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


def test_trusted_access_requires_timely_pin_setup_and_locks_repeated_failures():
    settings = get_settings()
    cipher = FieldCipher(settings.field_encryption_key)
    with Session(engine, expire_on_commit=False) as db:
        accounts = AccountService(settings, cipher)
        account, _, setup_token = accounts.create(db)
        _, verification_token = accounts.setup(
            db, setup_token, "correct horse battery staple", "owner@example.org"
        )
        accounts.verify_contact(db, verification_token)
        person, access_token = ManagementService(settings, cipher).add_trusted_person(
            db, account.id, "account", account.id, "Trusted"
        )

    path = f"/notify/{access_token}"
    with TestClient(app) as client:
        setup_form = client.get(path)
        assert setup_form.status_code == 200
        assert "Einrichtung bis" in setup_form.text
        rejected = client.post(
            f"{path}/setup",
            data={
                "csrf": hidden_value(setup_form.text, "csrf"),
                "pin": "123456",
                "pin_confirm": "123456",
            },
        )
        assert rejected.status_code == 400
        assert "einfachen Zahlenfolge" in rejected.text
        enrolled = client.post(
            f"{path}/setup",
            data={
                "csrf": hidden_value(rejected.text, "csrf"),
                "pin": "472915",
                "pin_confirm": "472915",
            },
            follow_redirects=False,
        )
        assert enrolled.status_code == 303
        client.cookies.delete("sr_trusted_person")
        client.cookies.delete("sr_trusted_person_csrf")
        login_form = client.get(path)
        login_csrf = hidden_value(login_form.text, "csrf")
        for _ in range(5):
            failed = client.post(
                f"{path}/login", data={"csrf": login_csrf, "pin": "472916"}
            )
            assert failed.status_code == 401

    with Session(engine) as db:
        record = db.get(TrustedPersonToken, person.id)
        assert record.locked_until > datetime.utcnow()


def test_trusted_access_setup_expires_after_fourteen_days():
    settings = get_settings()
    cipher = FieldCipher(settings.field_encryption_key)
    with Session(engine, expire_on_commit=False) as db:
        accounts = AccountService(settings, cipher)
        account, _, setup_token = accounts.create(db)
        _, verification_token = accounts.setup(
            db, setup_token, "correct horse battery staple", "owner@example.org"
        )
        accounts.verify_contact(db, verification_token)
        person, access_token = ManagementService(settings, cipher).add_trusted_person(
            db, account.id, "account", account.id, "Trusted"
        )
        record = db.get(TrustedPersonToken, person.id)
        record.enrollment_expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()

    with TestClient(app) as client:
        expired = client.get(f"/notify/{access_token}")
    assert expired.status_code == 410
    assert "Zugang ist abgelaufen" in expired.text


def test_contact_confirmation_get_is_neutral_and_changes_no_domain_state():
    settings = get_settings()
    account_id, token = create_initial_confirmation(
        settings, "private-owner@example.org"
    )
    with Session(engine) as db:
        contact = db.scalar(select(ContactMethod))
        before = (
            contact.is_verified,
            contact.verified_at,
            contact.verification_token_hash,
            db.scalar(select(func.count()).select_from(AuditLog)),
            db.get(Account, account_id).status,
        )

    with TestClient(app) as client:
        response = client.get(f"/verify-contact/{token}")

    assert response.status_code == 200
    assert "Kontakt jetzt bestätigen" in response.text
    assert 'method="post"' in response.text
    assert 'name="csrf"' in response.text
    assert "private-owner@example.org" not in response.text
    assert "Empfänger" not in response.text
    assert "Partner" not in response.text
    with Session(engine) as db:
        contact = db.scalar(select(ContactMethod))
        after = (
            contact.is_verified,
            contact.verified_at,
            contact.verification_token_hash,
            db.scalar(select(func.count()).select_from(AuditLog)),
            db.get(Account, account_id).status,
        )
    assert after == before


def test_contact_confirmation_post_requires_session_bound_csrf():
    settings = get_settings()
    _, token = create_initial_confirmation(settings)
    path = f"/verify-contact/{token}"
    with TestClient(app) as client:
        prompt = client.get(path)
        assert client.post(path, data={}).status_code == 422
        assert client.post(path, data={"csrf": "wrong"}).status_code == 403
        with Session(engine) as db:
            assert not db.scalar(select(ContactMethod)).is_verified
        confirmed = client.post(
            path, data={"csrf": hidden_value(prompt.text, "csrf")}
        )
    assert confirmed.status_code == 200
    assert "E-Mail-Adresse ist jetzt bestätigt" in confirmed.text


def test_periodic_contact_confirmation_post_and_neutral_token_errors():
    settings = get_settings()
    account_id, initial_token = create_initial_confirmation(settings)
    with Session(engine, expire_on_commit=False) as db:
        service = AccountService(settings, FieldCipher(settings.field_encryption_key))
        assert service.verify_contact(db, initial_token)
        account = db.get(Account, account_id)
        contact = db.scalar(select(ContactMethod))
        expired_token = ManagementService(
            settings, FieldCipher(settings.field_encryption_key)
        ).add_contact(db, account_id, "account", account_id, "expired@example.org")
        expired_contact = db.scalar(select(ContactMethod).where(
            ContactMethod.verification_token_hash
            == keyed_hash(expired_token, settings.token_hmac_key)
        ))
        expired_contact.verification_expires_at = datetime.utcnow() - timedelta(seconds=1)
        review = db.scalar(select(AccountReview).where(
            AccountReview.account_id == account_id,
            AccountReview.confirmed_at.is_(None),
        ))
        review.review_due_at = datetime.utcnow() - timedelta(seconds=1)
        account.next_review_due_at = review.review_due_at
        contact_review = ContactReview(
            account_review_id=review.id,
            contact_method_id=contact.id,
            confirmation_due_at=datetime.utcnow() + timedelta(hours=1),
        )
        db.add(contact_review)
        db.flush()
        periodic_token = "periodic-http-token"
        db.add(ContactReviewToken(
            token_hash=keyed_hash(periodic_token, settings.token_hmac_key),
            contact_review_id=contact_review.id,
            expires_at=contact_review.confirmation_due_at,
        ))
        db.commit()

    with TestClient(app) as client:
        confirmed = confirm_contact(client, f"/verify-contact/{periodic_token}")
        reused = client.get(f"/verify-contact/{periodic_token}")
        expired = client.get(f"/verify-contact/{expired_token}")
        invalid = client.get("/verify-contact/invalid-token")
    assert confirmed.status_code == 200
    assert reused.status_code == expired.status_code == invalid.status_code == 404
    assert reused.text == expired.text == invalid.text
    with Session(engine) as db:
        assert db.scalar(select(ContactReview)).confirmed_at is not None


def test_parallel_contact_confirmation_posts_succeed_at_most_once():
    settings = get_settings()
    _, token = create_initial_confirmation(settings)
    path = f"/verify-contact/{token}"
    with TestClient(app) as seed_client:
        prompt = seed_client.get(path)
        csrf = hidden_value(prompt.text, "csrf")
        session_cookie = seed_client.cookies.get("sr_public")

    def submit() -> int:
        with TestClient(app) as client:
            client.cookies.set("sr_public", session_cookie)
            return client.post(path, data={"csrf": csrf}).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(lambda _: submit(), range(2)))
    assert statuses.count(200) == 1
    assert statuses.count(404) == 1
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.event_type == "contact_verified"
        )) == 1


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
        verification_url = next(
            line for line in RecordingEmailProvider.messages[-1][2].splitlines()
            if "/verify-contact/" in line
        )
        verified = confirm_contact(
            client, verification_url.removeprefix("http://testserver")
        )
        assert verified.status_code == 200
        assert "E-Mail-Adresse ist jetzt bestätigt" in verified.text
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
        partner_verification_url = next(
            line for line in RecordingEmailProvider.messages[-1][2].splitlines()
            if "/verify-contact/" in line
        )
        assert confirm_contact(
            client, partner_verification_url.removeprefix("http://testserver")
        ).status_code == 200

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
        dashboard = client.get("/account/dashboard")
        with Session(engine) as db:
            trusted_person_ids = list(db.scalars(select(TrustedPerson.id)))
        assert trusted_person_ids
        for trusted_person_id in trusted_person_ids:
            disable_path = f"/account/trusted-persons/{trusted_person_id}/disable"
            assert disable_path not in dashboard.text
            assert client.post(
                disable_path, data={"csrf": csrf}, follow_redirects=False
            ).status_code == 404
        notify_url = html.unescape(
            re.search(
                r'<p class="secret" id="personal-access-link">(http://testserver/notify/[^<]+)</p>',
                token_page.text,
            ).group(1)
        )

        notify_path = notify_url.removeprefix("http://testserver")
        pin_setup_form = client.get(notify_path)
        assert "Persönliche PIN einrichten" in pin_setup_form.text
        pin_setup = client.post(
            f"{notify_path}/setup",
            data={
                "csrf": hidden_value(pin_setup_form.text, "csrf"),
                "pin": "472915",
                "pin_confirm": "472915",
            },
            follow_redirects=True,
        )
        assert pin_setup.status_code == 200
        assert "Was ist passiert?" in pin_setup.text
        notify_form = pin_setup
        assert notify_form.status_code == 200
        notify_csrf = hidden_value(notify_form.text, "csrf")
        confirmation = client.post(notify_path, data={
            "csrf": notify_csrf,
            "message": "Dies ist eine vertrauliche Testnachricht.",
        })
        assert confirmation.status_code == 200
        assert "owner@example.org" not in confirmation.text
        submission = hidden_value(confirmation.text, "submission")
        message_count = len(RecordingEmailProvider.messages)
        success = client.post(
            f"{notify_path}/confirm",
            data={"csrf": notify_csrf, "submission": submission},
            follow_redirects=True,
        )
        assert success.status_code == 200
        assert "Nachricht vorgemerkt" in success.text
        assert "Die Nachricht wurde noch nicht versendet" in success.text
        assert "Nachricht widerrufen" in success.text
        assert len(RecordingEmailProvider.messages) == message_count
        cancel_action = re.search(
            rf'action="({re.escape(notify_path)}/notifications/[^\"]+/cancel)"',
            success.text,
        ).group(1)
        cancelled = client.post(
            cancel_action, data={"csrf": notify_csrf}, follow_redirects=True,
        )
        assert "Die Nachricht wird nicht versendet und ihr Inhalt wurde gelöscht" in cancelled.text
        assert len(RecordingEmailProvider.messages) == message_count

        dashboard = client.get("/account/dashboard")
        partner_disable_path = f"/account/partners/{partner_id}/disable"
        assert partner_disable_path not in dashboard.text
        assert client.post(
            partner_disable_path, data={"csrf": csrf}, follow_redirects=False
        ).status_code == 404
        with Session(engine) as db:
            partner_person_ids = list(db.scalars(select(TrustedPerson.id).where(
                TrustedPerson.owner_type == "partner",
                TrustedPerson.owner_id == partner_id,
            )))
        assert partner_person_ids

        deleted_partner = client.post(
            f"/account/partners/{partner_id}/delete",
            data={"csrf": csrf},
            follow_redirects=True,
        )
        assert deleted_partner.status_code == 200
        assert "Ausgeschlossener Partner" not in deleted_partner.text
        with Session(engine) as db:
            assert db.get(Partner, partner_id) is None
            assert not list(db.scalars(select(ContactMethod).where(
                ContactMethod.owner_type == "partner",
                ContactMethod.owner_id == partner_id,
            )))
            assert not list(db.scalars(select(TrustedPerson).where(
                TrustedPerson.owner_type == "partner",
                TrustedPerson.owner_id == partner_id,
            )))
            for trusted_person_id in partner_person_ids:
                assert db.get(TrustedPersonToken, trusted_person_id) is None

        monkeypatch.setattr(
            web,
            "load_email_provider",
            lambda db, settings, cipher: PermanentlyRejectingEmailProvider(),
        )
        rejected_contact = client.post(
            "/account/contacts",
            data={
                "csrf": csrf,
                "owner_type": "account",
                "value": "missing@example.org",
            },
            follow_redirects=True,
        )
        assert rejected_contact.status_code == 200
        assert "Der Mailserver hat die Adresse abgelehnt." in rejected_contact.text
        assert "Unzustellbar" in rejected_contact.text
        with Session(engine) as db:
            contact = db.scalar(select(ContactMethod).where(
                ContactMethod.owner_type == "account",
                ContactMethod.is_verified.is_(False),
                ContactMethod.permanent_failure_count == 1,
            ))
            assert contact is not None
            assert contact.last_permanent_failure_at is not None


def test_admin_can_configure_and_test_smtp(monkeypatch):
    settings = get_settings()
    settings.admin_password_hash = hash_password("admin demo password")
    provider = RecordingEmailProvider(None)
    monkeypatch.setattr(admin, "test_smtp_connection", lambda config: None)
    monkeypatch.setattr(admin, "test_imap_connection", lambda config: 0)
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
        assert "Systemeinstellungen" in system.text
        assert "Allgemeine Einstellungen" in system.text
        assert 'name="minutes" min="0" max="1440" value="10"' in system.text
        csrf = hidden_value(system.text, "csrf")

        delay = client.post(
            "/admin/system/notification-delay",
            data={"csrf": csrf, "minutes": "90"},
            follow_redirects=True,
        )
        assert "Wartezeit für neue Nachrichten wurde gespeichert" in delay.text

        account_creation = client.post(
            "/admin/system/account-creation",
            data={"csrf": csrf},
            follow_redirects=True,
        )
        assert "Einstellung zur Kontoerstellung wurde gespeichert" in (
            account_creation.text
        )

        retention = client.post(
            "/admin/system/retention",
            data={
                "csrf": csrf,
                "account_pending_retention_days": "8",
                "account_review_interval_days": "120",
                "reminder_day": ["30", "-3", "-3", "0", "", "", ""],
                "account_review_grace_days": "45",
                "contact_problem_reminder_days": "5",
                "account_retention_after_disable_days": "300",
                "message_retention_hours": "72",
                "audit_retention_days": "60",
            },
            follow_redirects=True,
        )
        assert "Fristen und Aufbewahrungswerte wurden gespeichert" in retention.text

        email_settings = client.get("/admin/system/email")
        assert email_settings.status_code == 200
        assert "SMTP-Server und Absender" in email_settings.text

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
        assert "vollständig von SilentRelay verwaltet" in saved.text

        ndr = client.post(
            "/admin/system/ndr",
            data={
                "csrf": csrf,
                "host": "imap.example.org",
                "port": "993",
                "username": "relay@example.org",
                "password": "imap-secret",
                "acknowledged_address": "relay@example.org",
            },
            follow_redirects=True,
        )
        assert "Verarbeitung von Zustellfehlern wurde aktiviert" in ndr.text
        assert "IMAP ohne Löschung testen" in ndr.text
        ndr_connection = client.post(
            "/admin/system/ndr/test-connection",
            data={"csrf": csrf},
            follow_redirects=True,
        )
        assert "keine Nachrichten gelesen oder gelöscht" in ndr_connection.text

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
        assert "Antworten werden nicht gelesen und automatisch gelöscht" in (
            provider.messages[-1][2]
        )
        logout = client.post(
            "/admin/logout", data={"csrf": csrf}, follow_redirects=True,
        )
        assert "Admin-Anmeldung" in logout.text
        assert client.get("/admin/accounts", follow_redirects=False).status_code == 303

    with Session(engine) as db:
        stored = db.get(SmtpConfiguration, "default")
        assert b"secret-password" not in stored.encrypted_password
        config = db.get(SystemConfiguration, "default")
        assert config.notification_delay_minutes == 90
        assert config.account_creation_enabled is False
        assert config.account_review_interval_days == 120
        assert config.account_review_reminder_days == "-3,0,30"
        assert config.message_retention_hours == 72


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
                "imprint_text": "# Beispielbetrieb\n\n**Verantwortlich**\n\n<script>alert('x')</script>",
                "privacy_text": "## Datenschutz\n\nKeine externen Tracker.\n\n<b>Nicht als HTML</b>",
                "contact_email": "support@example.org",
                "contact_text": "Technische Fragen bitte per [E-Mail](mailto:support@example.org).\n\n![Bild](https://example.org/image.png)",
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
        assert "<h2>Beispielbetrieb</h2>" in imprint.text
        assert "<strong>Verantwortlich</strong>" in imprint.text
        assert "<b>Nicht als HTML</b>" not in privacy.text
        assert "&lt;b&gt;Nicht als HTML&lt;/b&gt;" in privacy.text
        assert "<h3>Datenschutz</h3>" in privacy.text
        assert 'href="mailto:support@example.org"' in contact.text
        assert "Technische Fragen" in contact.text
        assert "<img" not in contact.text
        assert "![Bild]" in contact.text

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
