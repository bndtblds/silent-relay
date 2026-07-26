from __future__ import annotations

import base64
import io
from datetime import datetime, timedelta

import qrcode
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.dependencies import account_owner_account, account_owner_session
from app.models import (
    Account, AccountStatus, ContactMethod, Delivery, DeliveryStatus, Partner, ServerSession,
    TrustedPerson, TrustedPersonToken,
)
from app.providers.email import EmailNotificationProvider
from app.public_site import load_public_site_content
from app.security.core import FieldCipher, SessionManager, generate_token, hash_password, keyed_hash, verify_password
from app.services import AccountService, AuthenticationService, DeliveryService, ManagementService, NotificationService
from app.smtp_config import load_email_provider

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def context(request: Request, **values: object) -> dict[str, object]:
    return {"request": request, **values}


def csrf_guard(request: Request, session: ServerSession, settings: Settings, csrf: str) -> None:
    if not SessionManager(settings).verify_csrf(session, csrf):
        raise HTTPException(403, "Ungültiger CSRF-Token.")


def qr_data(url: str) -> str:
    image = qrcode.make(url)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return base64.b64encode(stream.getvalue()).decode()


def public_form_response(request: Request, db: Session, settings: Settings, template: str, **values: object):
    raw_session, raw_csrf = SessionManager(settings).create(db, "public")
    db.commit()
    response = templates.TemplateResponse(request, template, context(request, csrf=raw_csrf, **values))
    response.set_cookie("sr_public", raw_session, secure=settings.secure_cookies, httponly=True, samesite="strict", max_age=900)
    return response


def public_csrf_guard(request: Request, db: Session, settings: Settings, csrf: str) -> None:
    session = SessionManager(settings).resolve(db, request.cookies.get("sr_public"), "public")
    if not session or not SessionManager(settings).verify_csrf(session, csrf):
        raise HTTPException(403, "Ungültiger CSRF-Token.")


@router.get("/", response_class=HTMLResponse)
def index(request: Request, settings: Settings = Depends(get_settings)):
    return templates.TemplateResponse(request, "index.html", context(request, creation_enabled=settings.account_creation_enabled))


@router.get("/imprint", response_class=HTMLResponse)
def imprint(request: Request, db: Session = Depends(get_db)):
    content = load_public_site_content(db)
    return templates.TemplateResponse(request, "publiccontent.html", context(
        request,
        title="Impressum",
        body=content.imprint_text if content else "",
        missing_message="Das Impressum wurde vom Betreiber noch nicht hinterlegt.",
    ))


@router.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request, db: Session = Depends(get_db)):
    content = load_public_site_content(db)
    return templates.TemplateResponse(request, "publiccontent.html", context(
        request,
        title="Datenschutz",
        body=content.privacy_text if content else "",
        missing_message="Die Datenschutzhinweise wurden vom Betreiber noch nicht hinterlegt.",
    ))


@router.get("/contact", response_class=HTMLResponse)
def contact(request: Request, db: Session = Depends(get_db)):
    content = load_public_site_content(db)
    return templates.TemplateResponse(request, "publiccontent.html", context(
        request,
        title="Kontakt",
        body=content.contact_text if content else "",
        contact_email=content.contact_email if content else "",
        missing_message="Die Kontaktadresse wurde vom Betreiber noch nicht hinterlegt.",
    ))


