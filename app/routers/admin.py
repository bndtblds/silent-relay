from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import Account, AccountStatus, Delivery, DeliveryStatus, Notification, SmtpConfiguration
from app.security.core import FieldCipher, SessionManager, verify_password
from app.services import audit
from app.smtp_config import load_email_config, load_email_provider, save_email_config, test_smtp_connection

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")
ACCOUNT_STATUS_LABELS = {
    AccountStatus.pending_verification: "Bestätigung ausstehend",
    AccountStatus.active: "Aktiv",
    AccountStatus.overdue: "Prüfung überfällig",
    AccountStatus.disabled: "Gesperrt",
}


def admin_session(request: Request, db: Session, settings: Settings):
    session = SessionManager(settings).resolve(db, request.cookies.get("sr_admin"), "admin")
    if not session:
        raise HTTPException(303, headers={"Location": "/admin/login"})
    return session


def verify_admin_csrf(request: Request, csrf: str, db: Session, settings: Settings):
    session = admin_session(request, db, settings)
    if not SessionManager(settings).verify_csrf(session, csrf):
        raise HTTPException(403, "Ungültiger CSRF-Token.")
    return session


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "adminlogin.html", {"request": request})


@router.post("/login")
def login(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    if username != settings.admin_username or not verify_password(settings.admin_password_hash, password):
        raise HTTPException(401, "Anmeldung fehlgeschlagen.")
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
    rows = []
    for account in db.scalars(select(Account)):
        failures = db.scalar(select(func.count()).select_from(Delivery).where(
            Delivery.status == DeliveryStatus.permanent_failure,
            Delivery.notification_id.in_(select(Notification.id).where(Notification.account_id == account.id))
        ))
        rows.append({
            "id": account.id,
            "status": ACCOUNT_STATUS_LABELS[account.status],
            "status_style": "success" if account.status == AccountStatus.active else "pending",
            "created": account.created_at.strftime("%d.%m.%Y, %H:%M"),
            "reviewed": account.last_reviewed_at.strftime("%d.%m.%Y, %H:%M") if account.last_reviewed_at else None,
            "failures": failures,
        })
    return templates.TemplateResponse(request, "adminaccounts.html", {"request": request, "accounts": rows, "csrf": request.cookies.get("sr_admin_csrf", "")})


@router.get("/health")
def system_status(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    admin_session(request, db, settings)
    configured = db.get(SmtpConfiguration, "default")
    return {
        "database": "ready",
        "smtp_configured": bool(configured or (settings.smtp_host and settings.smtp_from_address)),
        "scheduler_interval_seconds": settings.scheduler_interval_seconds,
    }


@router.get("/system", response_class=HTMLResponse)
def system_configuration(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    admin_session(request, db, settings)
    config = load_email_config(db, settings, FieldCipher(settings.field_encryption_key))
    stored = db.get(SmtpConfiguration, "default")
    return templates.TemplateResponse(request, "adminsystem.html", {
        "request": request,
        "config": config,
        "password_configured": bool((stored and stored.encrypted_password) or settings.smtp_password),
        "stored_in_database": bool(stored),
        "csrf": request.cookies.get("sr_admin_csrf", ""),
        "result": request.query_params.get("result"),
    })


@router.post("/system/smtp")
def update_smtp(
    request: Request,
    host: str = Form(...),
    port: int = Form(...),
    username: str = Form(""),
    password: str = Form(""),
    starttls: str | None = Form(None),
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
            starttls=starttls == "yes",
            from_address=from_address,
            from_name=from_name,
        )
    except ValueError:
        return RedirectResponse("/admin/system?result=invalid", 303)
    audit(db, "smtp_configuration_updated")
    db.commit()
    return RedirectResponse("/admin/system?result=saved", 303)


@router.post("/system/smtp/test-connection")
def test_connection(
    request: Request, csrf: str = Form(...), db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    verify_admin_csrf(request, csrf, db, settings)
    try:
        test_smtp_connection(load_email_config(db, settings, FieldCipher(settings.field_encryption_key)))
    except Exception:
        return RedirectResponse("/admin/system?result=connection_failed", 303)
    return RedirectResponse("/admin/system?result=connection_ok", 303)


@router.post("/system/smtp/test-email")
def test_email(
    request: Request, recipient: str = Form(...), csrf: str = Form(...),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    verify_admin_csrf(request, csrf, db, settings)
    if "@" not in recipient or len(recipient) > 320:
        return RedirectResponse("/admin/system?result=invalid_recipient", 303)
    result = load_email_provider(db, settings, FieldCipher(settings.field_encryption_key)).send(
        recipient.strip(), "SilentRelay: Test-E-Mail",
        "Diese Test-E-Mail bestätigt, dass der SMTP-Versand von SilentRelay funktioniert.",
    )
    return RedirectResponse(
        f"/admin/system?result={'email_ok' if result.successful else 'email_failed'}", 303
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
