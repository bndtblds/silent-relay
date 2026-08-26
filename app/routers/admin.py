from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.email_tracking import send_tracked_email
from app.i18n import (
    LANGUAGE_LABELS, SUPPORTED_LANGUAGES, browser_language, email_body,
    format_datetime, normalize_language, translate,
)
from app.models import (
    Account, AccountStatus, Delivery, DeliveryStatus, Notification, SmtpConfiguration,
)
from app.public_site import (
    load_public_site_content, public_site_content_is_complete,
    save_public_site_content,
)
from app.providers.email import EmailProviderConfig
from app.security.core import FieldCipher, SessionManager, verify_password
from app.services import audit
from app.smtp_config import (
    disable_ndr_config, load_email_config, load_email_provider, load_ndr_config,
    save_email_config, save_ndr_config, test_imap_connection, test_smtp_connection,
)
from app.system_config import (
    MAX_NOTIFICATION_DELAY_MINUTES, notification_delay_minutes,
    review_reminder_days, save_account_creation, save_notification_delay,
    save_retention_settings,
    system_configuration as load_system_configuration,
)

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")


def admin_context(request: Request, settings: Settings, **values: object) -> dict[str, object]:
    language = browser_language(request, settings.default_language)
    return {
        "request": request,
        "language": language,
        "supported_languages": SUPPORTED_LANGUAGES,
        "language_labels": LANGUAGE_LABELS,
        "t": lambda key, **arguments: translate(language, key, **arguments),
        **values,
    }


def admin_session(request: Request, db: Session, settings: Settings):
    session = SessionManager(settings).resolve(db, request.cookies.get("sr_admin"), "admin")
    if not session:
        raise HTTPException(303, headers={"Location": "/admin/login"})
    return session


def verify_admin_csrf(request: Request, csrf: str, db: Session, settings: Settings):
    session = admin_session(request, db, settings)
    if not SessionManager(settings).verify_csrf(session, csrf):
        language = browser_language(request, settings.default_language)
        raise HTTPException(403, translate(language, "error.csrf"))
    return session


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, settings: Settings = Depends(get_settings)):
    language = browser_language(request, settings.default_language)
    return templates.TemplateResponse(
        request, "adminlogin_en.html" if language == "en" else "adminlogin.html",
        admin_context(request, settings)
    )


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    if username != settings.admin_username or not verify_password(settings.admin_password_hash, password):
        raise HTTPException(401, translate(
            browser_language(request, settings.default_language), "error.login"
        ))
    raw, csrf = SessionManager(settings).create(db, "admin")
    db.commit()
    response = RedirectResponse("/admin/accounts", 303)
    response.set_cookie("sr_admin", raw, secure=settings.secure_cookies, httponly=True, samesite="strict")
    response.set_cookie("sr_admin_csrf", csrf, secure=settings.secure_cookies, httponly=True, samesite="strict")
    return response