@router.get("/account/create", response_class=HTMLResponse)
def create_form(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    if not settings.account_creation_enabled:
        raise HTTPException(404)
    return public_form_response(request, db, settings, "create.html")


@router.post("/account/create", response_class=HTMLResponse)
def create_account(request: Request, csrf: str = Form(...), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    public_csrf_guard(request, db, settings, csrf)
    account, account_owner_token, setup_token = AccountService(settings, FieldCipher(settings.field_encryption_key)).create(db)
    account_owner_url = f"{settings.app_base_url}/account/{account_owner_token}"
    setup_url = f"{settings.app_base_url}/account/setup/{setup_token}"
    return templates.TemplateResponse(request, "created.html", context(
        request, account_owner_url=account_owner_url, setup_url=setup_url, qr=qr_data(account_owner_url)
    ))


@router.get("/account/setup/{token}", response_class=HTMLResponse)
def setup_form(token: str, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    return public_form_response(request, db, settings, "setup.html", token=token)


@router.post("/account/setup/{token}", response_class=HTMLResponse)
def setup_account(
    token: str, request: Request, password: str = Form(...), email: str = Form(...), csrf: str = Form(...),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    public_csrf_guard(request, db, settings, csrf)
    try:
        _, verification = AccountService(settings, FieldCipher(settings.field_encryption_key)).setup(db, token, password, email)
    except (LookupError, ValueError) as exc:
        return templates.TemplateResponse(request, "setup.html", context(request, token=token, csrf=csrf, error=str(exc)), status_code=400)
    verify_url = f"{settings.app_base_url}/verify-contact/{verification}"
    provider = load_email_provider(db, settings, FieldCipher(settings.field_encryption_key))
    result = provider.send(email.strip(), "SilentRelay: Kontakt bestätigen", f"Bestätigen Sie Ihren Kontakt:\n\n{verify_url}")
    return templates.TemplateResponse(request, "setup_done.html", context(request, mail_sent=result.successful, verify_url=verify_url))


@router.get("/verify-contact/{token}", response_class=HTMLResponse)
def verify_contact(token: str, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    if not AccountService(settings, FieldCipher(settings.field_encryption_key)).verify_contact(db, token):
        raise HTTPException(404)
    return templates.TemplateResponse(request, "message.html", context(
        request,
        title="Kontakt bestätigt",
        message="Das Konto ist jetzt aktiv.",
        next_step="Öffnen Sie jetzt Ihren zuvor gespeicherten Kontoinhaber-Link oder scannen Sie den Kontoinhaber-QR-Code, um sich anzumelden.",
    ))


def account_owner_login_form(token: str, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    if not AuthenticationService(settings).credential_for_token(db, token):
        raise HTTPException(404)
    return templates.TemplateResponse(request, "login.html", context(request, token=token))


def account_owner_login(
    token: str, password: str = Form(...), db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    account = AuthenticationService(settings).login(db, token, password)
    if not account or account.is_admin_locked:
        raise HTTPException(401, "Anmeldung fehlgeschlagen.")
    raw_session, raw_csrf = SessionManager(settings).create(db, "account_owner", account.id)
    db.commit()
    response = RedirectResponse("/account/dashboard", 303)
    response.set_cookie("sr_account_owner", raw_session, secure=settings.secure_cookies, httponly=True, samesite="strict", max_age=settings.session_ttl_minutes * 60)
    response.set_cookie("sr_account_owner_csrf", raw_csrf, secure=settings.secure_cookies, httponly=True, samesite="strict", max_age=settings.session_ttl_minutes * 60)
    return response


@router.get("/account/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request, account: Account = Depends(account_owner_account), session: ServerSession = Depends(account_owner_session),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    cipher = FieldCipher(settings.field_encryption_key)
    contacts = list(db.scalars(select(ContactMethod).where(ContactMethod.account_id == account.id, ContactMethod.owner_type == "account")))
    partners = list(db.scalars(select(Partner).where(Partner.account_id == account.id)))
    owner_people = list(db.scalars(select(TrustedPerson).where(
        TrustedPerson.account_id == account.id,
        TrustedPerson.owner_type == "account",
        TrustedPerson.owner_id == account.id,
    )))
    notification_service = NotificationService(settings, cipher)
    partner_rows = []
    for partner in partners:
        people = list(db.scalars(select(TrustedPerson).where(
            TrustedPerson.account_id == account.id,
            TrustedPerson.owner_type == "partner",
            TrustedPerson.owner_id == partner.id,
        )))
        partner_contacts = list(db.scalars(select(ContactMethod).where(
            ContactMethod.account_id == account.id, ContactMethod.owner_type == "partner", ContactMethod.owner_id == partner.id
        )))
        partner_rows.append({"id": partner.id, "name": cipher.decrypt(partner.encrypted_name), "active": partner.is_active, "contacts": [
            {"id": c.id, "value": cipher.decrypt(c.encrypted_value), "verified": c.is_verified} for c in partner_contacts
        ], "people": [
            {
                "id": p.id,
                "name": cipher.decrypt(p.encrypted_display_name) if p.encrypted_display_name else "",
                "active": p.is_active,
                "ready": p.is_active and bool(notification_service.eligible_contacts(db, p)),
            }
            for p in people
        ]})
    failures = db.scalar(select(func.count()).select_from(Delivery).join(ContactMethod).where(
        ContactMethod.account_id == account.id, Delivery.status == DeliveryStatus.permanent_failure
    ))
    trusted_person_count = len(owner_people) + sum(len(partner["people"]) for partner in partner_rows)
    verified_partner_contact_count = sum(
        sum(1 for contact in partner["contacts"] if contact["verified"]) for partner in partner_rows
    )
    return templates.TemplateResponse(request, "dashboard.html", context(
        request, account=account, contacts=[{"id": c.id, "value": cipher.decrypt(c.encrypted_value), "verified": c.is_verified} for c in contacts],
        partners=partner_rows,
        owner_people=[
            {
                "id": person.id,
                "name": cipher.decrypt(person.encrypted_display_name) if person.encrypted_display_name else "",
                "active": person.is_active,
                "ready": person.is_active and bool(notification_service.eligible_contacts(db, person)),
            }
            for person in owner_people
        ],
        failures=failures, csrf=request.cookies.get("sr_account_owner_csrf", ""),
        status_label={
            "pending_verification": "Bestätigung ausstehend",
            "active": "Aktiv",
            "overdue": "Prüfung überfällig",
            "disabled": "Deaktiviert",
            "scheduled_for_deletion": "Zur Löschung vorgemerkt",
            "deleted": "Gelöscht",
        }.get(account.status.value, account.status.value),
        setup_steps={
            "owner_contact": any(contact.is_verified for contact in contacts),
            "partner": bool(partners),
            "partner_contact": verified_partner_contact_count > 0,
            "trusted_person": trusted_person_count > 0,
            "ready": (
                any(contact.is_verified for contact in contacts)
                and bool(partners)
                and verified_partner_contact_count > 0
                and (
                    any(
                        person.is_active and bool(notification_service.eligible_contacts(db, person))
                        for person in owner_people
                    )
                    or any(
                        person["ready"] for partner in partner_rows for person in partner["people"]
                    )
                )
            ),
        },
    ))


@router.post("/account/logout")
def logout(
    request: Request, csrf: str = Form(...), session: ServerSession = Depends(account_owner_session),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    SessionManager(settings).revoke(db, request.cookies.get("sr_account_owner"))
    db.commit()
    response = RedirectResponse("/", 303)
    response.delete_cookie("sr_account_owner")
    response.delete_cookie("sr_account_owner_csrf")
    return response


@router.post("/account/contacts")
def add_contact(
    request: Request, value: str = Form(...), owner_type: str = Form("account"), owner_id: str | None = Form(None),
    csrf: str = Form(...), account: Account = Depends(account_owner_account),
    session: ServerSession = Depends(account_owner_session), db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    target_id = account.id
    if owner_type == "partner":
        partner = db.scalar(select(Partner).where(Partner.id == owner_id, Partner.account_id == account.id))
        if not partner:
            raise HTTPException(404)
        target_id = partner.id
    elif owner_type != "account":
        raise HTTPException(400)
    token = ManagementService(settings, FieldCipher(settings.field_encryption_key)).add_contact(
        db, account.id, owner_type, target_id, value
    )
    verify_url = f"{settings.app_base_url}/verify-contact/{token}"
    load_email_provider(db, settings, FieldCipher(settings.field_encryption_key)).send(
        value, "SilentRelay: Kontakt bestätigen", f"Bestätigen Sie Ihren Kontakt:\n\n{verify_url}"
    )
    return RedirectResponse("/account/dashboard", 303)


@router.post("/account/contacts/{contact_id}/verify")
def resend_contact_verification(
    contact_id: str, request: Request, csrf: str = Form(...), account: Account = Depends(account_owner_account),
    session: ServerSession = Depends(account_owner_session), db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    contact = db.scalar(select(ContactMethod).where(ContactMethod.id == contact_id, ContactMethod.account_id == account.id))
    if not contact:
        raise HTTPException(404)
    token = generate_token()
    contact.verification_token_hash = keyed_hash(token, settings.token_hmac_key)
    contact.verification_expires_at = datetime.utcnow() + timedelta(hours=24)
    contact.is_verified = False
    db.commit()
    value = FieldCipher(settings.field_encryption_key).decrypt(contact.encrypted_value)
    verify_url = f"{settings.app_base_url}/verify-contact/{token}"
    load_email_provider(db, settings, FieldCipher(settings.field_encryption_key)).send(
        value, "SilentRelay: Kontakt bestätigen", f"Bestätigen Sie Ihren Kontakt:\n\n{verify_url}"
    )
    return RedirectResponse("/account/dashboard", 303)


@router.get("/account/contacts")
@router.get("/account/partners")
@router.get("/account/review")
def account_owner_section_redirect(account: Account = Depends(account_owner_account)):
    return RedirectResponse("/account/dashboard", 303)


@router.post("/account/contacts/{contact_id}/delete")
def delete_contact(
    contact_id: str, request: Request, csrf: str = Form(...), account: Account = Depends(account_owner_account),
    session: ServerSession = Depends(account_owner_session), db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    db.execute(delete(ContactMethod).where(ContactMethod.id == contact_id, ContactMethod.account_id == account.id))
    db.commit()
    return RedirectResponse("/account/dashboard", 303)


@router.post("/account/partners")
def add_partner(
    request: Request, name: str = Form(...), csrf: str = Form(...), account: Account = Depends(account_owner_account),
    session: ServerSession = Depends(account_owner_session), db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    ManagementService(settings, FieldCipher(settings.field_encryption_key)).add_partner(db, account.id, name)
    return RedirectResponse("/account/dashboard", 303)


@router.post("/account/partners/{partner_id}/edit")
def edit_partner(
    partner_id: str, request: Request, name: str = Form(...), csrf: str = Form(...),
    account: Account = Depends(account_owner_account), session: ServerSession = Depends(account_owner_session),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    partner = db.scalar(select(Partner).where(Partner.id == partner_id, Partner.account_id == account.id))
    if not partner:
        raise HTTPException(404)
    partner.encrypted_name = FieldCipher(settings.field_encryption_key).encrypt(name.strip())
    db.commit()
    return RedirectResponse("/account/dashboard", 303)


@router.post("/account/partners/{partner_id}/disable")
def disable_partner(
    partner_id: str, request: Request, csrf: str = Form(...), account: Account = Depends(account_owner_account),
    session: ServerSession = Depends(account_owner_session), db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    partner = db.scalar(select(Partner).where(Partner.id == partner_id, Partner.account_id == account.id))
    if not partner:
        raise HTTPException(404)
    partner.is_active = False
    people = list(db.scalars(select(TrustedPerson).where(
        TrustedPerson.account_id == account.id,
        TrustedPerson.owner_type == "partner",
        TrustedPerson.owner_id == partner.id,
    )))
    for person in people:
        person.is_active = False
        token = db.get(TrustedPersonToken, person.id)
        if token:
            token.revoked_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/account/dashboard", 303)


@router.post("/account/partners/{partner_id}/delete")
def delete_partner(
    partner_id: str, request: Request, csrf: str = Form(...), account: Account = Depends(account_owner_account),
    session: ServerSession = Depends(account_owner_session), db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    partner = db.scalar(select(Partner).where(Partner.id == partner_id, Partner.account_id == account.id))
    if not partner:
        raise HTTPException(404)
    for person in db.scalars(select(TrustedPerson).where(
        TrustedPerson.account_id == account.id,
        TrustedPerson.owner_type == "partner",
        TrustedPerson.owner_id == partner.id,
    )):
        db.delete(person)
    db.execute(delete(ContactMethod).where(
        ContactMethod.account_id == account.id, ContactMethod.owner_type == "partner", ContactMethod.owner_id == partner.id
    ))
    db.delete(partner)
    db.commit()
    return RedirectResponse("/account/dashboard", 303)


@router.post("/account/partners/{partner_id}/trusted-persons", response_class=HTMLResponse)
def add_person(
    partner_id: str, request: Request, name: str = Form(""), csrf: str = Form(...), account: Account = Depends(account_owner_account),
    session: ServerSession = Depends(account_owner_session), db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    _, token = ManagementService(settings, FieldCipher(settings.field_encryption_key)).add_trusted_person(
        db, account.id, "partner", partner_id, name
    )
    url = f"{settings.app_base_url}/notify/{token}"
    return templates.TemplateResponse(request, "token.html", context(request, title="QR-Code der Vertrauensperson", url=url, qr=qr_data(url)))


@router.post("/account/trusted-persons", response_class=HTMLResponse)
def add_owner_person(
    request: Request, name: str = Form(""), csrf: str = Form(...),
    account: Account = Depends(account_owner_account), session: ServerSession = Depends(account_owner_session),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    _, token = ManagementService(settings, FieldCipher(settings.field_encryption_key)).add_trusted_person(
        db, account.id, "account", account.id, name
    )
    url = f"{settings.app_base_url}/notify/{token}"
    return templates.TemplateResponse(request, "token.html", context(
        request, title="QR-Code Ihrer Vertrauensperson", url=url, qr=qr_data(url)
    ))


@router.post("/account/trusted-persons/{person_id}/rotate-token", response_class=HTMLResponse)
def rotate_person(
    person_id: str, request: Request, csrf: str = Form(...), account: Account = Depends(account_owner_account),
    session: ServerSession = Depends(account_owner_session), db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    token = ManagementService(settings, FieldCipher(settings.field_encryption_key)).rotate_trusted_token(db, account.id, person_id)
    url = f"{settings.app_base_url}/notify/{token}"
    return templates.TemplateResponse(request, "token.html", context(request, title="Neuer QR-Code", url=url, qr=qr_data(url)))


def owned_person(db: Session, account_id: str, person_id: str) -> TrustedPerson | None:
    return db.scalar(select(TrustedPerson).where(
        TrustedPerson.id == person_id, TrustedPerson.account_id == account_id
    ))


@router.post("/account/trusted-persons/{person_id}/disable")
def disable_person(
    person_id: str, request: Request, csrf: str = Form(...), account: Account = Depends(account_owner_account),
    session: ServerSession = Depends(account_owner_session), db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    person = owned_person(db, account.id, person_id)
    if not person:
        raise HTTPException(404)
    person.is_active = False
    token = db.get(TrustedPersonToken, person.id)
    if token:
        token.revoked_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/account/dashboard", 303)


@router.post("/account/trusted-persons/{person_id}/delete")
def delete_person(
    person_id: str, request: Request, csrf: str = Form(...), account: Account = Depends(account_owner_account),
    session: ServerSession = Depends(account_owner_session), db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    person = owned_person(db, account.id, person_id)
    if not person:
        raise HTTPException(404)
    db.delete(person)
    db.commit()
    return RedirectResponse("/account/dashboard", 303)


@router.post("/account/review/confirm")
def confirm_review(
    request: Request, csrf: str = Form(...), account: Account = Depends(account_owner_account), session: ServerSession = Depends(account_owner_session),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    AccountService(settings, FieldCipher(settings.field_encryption_key)).confirm_review(db, account)
    return RedirectResponse("/account/dashboard", 303)


@router.post("/account/token/rotate", response_class=HTMLResponse)
def rotate_account_owner(
    request: Request, csrf: str = Form(...), account: Account = Depends(account_owner_account), session: ServerSession = Depends(account_owner_session),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    token = AuthenticationService(settings).rotate_account_owner_token(db, account.id)
    url = f"{settings.app_base_url}/account/{token}"
    return templates.TemplateResponse(request, "token.html", context(request, title="Neuer Kontoinhaber-Zugang", url=url, qr=qr_data(url), recovery=True))


@router.post("/account/password/change")
def change_password(
    request: Request, current_password: str = Form(...), new_password: str = Form(...), csrf: str = Form(...),
    account: Account = Depends(account_owner_account), session: ServerSession = Depends(account_owner_session),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    if not verify_password(account.credential.password_hash, current_password):
        raise HTTPException(401, "Anmeldung fehlgeschlagen.")
    account.credential.password_hash = hash_password(new_password)
    account.credential.password_changed_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/account/dashboard", 303)


@router.post("/account/delete")
def delete_account(
    request: Request, password: str = Form(...), csrf: str = Form(...), account: Account = Depends(account_owner_account),
    session: ServerSession = Depends(account_owner_session), db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    if not verify_password(account.credential.password_hash, password):
        raise HTTPException(401, "Anmeldung fehlgeschlagen.")
    db.delete(account)
    db.commit()
    response = RedirectResponse("/", 303)
    response.delete_cookie("sr_account_owner")
    response.delete_cookie("sr_account_owner_csrf")
    return response


# Dynamic secret-bearing account-owner routes must be registered after every
# static /account route, otherwise values such as "dashboard" are consumed as tokens.
router.add_api_route("/account/{token}", account_owner_login_form, methods=["GET"], response_class=HTMLResponse)
router.add_api_route("/account/{token}/login", account_owner_login, methods=["POST"])


@router.get("/notify/{token}", response_class=HTMLResponse)
def notify_form(token: str, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    if not NotificationService(settings, FieldCipher(settings.field_encryption_key)).resolve_person(db, token):
        raise HTTPException(404)
    db.commit()
    return public_form_response(request, db, settings, "notify.html", token=token)


@router.post("/notify/{token}", response_class=HTMLResponse)
def notify_stage(
    token: str, request: Request, message: str = Form(...), csrf: str = Form(...),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    public_csrf_guard(request, db, settings, csrf)
    service = NotificationService(settings, FieldCipher(settings.field_encryption_key))
    person = service.resolve_person(db, token)
    if not person:
        raise HTTPException(404)
    try:
        submission = service.stage(db, person, message)
    except ValueError as exc:
        return templates.TemplateResponse(request, "notify.html", context(request, token=token, csrf=csrf, error=str(exc)), status_code=400)
    return templates.TemplateResponse(request, "confirm.html", context(request, token=token, submission=submission, message=message, csrf=csrf))


@router.post("/notify/{token}/confirm")
def notify_confirm(
    token: str, request: Request, submission: str = Form(...), csrf: str = Form(...),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    public_csrf_guard(request, db, settings, csrf)
    service = NotificationService(settings, FieldCipher(settings.field_encryption_key))
    if not service.resolve_person(db, token):
        raise HTTPException(404)
    try:
        service.accept(db, submission)
    except LookupError:
        raise HTTPException(409, "Diese Nachricht wurde bereits verarbeitet oder ist abgelaufen.")
    cipher = FieldCipher(settings.field_encryption_key)
    DeliveryService(settings, cipher, {"email": load_email_provider(db, settings, cipher)}).process_due(db)
    return RedirectResponse("/notification/success", 303)


@router.get("/notification/success", response_class=HTMLResponse)
def success(request: Request):
    return templates.TemplateResponse(request, "message.html", context(
        request,
        title="Nachricht übermittelt",
        message="Die vertrauliche Nachricht wurde zur Zustellung angenommen.",
        next_step="Sie müssen nichts weiter tun. Schließen Sie diese Seite, damit der persönliche Zugangslink nicht offen bleibt.",
    ))


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nDisallow: /\n"
