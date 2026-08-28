from __future__ import annotations

import base64
import io
from datetime import UTC, timedelta

import qrcode
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.dependencies import account_owner_account, account_owner_session
from app.email_tracking import send_tracked_email
from app.entitlements import (
    AccountCreatedContext,
    EntitlementProviderUnavailableError,
    RegistrationContext,
    RegistrationDecision,
    notify_account_created,
    registration_policy,
)
from app.i18n import (
    LANGUAGE_LABELS, SUPPORTED_LANGUAGES, browser_language, email_body, format_date, format_datetime,
    normalize_language, translate,
)
from app.models import (
    Account, AccountOwnerCredential, AccountReview, AccountStatus, ContactMethod,
    ContactReview, Partner, PartnerCredential, ServerSession,
    TrustedPerson, TrustedPersonToken,
)
from app.providers.email import EmailNotificationProvider
from app.public_markdown import render_public_markdown
from app.public_site import load_public_site_content_with_fallback
from app.security.core import (
    FieldCipher, SessionManager, generate_token, hash_pin,
    keyed_hash, verify_password, verify_pin,
)
from app.services import (
    AccountService, AuthenticationService, DeliveryService, InboxService,
    ManagementService, NotificationService, PartnerAuthenticationService,
)
from app.smtp_config import load_email_provider
from app.system_config import notification_delay_minutes, system_configuration
from app.time import utc_now

from app.routers.web.shared import (
    context,
    csrf_guard,
    inbox_rows,
    partner_session,
    public_csrf_guard,
    public_form_response,
    qr_data,
    set_partner_session_cookies,
    set_trusted_session_cookies,
    templates,
    trusted_csrf_guard,
    trusted_session,
)

router = APIRouter()

def account_owner_login_form(token: str, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    credential = AuthenticationService(settings).credential_for_token(db, token)
    if not credential:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request, "login.html", context(request, credential.account.language_code, token=token)
    )


