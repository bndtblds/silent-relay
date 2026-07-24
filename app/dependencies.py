from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import Account, ServerSession
from app.security.core import FieldCipher, SessionManager


def get_cipher(settings: Settings = Depends(get_settings)) -> FieldCipher:
    return FieldCipher(settings.field_encryption_key)


def account_owner_session(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> ServerSession:
    session = SessionManager(settings).resolve(db, request.cookies.get("sr_account_owner"), "account_owner")
    if not session or not session.account_id:
        raise HTTPException(303, headers={"Location": "/"})
    return session


def account_owner_account(session: ServerSession = Depends(account_owner_session), db: Session = Depends(get_db)) -> Account:
    account = db.get(Account, session.account_id)
    if not account:
        raise HTTPException(303, headers={"Location": "/"})
    return account


def require_csrf(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ServerSession:
    session = SessionManager(settings).resolve(db, request.cookies.get("sr_account_owner"), "account_owner")
    if not session:
        raise HTTPException(403, "Ungültige Sitzung.")
    token = request.headers.get("X-CSRF-Token")
    if not SessionManager(settings).verify_csrf(session, token):
        raise HTTPException(403, "Ungültiger CSRF-Token.")
    return session
