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

@router.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    language = browser_language(request, settings.default_language, allow_query=True)
    return templates.TemplateResponse(
        request, "index.html",
        context(request, language, creation_enabled=system_configuration(db).account_creation_enabled),
    )


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request, settings: Settings = Depends(get_settings)):
    language = browser_language(request, settings.default_language, allow_query=True)
    return templates.TemplateResponse(
        request, "help.html", context(request, language)
    )


@router.get("/imprint", response_class=HTMLResponse)
def imprint(
    request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    language = browser_language(request, settings.default_language, allow_query=True)
    content, fallback = load_public_site_content_with_fallback(
        db, language, settings.default_language
    )
    return templates.TemplateResponse(request, "publiccontent.html", context(
        request, language,
        title=translate(language, "site.imprint"),
        body=render_public_markdown(content.imprint_text) if content else "",
        fallback=fallback,
        missing_message=translate(language, "public.imprint_missing"),
    ))


@router.get("/privacy", response_class=HTMLResponse)
def privacy(
    request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    language = browser_language(request, settings.default_language, allow_query=True)
    content, fallback = load_public_site_content_with_fallback(
        db, language, settings.default_language
    )
    return templates.TemplateResponse(request, "publiccontent.html", context(
        request, language,
        title=translate(language, "site.privacy"),
        body=render_public_markdown(content.privacy_text) if content else "",
        fallback=fallback,
        missing_message=translate(language, "public.privacy_missing"),
    ))


@router.get("/contact", response_class=HTMLResponse)
def contact(
    request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    language = browser_language(request, settings.default_language, allow_query=True)
    content, fallback = load_public_site_content_with_fallback(
        db, language, settings.default_language
    )
    return templates.TemplateResponse(request, "publiccontent.html", context(
        request, language,
        title=translate(language, "site.contact"),
        body=render_public_markdown(content.contact_text) if content else "",
        contact_email=content.contact_email if content else "",
        fallback=fallback,
        missing_message=translate(language, "public.contact_missing"),
    ))


@router.get("/account/create", response_class=HTMLResponse)
def create_form(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    if not system_configuration(db).account_creation_enabled:
        raise HTTPException(404)
    language = browser_language(request, settings.default_language, allow_query=True)
    return public_form_response(request, db, settings, "create.html", language=language)


@router.post("/account/create", response_class=HTMLResponse)
async def create_account(
    request: Request,
    csrf: str = Form(...),
    language_code: str = Form("de"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not system_configuration(db).account_creation_enabled:
        raise HTTPException(404)
    public_csrf_guard(request, db, settings, csrf)
    language = normalize_language(language_code, settings.default_language)
    try:
        decision = await registration_policy(
            request.app.state.entitlement_provider,
            RegistrationContext(),
            settings.entitlement_provider_timeout_seconds,
        )
    except EntitlementProviderUnavailableError:
        return templates.TemplateResponse(
            request,
            "error.html",
            context(
                request,
                language,
                title=translate(language, "registration.unavailable_title"),
                message=translate(language, "registration.unavailable_message"),
            ),
            status_code=503,
        )
    if decision == RegistrationDecision.deny:
        return templates.TemplateResponse(
            request,
            "error.html",
            context(
                request,
                language,
                title=translate(language, "registration.denied_title"),
                message=translate(language, "registration.denied_message"),
            ),
            status_code=403,
        )
    account, account_owner_token, setup_token = AccountService(
        settings, FieldCipher(settings.field_encryption_key)
    ).create(db, language)
    await notify_account_created(
        request.app.state.entitlement_provider,
        AccountCreatedContext(account_id=account.id),
        settings.entitlement_provider_timeout_seconds,
    )
    account_owner_url = f"{settings.app_base_url}/account/{account_owner_token}"
    setup_url = f"{settings.app_base_url}/account/setup/{setup_token}"
    return templates.TemplateResponse(request, "created.html", context(
        request, account.language_code, account_owner_url=account_owner_url,
        setup_url=setup_url, qr=qr_data(account_owner_url)
    ))


@router.get("/account/setup/{token}", response_class=HTMLResponse)
def setup_form(token: str, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    credential = db.scalar(select(AccountOwnerCredential).where(
        AccountOwnerCredential.setup_token_hash == keyed_hash(token, settings.token_hmac_key),
        AccountOwnerCredential.setup_expires_at > utc_now(),
    ))
    if not credential:
        raise HTTPException(404)
    return public_form_response(
        request,
        db,
        settings,
        "setup.html",
        language=credential.account.language_code,
        account_id=credential.account_id,
        token=token,
    )


@router.post("/account/setup/{token}", response_class=HTMLResponse)
def setup_account(
    token: str, request: Request, owner_name: str = Form(...), password: str = Form(...),
    password_confirm: str = Form(...), email: str = Form(...), csrf: str = Form(...),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    public_csrf_guard(request, db, settings, csrf)
    credential = db.scalar(select(AccountOwnerCredential).where(
        AccountOwnerCredential.setup_token_hash == keyed_hash(token, settings.token_hmac_key)
    ))
    language = credential.account.language_code if credential else settings.default_language
    if password != password_confirm:
        return templates.TemplateResponse(
            request, "setup.html",
            context(request, language, token=token, csrf=csrf, error=translate(language, "error.password_mismatch")),
            status_code=400,
        )
    try:
        account, verification = AccountService(
            settings, FieldCipher(settings.field_encryption_key)
        ).setup(db, token, password, email, owner_name=owner_name)
        language = account.language_code
    except (LookupError, ValueError) as exc:
        return templates.TemplateResponse(
            request,
            "setup.html",
            context(request, language, token=token, csrf=csrf, error=str(exc)),
            status_code=400,
        )
    verify_url = f"{settings.app_base_url}/verify-contact/{verification}"
    provider = load_email_provider(db, settings, FieldCipher(settings.field_encryption_key))
    contact = db.scalar(select(ContactMethod).where(
        ContactMethod.account_id == account.id,
        ContactMethod.verification_token_hash
        == keyed_hash(verification, settings.token_hmac_key),
    ))
    result = send_tracked_email(
        db,
        settings,
        FieldCipher(settings.field_encryption_key),
        provider,
        email.strip(),
        translate(language, "email.verify_subject"),
        email_body(
            language,
            "email.verify_body",
            url=verify_url,
            privacy_url=f"{settings.app_base_url}/privacy",
        ),
        contact_method_id=contact.id if contact else None,
    )
    db.commit()
    return templates.TemplateResponse(
        request,
        "setup_done.html",
        context(
            request,
            language,
            mail_sent=result.successful,
            permanent_failure=result.permanent_failure,
            verify_url=verify_url if settings.app_env != "production" else None,
        ),
    )


@router.get("/verify-contact/{token}", response_class=HTMLResponse)
def verify_contact(token: str, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    account = AccountService(
        settings, FieldCipher(settings.field_encryption_key)
    ).contact_confirmation_account(db, token)
    if not account:
        raise HTTPException(404)
    return public_form_response(
        request,
        db,
        settings,
        "verify_contact.html",
        language=account.language_code,
        account_id=account.id,
        token=token,
    )


@router.post("/verify-contact/{token}", response_class=HTMLResponse)
def confirm_contact(
    token: str,
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    public_csrf_guard(request, db, settings, csrf)
    session = SessionManager(settings).resolve(
        db, request.cookies.get("sr_public"), "public"
    )
    service = AccountService(settings, FieldCipher(settings.field_encryption_key))
    candidate = service.contact_confirmation_account(db, token)
    if not session or not candidate or session.account_id != candidate.id:
        raise HTTPException(404)
    account = service.verify_contact(db, token)
    if not account:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "message.html", context(
        request, account.language_code,
        title=translate(account.language_code, "verify.title"),
        message=translate(account.language_code, "verify.message"),
        next_step=translate(account.language_code, "verify.next"),
    ))

@router.get("/notification/success", response_class=HTMLResponse)
def success(
    request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    session = SessionManager(settings).resolve(db, request.cookies.get("sr_public"), "public")
    account = db.get(Account, session.account_id) if session and session.account_id else None
    language = account.language_code if account else browser_language(request, settings.default_language)
    return templates.TemplateResponse(request, "message.html", context(
        request, language,
        title=translate(language, "success.title"),
        message=translate(language, "success.message"),
        next_step=translate(language, "success.next"),
    ))


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nDisallow: /\n"