def account_owner_login(
    token: str, request: Request, password: str = Form(...),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    account = AuthenticationService(settings).login(db, token, password)
    if not account or account.is_admin_locked:
        credential = AuthenticationService(settings).credential_for_token(db, token)
        language = credential.account.language_code if credential else settings.default_language
        raise HTTPException(401, translate(language, "error.login"))
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
    all_contacts = list(db.scalars(select(ContactMethod).where(
        ContactMethod.account_id == account.id
    )))
    contacts = [contact for contact in all_contacts if contact.owner_type == "account"]
    partners = list(db.scalars(select(Partner).where(Partner.account_id == account.id)))
    all_people = list(db.scalars(select(TrustedPerson).where(
        TrustedPerson.account_id == account.id
    )))
    owner_people = [
        person for person in all_people
        if person.owner_type == "account" and person.owner_id == account.id
    ]
    partner_credentials = {
        credential.partner_id: credential
        for credential in db.scalars(select(PartnerCredential).where(
            PartnerCredential.partner_id.in_([partner.id for partner in partners])
        ))
    } if partners else {}
    token_records = {
        item.trusted_person_id: item
        for item in db.scalars(select(TrustedPersonToken).join(TrustedPerson).where(
            TrustedPerson.account_id == account.id
        ))
    }
    now = utc_now()

    def person_row(person: TrustedPerson) -> dict[str, object]:
        token_record = token_records.get(person.id)
        enrolled = bool(token_record and token_record.pin_hash and token_record.enrolled_at)
        expired = bool(
            token_record and not enrolled
            and token_record.enrollment_expires_at <= now
        )
        return {
            "id": person.id,
            "name": cipher.decrypt(person.encrypted_display_name) if person.encrypted_display_name else "",
            "active": person.is_active,
            "enrolled": enrolled,
            "expired": expired,
            "enrollment_deadline": (
                format_date(token_record.enrollment_expires_at, account.language_code)
                if token_record and not enrolled else ""
            ),
            "ready": (
                person.is_active and enrolled
            ),
        }
    current_review = db.scalar(select(AccountReview).where(
        AccountReview.account_id == account.id,
        AccountReview.confirmed_at.is_(None),
    ).order_by(AccountReview.review_due_at.desc()))
    contact_review_rows = (
        list(db.scalars(select(ContactReview).where(
            ContactReview.account_review_id == current_review.id
        )))
        if current_review
        else []
    )
    contact_reviews = {
        row.contact_method_id: row for row in contact_review_rows
    }

    def contact_row(contact: ContactMethod) -> dict[str, object]:
        review = contact_reviews.get(contact.id)
        if contact.last_permanent_failure_at and not contact.is_verified:
            state = "undeliverable"
        elif contact.last_review_expired_at and not contact.is_verified:
            state = "expired"
        elif review and not review.confirmed_at:
            state = "review_pending"
        elif contact.is_verified:
            state = "confirmed"
        else:
            state = "initial_pending"
        return {
            "id": contact.id,
            "value": cipher.decrypt(contact.encrypted_value),
            "verified": contact.is_verified,
            "delivery_failed": bool(contact.last_permanent_failure_at),
            "review_expired": bool(contact.last_review_expired_at),
            "state": state,
        }

    partner_rows = []
    for partner in partners:
        partner_credential = partner_credentials.get(partner.id)
        people = [
            person for person in all_people
            if person.owner_type == "partner" and person.owner_id == partner.id
        ]
        partner_contacts = [
            contact for contact in all_contacts
            if contact.owner_type == "partner" and contact.owner_id == partner.id
        ]
        partner_rows.append({"id": partner.id, "name": cipher.decrypt(partner.encrypted_name), "active": partner.is_active,
            "access_enrolled": bool(partner_credential and partner_credential.enrolled_at and partner_credential.password_hash),
            "access_expired": bool(partner_credential and not partner_credential.enrolled_at and partner_credential.enrollment_expires_at <= now),
            "access_deadline": format_date(partner_credential.enrollment_expires_at, account.language_code) if partner_credential and not partner_credential.enrolled_at else "",
            "contacts": [
            contact_row(c) for c in partner_contacts
        ], "people": [person_row(p) for p in people]})
    failures = db.scalar(select(func.count()).select_from(ContactMethod).where(
        ContactMethod.account_id == account.id,
        ContactMethod.last_permanent_failure_at.is_not(None),
        ContactMethod.is_verified.is_(False),
    ))
    trusted_person_count = len(owner_people) + sum(len(partner["people"]) for partner in partner_rows)
    verified_partner_contact_count = sum(
        sum(1 for contact in partner["contacts"] if contact["verified"]) for partner in partner_rows
    )
    dashboard_messages = InboxService(cipher).messages(
        db, "account", account.id
    )
    return templates.TemplateResponse(
        request, "dashboard_en.html" if account.language_code == "en" else "dashboard.html",
        context(
        request, account.language_code, account=account,
        next_review_date=(
            translate(account.language_code, "dashboard.after_activation")
            if not account.next_review_due_at
            else format_date(account.next_review_due_at, account.language_code)
        ),
        owner_name=(cipher.decrypt(account.encrypted_owner_name) if account.encrypted_owner_name else ""),
        contacts=[contact_row(c) for c in contacts],
        partners=partner_rows,
        owner_people=[person_row(person) for person in owner_people],
        failures=failures, csrf=request.cookies.get("sr_account_owner_csrf", ""),
        inbox_available=bool(dashboard_messages),
        inbox_unread=any(
            recipient.read_at is None for recipient, _ in dashboard_messages
        ),
        owner_contact_count=sum(1 for contact in contacts if contact.is_verified),
        review={
            "due": bool(
                current_review
                and current_review.review_due_at <= utc_now()
            ),
            "details_confirmed": bool(
                current_review and current_review.details_confirmed_at
            ),
            "total": len(contact_review_rows),
            "confirmed": sum(
                1 for row in contact_review_rows if row.confirmed_at
            ),
            "pending": sum(
                1 for row in contact_review_rows if not row.confirmed_at
            ),
        },
        status_label=translate(account.language_code, f"status.{account.status.value}"),
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
                        person_row(person)["ready"]
                        for person in owner_people
                    )
                    or any(
                        person["ready"] for partner in partner_rows for person in partner["people"]
                    )
                )
            ),
        },
    ))


