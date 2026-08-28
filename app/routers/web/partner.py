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

@router.get("/partner/access/{token}", response_class=HTMLResponse)
def partner_access_form(
    token: str, request: Request, db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    access = PartnerAuthenticationService(settings).resolve_access(db, token)
    if not access:
        raise HTTPException(404)
    partner, credential = access
    account = db.get(Account, partner.account_id)
    if not credential.password_hash:
        if credential.enrollment_expires_at <= utc_now():
            return templates.TemplateResponse(request, "partner_access_expired.html", context(request, account.language_code), status_code=410)
        return public_form_response(
            request, db, settings, "partner_setup.html", language=account.language_code,
            token=token, expires=format_date(credential.enrollment_expires_at, account.language_code),
        )
    return public_form_response(request, db, settings, "partner_login.html", language=account.language_code, token=token)


@router.post("/partner/access/{token}/setup")
def partner_setup(
    token: str, request: Request, password: str = Form(...), password_confirm: str = Form(...),
    csrf: str = Form(...), db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    public_csrf_guard(request, db, settings, csrf)
    service = PartnerAuthenticationService(settings)
    access = service.resolve_access(db, token)
    if not access:
        raise HTTPException(404)
    partner, credential = access
    account = db.get(Account, partner.account_id)
    if password != password_confirm:
        error = translate(account.language_code, "partner.password_mismatch")
    else:
        try:
            service.enroll(db, credential, password)
            error = None
        except ValueError as exc:
            error = str(exc) if account.language_code == "de" else translate("en", "partner.password_invalid")
        except LookupError:
            return templates.TemplateResponse(request, "partner_access_expired.html", context(request, account.language_code), status_code=410)
    if error:
        return templates.TemplateResponse(request, "partner_setup.html", context(
            request, account.language_code, token=token, csrf=csrf, expires=format_date(credential.enrollment_expires_at, account.language_code), error=error,
        ), status_code=400)
    raw_session, raw_csrf = SessionManager(settings).create(db, "partner", account.id, partner_id=partner.id)
    db.commit()
    response = RedirectResponse("/partner/inbox", 303)
    set_partner_session_cookies(response, settings, raw_session, raw_csrf)
    return response


@router.post("/partner/access/{token}/login")
def partner_login(
    token: str, request: Request, password: str = Form(...), csrf: str = Form(...),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    public_csrf_guard(request, db, settings, csrf)
    service = PartnerAuthenticationService(settings)
    access = service.resolve_access(db, token)
    if not access:
        raise HTTPException(404)
    partner, credential = access
    account = db.get(Account, partner.account_id)
    if not service.login(db, credential, password):
        return templates.TemplateResponse(request, "partner_login.html", context(
            request, account.language_code, token=token, csrf=csrf, error=translate(account.language_code, "error.login"),
        ), status_code=401)
    raw_session, raw_csrf = SessionManager(settings).create(db, "partner", account.id, partner_id=partner.id)
    db.commit()
    response = RedirectResponse("/partner/inbox", 303)
    set_partner_session_cookies(response, settings, raw_session, raw_csrf)
    return response


@router.get("/partner/inbox", response_class=HTMLResponse)
def partner_inbox(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    resolved = partner_session(request, db, settings)
    if not resolved:
        return RedirectResponse("/", 303)
    _, partner = resolved
    account = db.get(Account, partner.account_id)
    cipher = FieldCipher(settings.field_encryption_key)
    return templates.TemplateResponse(request, "inbox.html", context(
        request, account.language_code, messages=inbox_rows(db, cipher, account.language_code, "partner", partner.id),
        csrf=request.cookies.get("sr_partner_csrf", ""), confirm_base="/partner/inbox",
        partner_view=True,
    ))


@router.post("/partner/inbox/{notification_id}/read")
def partner_confirm_read(
    notification_id: str, request: Request, csrf: str = Form(...),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    resolved = partner_session(request, db, settings)
    if not resolved:
        raise HTTPException(404)
    session, partner = resolved
    if not SessionManager(settings).verify_csrf(session, csrf):
        raise HTTPException(403, translate("de", "error.csrf"))
    if not InboxService(FieldCipher(settings.field_encryption_key)).confirm_read(db, notification_id, "partner", partner.id):
        raise HTTPException(404)
    return RedirectResponse("/partner/inbox", 303)


@router.post("/partner/password/change")
def partner_change_password(
    request: Request, current_password: str = Form(...), new_password: str = Form(...),
    new_password_confirm: str = Form(...), csrf: str = Form(...),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    resolved = partner_session(request, db, settings)
    if not resolved:
        raise HTTPException(404)
    session, partner = resolved
    if not SessionManager(settings).verify_csrf(session, csrf):
        raise HTTPException(403, translate("de", "error.csrf"))
    account = db.get(Account, partner.account_id)
    if new_password != new_password_confirm:
        raise HTTPException(400, translate(account.language_code, "error.password_mismatch"))
    try:
        changed = PartnerAuthenticationService(settings).change_password(db, partner.id, current_password, new_password)
    except ValueError:
        raise HTTPException(400, translate(db.get(Account, partner.account_id).language_code, "partner.password_invalid"))
    if not changed:
        raise HTTPException(401, translate(db.get(Account, partner.account_id).language_code, "error.login"))
    response = RedirectResponse("/", 303)
    response.delete_cookie("sr_partner")
    response.delete_cookie("sr_partner_csrf")
    return response

@router.post("/partner/logout")
def partner_logout(
    request: Request, csrf: str = Form(...), db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    resolved = partner_session(request, db, settings)
    if resolved and SessionManager(settings).verify_csrf(resolved[0], csrf):
        SessionManager(settings).revoke(db, request.cookies.get("sr_partner"))
        db.commit()
    response = RedirectResponse("/", 303)
    response.delete_cookie("sr_partner")
    response.delete_cookie("sr_partner_csrf")
    return response