@router.post("/logout")
def logout(
    request: Request, csrf: str = Form(...), db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    verify_admin_csrf(request, csrf, db, settings)
    SessionManager(settings).revoke(db, request.cookies.get("sr_admin"))
    db.commit()
    response = RedirectResponse("/admin/login", 303)
    response.delete_cookie("sr_admin")
    response.delete_cookie("sr_admin_csrf")
    return response


@router.get("/accounts", response_class=HTMLResponse)
def accounts(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    admin_session(request, db, settings)
    language = browser_language(request, settings.default_language)
    rows = []
    for account in db.scalars(select(Account)):
        failures = db.scalar(select(func.count()).select_from(Delivery).where(
            Delivery.status == DeliveryStatus.permanent_failure,
            Delivery.notification_id.in_(select(Notification.id).where(Notification.account_id == account.id))
        ))
        rows.append({
            "id": account.id,
            "status": translate(language, f"status.{account.status.value}"),
            "status_style": "success" if account.status == AccountStatus.active else "pending",
            "created": format_datetime(account.created_at, language),
            "reviewed": format_datetime(account.last_reviewed_at, language) if account.last_reviewed_at else None,
            "failures": failures,
        })
    public_content = load_public_site_content(db, language)
    return templates.TemplateResponse(
        request, "adminaccounts_en.html" if language == "en" else "adminaccounts.html",
        admin_context(
            request, settings,
            accounts=rows,
            admin_section="accounts",
            public_content_complete=public_site_content_is_complete(public_content),
            csrf=request.cookies.get("sr_admin_csrf", ""),
        )
    )


@router.get("/health")
def system_status(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    admin_session(request, db, settings)
    configured = db.get(SmtpConfiguration, "default")
    return {
        "database": "ready",
        "smtp_configured": bool(configured),
        "scheduler_interval_seconds": settings.scheduler_interval_seconds,
    }


@router.get("/system", response_class=HTMLResponse)
def system_configuration(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    admin_session(request, db, settings)
    language = browser_language(request, settings.default_language)
    return templates.TemplateResponse(
        request, "adminsystem_en.html" if language == "en" else "adminsystem.html",
        admin_context(
            request, settings,
            notification_delay_minutes=notification_delay_minutes(db),
            admin_section="system",
            max_notification_delay_minutes=MAX_NOTIFICATION_DELAY_MINUTES,
            system_config=load_system_configuration(db),
            csrf=request.cookies.get("sr_admin_csrf", ""),
            result=request.query_params.get("result"),
        ),
    )


@router.get("/system/retention", response_class=HTMLResponse)
def retention_configuration(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    admin_session(request, db, settings)
    config = load_system_configuration(db)
    language = browser_language(request, settings.default_language)
    return templates.TemplateResponse(
        request,
        "adminretention_en.html" if language == "en" else "adminretention.html",
        admin_context(
            request, settings,
            system_config=config,
            admin_section="system",
            reminder_days=review_reminder_days(config),
            csrf=request.cookies.get("sr_admin_csrf", ""),
            result=request.query_params.get("result"),
        ),
    )


@router.get("/system/email", response_class=HTMLResponse)
def email_configuration(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    admin_session(request, db, settings)
    config = load_email_config(db, FieldCipher(settings.field_encryption_key))
    stored = db.get(SmtpConfiguration, "default")
    if config is None:
        config = EmailProviderConfig("", 587, "", "", "", "SilentRelay")
    cipher = FieldCipher(settings.field_encryption_key)
    ndr_config = load_ndr_config(db, settings, cipher)
    language = browser_language(request, settings.default_language)
    return templates.TemplateResponse(
        request, "adminemail_en.html" if language == "en" else "adminemail.html",
        admin_context(
            request, settings,
            config=config,
            admin_section="system",
            password_configured=bool(stored and stored.encrypted_password),
            stored_in_database=bool(stored),
            ndr_enabled=bool(ndr_config),
            imap_host=(
                cipher.decrypt(stored.encrypted_imap_host)
                if stored and stored.encrypted_imap_host else ""
            ),
            imap_port=(stored.imap_port if stored and stored.imap_port else 993),
            imap_username=(
                cipher.decrypt(stored.encrypted_imap_username)
                if stored and stored.encrypted_imap_username else ""
            ),
            imap_password_configured=bool(stored and stored.encrypted_imap_password),
            csrf=request.cookies.get("sr_admin_csrf", ""),
            result=request.query_params.get("result"),
        )
    )


@router.post("/system/notification-delay")
def update_notification_delay(
    request: Request,
    minutes: int = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    verify_admin_csrf(request, csrf, db, settings)
    try:
        save_notification_delay(db, minutes)
    except ValueError:
        return RedirectResponse("/admin/system?result=delay_invalid", 303)
    audit(db, "notification_delay_updated")
    db.commit()
    return RedirectResponse("/admin/system?result=delay_saved", 303)


@router.post("/system/account-creation")
def update_account_creation(
    request: Request,
    account_creation_enabled: str | None = Form(None),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    verify_admin_csrf(request, csrf, db, settings)
    save_account_creation(db, account_creation_enabled == "yes")
    audit(db, "account_creation_configuration_updated")
    db.commit()
    return RedirectResponse("/admin/system?result=account_creation_saved", 303)


@router.post("/system/retention")
def update_retention_settings(
    request: Request,
    account_pending_retention_days: int = Form(...),
    account_review_interval_days: int = Form(...),
    reminder_day: list[str] = Form(...),
    account_review_grace_days: int = Form(...),
    contact_problem_reminder_days: int = Form(...),
    account_retention_after_disable_days: int = Form(...),
    message_retention_days: int = Form(...),
    audit_retention_days: int = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    verify_admin_csrf(request, csrf, db, settings)
    try:
        save_retention_settings(
            db,
            account_pending_retention_days=account_pending_retention_days,
            account_review_interval_days=account_review_interval_days,
            account_review_reminder_days=",".join(reminder_day),
            account_review_grace_days=account_review_grace_days,
            contact_problem_reminder_days=contact_problem_reminder_days,
            account_retention_after_disable_days=account_retention_after_disable_days,
            message_retention_days=message_retention_days,
            audit_retention_days=audit_retention_days,
        )
    except ValueError:
        return RedirectResponse("/admin/system/retention?result=retention_invalid", 303)
    audit(db, "retention_configuration_updated")
    db.commit()
    return RedirectResponse("/admin/system/retention?result=retention_saved", 303)


@router.get("/public-content", response_class=HTMLResponse)
def public_content_configuration(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    admin_session(request, db, settings)
    content_language = normalize_language(
        request.query_params.get("content_language"), settings.default_language
    )
    stored = load_public_site_content(db, content_language)
    language = browser_language(request, settings.default_language)
    return templates.TemplateResponse(
        request, "adminpubliccontent_en.html" if language == "en" else "adminpubliccontent.html",
        admin_context(
            request, settings,
            content=stored,
            admin_section="public",
            content_language=content_language,
            complete=public_site_content_is_complete(stored),
            csrf=request.cookies.get("sr_admin_csrf", ""),
            result=request.query_params.get("result"),
        )
    )


@router.post("/public-content", response_class=HTMLResponse)
def update_public_content(
    request: Request,
    imprint_text: str = Form(""),
    privacy_text: str = Form(""),
    contact_email: str = Form(""),
    contact_text: str = Form(""),
    language_code: str = Form("de"),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    verify_admin_csrf(request, csrf, db, settings)
    language_code = normalize_language(language_code, settings.default_language)
    language = browser_language(request, settings.default_language)
    try:
        content = save_public_site_content(
            db,
            language_code=language_code,
            imprint_text=imprint_text,
            privacy_text=privacy_text,
            contact_email=contact_email,
            contact_text=contact_text,
            validation_language=language,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "adminpubliccontent_en.html" if language == "en" else "adminpubliccontent.html",
            admin_context(
                request, settings,
                content={
                "imprint_text": imprint_text,
                "privacy_text": privacy_text,
                "contact_email": contact_email,
                "contact_text": contact_text,
                },
                admin_section="public",
                complete=False,
                content_language=language_code,
                csrf=csrf,
                error=str(exc),
            ), status_code=400)
    audit(db, "public_site_content_updated", language=language_code)
    db.commit()
    return RedirectResponse(
        f"/admin/public-content?content_language={language_code}&result=saved", 303
    )


@router.post("/system/smtp")
def update_smtp(
    request: Request,
    host: str = Form(...),
    port: int = Form(...),
    username: str = Form(""),
    password: str = Form(""),
    from_address: str = Form(...),
    from_name: str = Form("SilentRelay"),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    verify_admin_csrf(request, csrf, db, settings)
    try:
        save_email_config(
            db,
            FieldCipher(settings.field_encryption_key),
            host=host,
            port=port,
            username=username,
            password=password or None,
            from_address=from_address,
            from_name=from_name,
        )
    except ValueError:
        return RedirectResponse("/admin/system/email?result=invalid", 303)
    audit(db, "smtp_configuration_updated")
    db.commit()
    return RedirectResponse("/admin/system/email?result=saved", 303)


@router.post("/system/smtp/test-connection")
def test_connection(
    request: Request, csrf: str = Form(...), db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    verify_admin_csrf(request, csrf, db, settings)
    try:
        config = load_email_config(db, FieldCipher(settings.field_encryption_key))
        if config is None:
            raise ValueError
        test_smtp_connection(config)
    except Exception:
        return RedirectResponse("/admin/system/email?result=connection_failed", 303)
    return RedirectResponse("/admin/system/email?result=connection_ok", 303)


@router.post("/system/ndr")
def update_ndr(
    request: Request,
    host: str = Form(...),
    port: int = Form(...),
    username: str = Form(...),
    password: str = Form(""),
    acknowledged_address: str = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    verify_admin_csrf(request, csrf, db, settings)
    try:
        save_ndr_config(
            db,
            settings,
            FieldCipher(settings.field_encryption_key),
            host=host,
            port=port,
            username=username,
            password=password or None,
            acknowledged_address=acknowledged_address,
        )
    except ValueError:
        return RedirectResponse("/admin/system/email?result=ndr_invalid", 303)
    audit(db, "ndr_configuration_updated")
    db.commit()
    return RedirectResponse("/admin/system/email?result=ndr_saved", 303)


@router.post("/system/ndr/disable")
def disable_ndr(
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    verify_admin_csrf(request, csrf, db, settings)
    disable_ndr_config(db)
    audit(db, "ndr_configuration_disabled")
    db.commit()
    return RedirectResponse("/admin/system/email?result=ndr_disabled", 303)


@router.post("/system/ndr/test-connection")
def test_ndr_connection(
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    verify_admin_csrf(request, csrf, db, settings)
    config = load_ndr_config(
        db, settings, FieldCipher(settings.field_encryption_key)
    )
    if not config:
        return RedirectResponse("/admin/system/email?result=ndr_connection_failed", 303)
    try:
        test_imap_connection(config)
    except Exception:
        return RedirectResponse("/admin/system/email?result=ndr_connection_failed", 303)
    return RedirectResponse("/admin/system/email?result=ndr_connection_ok", 303)


@router.post("/system/smtp/test-email")
def test_email(
    request: Request, recipient: str = Form(...), csrf: str = Form(...),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    verify_admin_csrf(request, csrf, db, settings)
    if "@" not in recipient or len(recipient) > 320:
        return RedirectResponse("/admin/system/email?result=invalid_recipient", 303)
    language = browser_language(request, settings.default_language)
    cipher = FieldCipher(settings.field_encryption_key)
    result = send_tracked_email(
        db, settings, cipher, load_email_provider(db, settings, cipher),
        recipient.strip(),
        translate(language, "email.test_subject"),
        email_body(language, "email.test_body"),
    )
    db.commit()
    return RedirectResponse(
        f"/admin/system/email?result={'email_ok' if result.successful else 'email_failed'}", 303
    )


@router.post("/accounts/{account_id}/disable")
def disable(account_id: str, request: Request, csrf: str = Form(...), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    verify_admin_csrf(request, csrf, db, settings)
    account = db.get(Account, account_id)
    if account:
        account.is_admin_locked = True
        account.status = AccountStatus.disabled
        db.commit()
    return RedirectResponse("/admin/accounts", 303)


@router.post("/accounts/{account_id}/delete")
def remove(account_id: str, request: Request, csrf: str = Form(...), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    verify_admin_csrf(request, csrf, db, settings)
    account = db.get(Account, account_id)
    if account:
        db.delete(account)
        db.commit()
    return RedirectResponse("/admin/accounts", 303)
