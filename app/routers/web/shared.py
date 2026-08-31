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

templates = Jinja2Templates(directory="app/templates")

def context(request: Request, language: str = "de", **values: object) -> dict[str, object]:
    language = normalize_language(language)
    return {
        "request": request,
        "language": language,
        "supported_languages": SUPPORTED_LANGUAGES,
        "language_labels": LANGUAGE_LABELS,
        "t": lambda key, **arguments: translate(language, key, **arguments),
        **values,
    }


def csrf_guard(request: Request, session: ServerSession, settings: Settings, csrf: str) -> None:
    if not SessionManager(settings).verify_csrf(session, csrf):
        raise HTTPException(403, "Ungültiger CSRF-Token.")


def qr_data(url: str) -> str:
    image = qrcode.make(url)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return base64.b64encode(stream.getvalue()).decode()


def public_form_response(
    request: Request,
    db: Session,
    settings: Settings,
    template: str,
    *,
    language: str,
    account_id: str | None = None,
    **values: object,
):
    raw_session, raw_csrf = SessionManager(settings).create(db, "public", account_id)
    db.commit()
    response = templates.TemplateResponse(
        request, template, context(request, language, csrf=raw_csrf, **values)
    )
    response.set_cookie("sr_public", raw_session, secure=settings.secure_cookies, httponly=True, samesite="strict", max_age=900)
    return response


def public_csrf_guard(request: Request, db: Session, settings: Settings, csrf: str) -> None:
    session = SessionManager(settings).resolve(db, request.cookies.get("sr_public"), "public")
    if not session or not SessionManager(settings).verify_csrf(session, csrf):
        raise HTTPException(403, "Ungültiger CSRF-Token.")


def trusted_session(
    request: Request, db: Session, settings: Settings, person_id: str
) -> ServerSession | None:
    session = SessionManager(settings).resolve(
        db, request.cookies.get("sr_trusted_person"), "trusted_person"
    )
    if not session or session.trusted_person_id != person_id:
        return None
    return session


def set_trusted_session_cookies(
    response, settings: Settings, raw_session: str, raw_csrf: str
) -> None:
    max_age = settings.session_ttl_minutes * 60
    response.set_cookie(
        "sr_trusted_person", raw_session, secure=settings.secure_cookies,
        httponly=True, samesite="strict", max_age=max_age,
    )
    response.set_cookie(
        "sr_trusted_person_csrf", raw_csrf, secure=settings.secure_cookies,
        httponly=False, samesite="strict", max_age=max_age,
    )


def trusted_csrf_guard(
    request: Request, db: Session, settings: Settings, person_id: str, csrf: str
) -> ServerSession:
    session = trusted_session(request, db, settings, person_id)
    if not session or not SessionManager(settings).verify_csrf(session, csrf):
        raise HTTPException(403, translate("de", "error.csrf"))
    return session


def partner_session(request: Request, db: Session, settings: Settings) -> tuple[ServerSession, Partner] | None:
    session = SessionManager(settings).resolve(
        db, request.cookies.get("sr_partner"), "partner"
    )
    partner = db.get(Partner, session.partner_id) if session and session.partner_id else None
    account = db.get(Account, partner.account_id) if partner else None
    if not session or not partner or not partner.is_active or not account or not account.allows_access:
        return None
    credential = db.get(PartnerCredential, partner.id)
    if not credential or not credential.enrolled_at or not credential.password_hash:
        return None
    return session, partner


def set_partner_session_cookies(response, settings: Settings, raw_session: str, raw_csrf: str) -> None:
    max_age = settings.session_ttl_minutes * 60
    response.set_cookie("sr_partner", raw_session, secure=settings.secure_cookies, httponly=True, samesite="strict", max_age=max_age)
    response.set_cookie("sr_partner_csrf", raw_csrf, secure=settings.secure_cookies, httponly=False, samesite="strict", max_age=max_age)


def inbox_rows(db: Session, cipher: FieldCipher, language: str, owner_type: str, owner_id: str):
    messages = InboxService(cipher).messages(db, owner_type, owner_id)
    trusted_person_ids = {
        notification.trusted_person_id
        for _, notification in messages
        if notification.trusted_person_id
    }
    trusted_people = {
        person.id: person
        for person in db.scalars(select(TrustedPerson).where(
            TrustedPerson.id.in_(trusted_person_ids)
        ))
    } if trusted_person_ids else {}
    affected_partner_ids = {
        person.owner_id for person in trusted_people.values()
        if person.owner_type == "partner"
    }
    affected_partners = {
        partner.id: partner
        for partner in db.scalars(select(Partner).where(
            Partner.id.in_(affected_partner_ids)
        ))
    } if affected_partner_ids else {}
    affected_account_ids = {
        notification.account_id
        for _, notification in messages
        if not (
            notification.trusted_person_id
            and notification.trusted_person_id in trusted_people
            and trusted_people[notification.trusted_person_id].owner_type == "partner"
        )
    }
    affected_accounts = {
        account.id: account
        for account in db.scalars(select(Account).where(
            Account.id.in_(affected_account_ids)
        ))
    } if affected_account_ids else {}
    rows = []
    for recipient, notification in messages:
        person = trusted_people.get(notification.trusted_person_id)
        if person and person.owner_type == "partner":
            affected = affected_partners.get(person.owner_id)
            affected_label = cipher.decrypt(affected.encrypted_name) if affected else translate(language, "inbox.partner")
        else:
            affected_account = affected_accounts.get(notification.account_id)
            affected_label = (
                cipher.decrypt(affected_account.encrypted_owner_name)
                if affected_account and affected_account.encrypted_owner_name
                else translate(language, "inbox.account_owner")
            )
        rows.append({
            "id": notification.id,
            "affected": affected_label,
            "message": cipher.decrypt(notification.encrypted_message_payload),
            "released_iso": notification.release_at.astimezone(
                UTC
            ).isoformat().replace("+00:00", "Z"),
            "released": format_datetime(notification.release_at, language),
            "read": recipient.read_at is not None,
        })
    return rows
