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

@router.get("/notify/{token}", response_class=HTMLResponse)
def notify_form(token: str, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    cipher = FieldCipher(settings.field_encryption_key)
    service = NotificationService(settings, cipher)
    access = service.resolve_access(db, token)
    if not access:
        raise HTTPException(404)
    person, record = access
    account = db.get(Account, person.account_id)
    if not record.pin_hash:
        if record.enrollment_expires_at <= utc_now():
            return templates.TemplateResponse(
                request, "trusted_access_expired.html",
                context(request, account.language_code), status_code=410,
            )
        db.commit()
        return public_form_response(
            request, db, settings, "trusted_setup.html",
            language=account.language_code, token=token,
            expires=format_date(record.enrollment_expires_at, account.language_code),
        )
    if not trusted_session(request, db, settings, person.id):
        db.commit()
        return public_form_response(
            request, db, settings, "trusted_login.html",
            language=account.language_code, token=token,
        )
    pending = [
        {
            "id": notification.id,
            "message": (
                cipher.decrypt(notification.encrypted_message_payload)
                if notification.encrypted_message_payload else ""
            ),
            "release_at_iso": notification.release_at.astimezone(
                UTC
            ).isoformat().replace("+00:00", "Z"),
            "release_at_fallback": (
                f"{format_datetime(notification.release_at, account.language_code)} UTC"
            ),
        }
        for notification in service.pending_for_person(db, person.id)
    ]
    db.commit()
    return templates.TemplateResponse(
        request, "notify.html", context(
            request, account.language_code, token=token,
            csrf=request.cookies.get("sr_trusted_person_csrf", ""),
            pending=pending,
        )
    )


@router.post("/notify/{token}/setup", response_class=HTMLResponse)
def trusted_setup(
    token: str, request: Request, pin: str = Form(...), pin_confirm: str = Form(...),
    csrf: str = Form(...), db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    public_csrf_guard(request, db, settings, csrf)
    service = NotificationService(settings, FieldCipher(settings.field_encryption_key))
    access = service.resolve_access(db, token)
    if not access:
        raise HTTPException(404)
    person, record = access
    account = db.get(Account, person.account_id)
    if record.pin_hash:
        return RedirectResponse(f"/notify/{token}", 303)
    if record.enrollment_expires_at <= utc_now():
        return templates.TemplateResponse(
            request, "trusted_access_expired.html",
            context(request, account.language_code), status_code=410,
        )
    error = None
    if pin != pin_confirm:
        error = translate(account.language_code, "trusted.pin_mismatch")
    else:
        try:
            record.pin_hash = hash_pin(pin)
        except ValueError:
            error = translate(account.language_code, "trusted.pin_invalid")
    if error:
        return templates.TemplateResponse(
            request, "trusted_setup.html", context(
                request, account.language_code, token=token, csrf=csrf,
                expires=format_date(record.enrollment_expires_at, account.language_code),
                error=error,
            ), status_code=400,
        )
    now = utc_now()
    record.enrolled_at = now
    record.failed_pin_attempts = 0
    record.locked_until = None
    raw_session, raw_csrf = SessionManager(settings).create(
        db, "trusted_person", account.id, person.id
    )
    db.commit()
    response = RedirectResponse(f"/notify/{token}?setup=complete", 303)
    set_trusted_session_cookies(response, settings, raw_session, raw_csrf)
    return response


@router.post("/notify/{token}/login", response_class=HTMLResponse)
def trusted_login(
    token: str, request: Request, pin: str = Form(...), csrf: str = Form(...),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    public_csrf_guard(request, db, settings, csrf)
    service = NotificationService(settings, FieldCipher(settings.field_encryption_key))
    access = service.resolve_access(db, token)
    if not access:
        raise HTTPException(404)
    person, record = access
    account = db.get(Account, person.account_id)
    now = utc_now()
    if not record.pin_hash:
        return RedirectResponse(f"/notify/{token}", 303)
    if record.locked_until and record.locked_until > now:
        error = translate(account.language_code, "trusted.pin_locked")
    elif verify_pin(record.pin_hash, pin):
        record.failed_pin_attempts = 0
        record.locked_until = None
        raw_session, raw_csrf = SessionManager(settings).create(
            db, "trusted_person", account.id, person.id
        )
        db.commit()
        response = RedirectResponse(f"/notify/{token}", 303)
        set_trusted_session_cookies(response, settings, raw_session, raw_csrf)
        return response
    else:
        record.failed_pin_attempts += 1
        attempts = record.failed_pin_attempts
        lock_minutes = 30 if attempts >= 8 else 5 if attempts >= 5 else 1 if attempts >= 3 else 0
        if lock_minutes:
            record.locked_until = now + timedelta(minutes=lock_minutes)
        error = translate(account.language_code, "trusted.pin_failed")
    db.commit()
    return templates.TemplateResponse(
        request, "trusted_login.html", context(
            request, account.language_code, token=token, csrf=csrf, error=error,
        ), status_code=401,
    )


@router.post("/notify/{token}/pin/change")
def trusted_change_pin(
    token: str, request: Request, current_pin: str = Form(...),
    new_pin: str = Form(...), new_pin_confirm: str = Form(...),
    csrf: str = Form(...), db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    service = NotificationService(settings, FieldCipher(settings.field_encryption_key))
    access = service.resolve_access(db, token)
    if not access:
        raise HTTPException(404)
    person, _ = access
    account = db.get(Account, person.account_id)
    trusted_csrf_guard(request, db, settings, person.id, csrf)
    if new_pin != new_pin_confirm:
        raise HTTPException(400, translate(account.language_code, "trusted.pin_mismatch"))
    try:
        changed = service.change_pin(db, person.id, current_pin, new_pin)
    except ValueError:
        raise HTTPException(400, translate(account.language_code, "trusted.pin_change_invalid"))
    if not changed:
        raise HTTPException(401, translate(account.language_code, "trusted.pin_failed"))
    response = RedirectResponse(f"/notify/{token}", 303)
    response.delete_cookie("sr_trusted_person")
    response.delete_cookie("sr_trusted_person_csrf")
    return response


@router.post("/notify/{token}", response_class=HTMLResponse)
def notify_stage(
    token: str, request: Request, message: str = Form(...), csrf: str = Form(...),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    service = NotificationService(settings, FieldCipher(settings.field_encryption_key))
    person = service.resolve_person(db, token)
    if not person:
        raise HTTPException(404)
    trusted_csrf_guard(request, db, settings, person.id, csrf)
    account = db.get(Account, person.account_id)
    language = account.language_code
    try:
        submission = service.stage(db, person, message)
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "notify.html",
            context(request, language, token=token, csrf=csrf, error=str(exc)),
            status_code=400
        )
    return templates.TemplateResponse(
        request, "confirm.html",
        context(
            request, language, token=token, submission=submission, message=message,
            csrf=csrf, delay_minutes=notification_delay_minutes(db),
        )
    )


@router.post("/notify/{token}/confirm")
def notify_confirm(
    token: str, request: Request, submission: str = Form(...), csrf: str = Form(...),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    service = NotificationService(settings, FieldCipher(settings.field_encryption_key))
    person = service.resolve_person(db, token)
    if not person:
        raise HTTPException(404)
    trusted_csrf_guard(request, db, settings, person.id, csrf)
    try:
        notification = service.accept(db, submission, person.id)
    except LookupError:
        account = db.get(Account, person.account_id)
        raise HTTPException(409, translate(account.language_code, "error.submission"))
    cipher = FieldCipher(settings.field_encryption_key)
    DeliveryService(settings, cipher, {"email": load_email_provider(db, settings, cipher)}).process_due(db)
    if notification.release_at > utc_now():
        return RedirectResponse(f"/notify/{token}?queued={notification.id}", 303)
    return RedirectResponse("/notification/success", 303)


@router.post("/notify/{token}/notifications/{notification_id}/cancel")
def cancel_notification(
    token: str, notification_id: str, request: Request, csrf: str = Form(...),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    service = NotificationService(settings, FieldCipher(settings.field_encryption_key))
    person = service.resolve_person(db, token)
    if not person:
        raise HTTPException(404)
    trusted_csrf_guard(request, db, settings, person.id, csrf)
    result = "cancelled" if service.cancel(db, person.id, notification_id) else "cancel_unavailable"
    return RedirectResponse(f"/notify/{token}?result={result}", 303)