@router.post("/account/language")
def change_account_language(
    request: Request,
    language_code: str = Form(...),
    csrf: str = Form(...),
    account: Account = Depends(account_owner_account),
    session: ServerSession = Depends(account_owner_session),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    account.language_code = normalize_language(language_code, account.language_code)
    db.commit()
    return RedirectResponse("/account/dashboard", 303)


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
    cipher = FieldCipher(settings.field_encryption_key)
    try:
        token = ManagementService(settings, cipher).add_contact(
            db, account.id, owner_type, target_id, value
        )
    except ValueError:
        return RedirectResponse("/account/dashboard?contact_delivery=invalid", 303)
    verify_url = f"{settings.app_base_url}/verify-contact/{token}"
    contact = db.scalar(select(ContactMethod).where(
        ContactMethod.account_id == account.id,
        ContactMethod.verification_token_hash == keyed_hash(token, settings.token_hmac_key),
    ))
    normalized_value = cipher.decrypt(contact.encrypted_value) if contact else value
    result = send_tracked_email(
        db, settings, cipher, load_email_provider(db, settings, cipher),
        normalized_value,
        translate(account.language_code, "email.verify_subject"),
        email_body(
            account.language_code, "email.verify_body", url=verify_url,
            privacy_url=f"{settings.app_base_url}/privacy",
        ),
        contact_method_id=contact.id if contact else None,
    )
    db.commit()
    if result.permanent_failure:
        return RedirectResponse(
            "/account/dashboard?contact_delivery=permanent_failure", 303
        )
    if not result.successful:
        return RedirectResponse(
            "/account/dashboard?contact_delivery=temporary_failure", 303
        )
    return RedirectResponse("/account/dashboard", 303)


@router.post("/account/name")
def change_account_owner_name(
    request: Request, name: str = Form(...), csrf: str = Form(...),
    account: Account = Depends(account_owner_account),
    session: ServerSession = Depends(account_owner_session),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    try:
        ManagementService(settings, FieldCipher(settings.field_encryption_key)).update_owner_name(
            db, account, name
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
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
    contact.verification_expires_at = utc_now() + timedelta(hours=24)
    contact.is_verified = False
    db.commit()
    value = FieldCipher(settings.field_encryption_key).decrypt(contact.encrypted_value)
    verify_url = f"{settings.app_base_url}/verify-contact/{token}"
    cipher = FieldCipher(settings.field_encryption_key)
    result = send_tracked_email(
        db, settings, cipher, load_email_provider(db, settings, cipher),
        value,
        translate(account.language_code, "email.verify_subject"),
        email_body(
            account.language_code, "email.verify_body", url=verify_url,
            privacy_url=f"{settings.app_base_url}/privacy",
        ),
        contact_method_id=contact.id,
    )
    db.commit()
    if result.permanent_failure:
        return RedirectResponse(
            "/account/dashboard?contact_delivery=permanent_failure", 303
        )
    if not result.successful:
        return RedirectResponse(
            "/account/dashboard?contact_delivery=temporary_failure", 303
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
    db.flush()
    AccountService(
        settings, FieldCipher(settings.field_encryption_key)
    ).finish_current_review_if_complete(db, account)
    return RedirectResponse("/account/dashboard", 303)


@router.post("/account/partners")
def add_partner(
    request: Request, name: str = Form(...), csrf: str = Form(...), account: Account = Depends(account_owner_account),
    session: ServerSession = Depends(account_owner_session), db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    _, token = ManagementService(settings, FieldCipher(settings.field_encryption_key)).add_partner_with_access(db, account.id, name)
    url = f"{settings.app_base_url}/partner/access/{token}"
    return templates.TemplateResponse(request, "token.html", context(
        request, account.language_code, title=translate(account.language_code, "token.partner_title"),
        url=url, qr=qr_data(url), partner_access=True,
    ))


@router.post("/account/partners/{partner_id}/rotate-access", response_class=HTMLResponse)
def rotate_partner_access(
    partner_id: str, request: Request, csrf: str = Form(...),
    account: Account = Depends(account_owner_account), session: ServerSession = Depends(account_owner_session),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    token = ManagementService(settings, FieldCipher(settings.field_encryption_key)).rotate_partner_access(db, account.id, partner_id)
    url = f"{settings.app_base_url}/partner/access/{token}"
    return templates.TemplateResponse(request, "token.html", context(
        request, account.language_code, title=translate(account.language_code, "token.partner_new_title"),
        url=url, qr=qr_data(url), partner_access=True, recovery=True,
    ))


@router.get("/account/inbox", response_class=HTMLResponse)
def account_inbox(
    request: Request, account: Account = Depends(account_owner_account),
    session: ServerSession = Depends(account_owner_session), db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    cipher = FieldCipher(settings.field_encryption_key)
    return templates.TemplateResponse(request, "inbox.html", context(
        request, account.language_code, messages=inbox_rows(db, cipher, account.language_code, "account", account.id),
        csrf=request.cookies.get("sr_account_owner_csrf", ""), confirm_base="/account/inbox",
        partner_view=False,
    ))


@router.post("/account/inbox/{notification_id}/read")
def account_confirm_read(
    notification_id: str, request: Request, csrf: str = Form(...),
    account: Account = Depends(account_owner_account), session: ServerSession = Depends(account_owner_session),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    if not InboxService(FieldCipher(settings.field_encryption_key)).confirm_read(db, notification_id, "account", account.id):
        raise HTTPException(404)
    return RedirectResponse("/account/inbox", 303)


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


@router.post("/account/partners/{partner_id}/state/{action}")
def set_partner_state(
    partner_id: str, action: str, request: Request, csrf: str = Form(...),
    account: Account = Depends(account_owner_account), session: ServerSession = Depends(account_owner_session),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    if action not in {"disable", "enable"}:
        raise HTTPException(404)
    csrf_guard(request, session, settings, csrf)
    ManagementService(settings, FieldCipher(settings.field_encryption_key)).set_partner_active(
        db, account.id, partner_id, action == "enable"
    )
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
    db.flush()
    AccountService(
        settings, FieldCipher(settings.field_encryption_key)
    ).finish_current_review_if_complete(db, account)
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
    return templates.TemplateResponse(request, "token.html", context(
        request, account.language_code, title=translate(account.language_code, "token.trusted_title"),
        url=url, qr=qr_data(url), trusted_access=True
    ))


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
        request, account.language_code,
        title=translate(account.language_code, "token.owner_trusted_title"), url=url, qr=qr_data(url), trusted_access=True
    ))


@router.post("/account/trusted-persons/{person_id}/rotate-token", response_class=HTMLResponse)
def rotate_person(
    person_id: str, request: Request, csrf: str = Form(...), account: Account = Depends(account_owner_account),
    session: ServerSession = Depends(account_owner_session), db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    token = ManagementService(settings, FieldCipher(settings.field_encryption_key)).rotate_trusted_token(db, account.id, person_id)
    url = f"{settings.app_base_url}/notify/{token}"
    return templates.TemplateResponse(request, "token.html", context(
        request, account.language_code, title=translate(account.language_code, "token.new_title"),
        url=url, qr=qr_data(url), trusted_access=True, recovery=True
    ))


def owned_person(db: Session, account_id: str, person_id: str) -> TrustedPerson | None:
    return db.scalar(select(TrustedPerson).where(
        TrustedPerson.id == person_id, TrustedPerson.account_id == account_id
    ))


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
    completed = AccountService(
        settings, FieldCipher(settings.field_encryption_key)
    ).confirm_review(db, account)
    suffix = "complete" if completed else "waiting"
    return RedirectResponse(f"/account/dashboard?review={suffix}", 303)


@router.post("/account/token/rotate", response_class=HTMLResponse)
def rotate_account_owner(
    request: Request, csrf: str = Form(...), account: Account = Depends(account_owner_account), session: ServerSession = Depends(account_owner_session),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    token = AuthenticationService(settings).rotate_account_owner_token(db, account.id)
    url = f"{settings.app_base_url}/account/{token}"
    response = templates.TemplateResponse(request, "token.html", context(
        request, account.language_code, title=translate(account.language_code, "token.owner_title"),
        url=url, qr=qr_data(url), recovery=True, owner_access=True
    ))
    response.delete_cookie("sr_account_owner")
    response.delete_cookie("sr_account_owner_csrf")
    return response


@router.post("/account/password/change")
def change_password(
    request: Request, current_password: str = Form(...), new_password: str = Form(...),
    new_password_confirm: str = Form(...), csrf: str = Form(...),
    account: Account = Depends(account_owner_account), session: ServerSession = Depends(account_owner_session),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    if new_password != new_password_confirm:
        raise HTTPException(400, translate(account.language_code, "error.password_mismatch"))
    try:
        changed = AuthenticationService(settings).change_password(
            db, account.id, current_password, new_password
        )
    except ValueError:
        raise HTTPException(400, translate(account.language_code, "error.password_invalid"))
    if not changed:
        raise HTTPException(401, translate(account.language_code, "error.login"))
    response = RedirectResponse("/", 303)
    response.delete_cookie("sr_account_owner")
    response.delete_cookie("sr_account_owner_csrf")
    return response


@router.post("/account/delete")
def delete_account(
    request: Request, password: str = Form(...), csrf: str = Form(...), account: Account = Depends(account_owner_account),
    session: ServerSession = Depends(account_owner_session), db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    csrf_guard(request, session, settings, csrf)
    if not verify_password(account.credential.password_hash, password):
        raise HTTPException(401, translate(account.language_code, "error.login"))
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
