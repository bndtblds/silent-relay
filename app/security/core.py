from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ServerSession

_password_hasher = PasswordHasher()


class CryptoError(ValueError):
    pass


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def keyed_hash(value: str, key: str) -> str:
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()


def fingerprint(value: str, key: str) -> str:
    return keyed_hash(value.strip().casefold(), key)


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 256:
        raise ValueError("Das Passwort muss 12 bis 256 Zeichen lang sein.")
    return _password_hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    if not stored_hash or len(password) > 256:
        return False
    try:
        return _password_hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


class FieldCipher:
    def __init__(self, key: str):
        try:
            self._fernet = Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            raise CryptoError("FIELD_ENCRYPTION_KEY is not a valid Fernet key") from exc

    def encrypt(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, value: bytes | None) -> str:
        if value is None:
            return ""
        try:
            return self._fernet.decrypt(value).decode("utf-8")
        except InvalidToken as exc:
            raise CryptoError("Encrypted field could not be authenticated") from exc


class SessionManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    def create(self, db: Session, kind: str, account_id: str | None = None) -> tuple[str, str]:
        raw_id, raw_csrf = generate_token(), generate_token()
        db.add(ServerSession(
            id_hash=keyed_hash(raw_id, self.settings.session_secret),
            csrf_hash=keyed_hash(raw_csrf, self.settings.csrf_secret),
            kind=kind,
            account_id=account_id,
            expires_at=datetime.utcnow() + timedelta(minutes=self.settings.session_ttl_minutes),
        ))
        db.flush()
        return raw_id, raw_csrf

    def resolve(self, db: Session, raw_id: str | None, kind: str) -> ServerSession | None:
        if not raw_id:
            return None
        session = db.get(ServerSession, keyed_hash(raw_id, self.settings.session_secret))
        if not session or session.kind != kind or session.expires_at <= datetime.utcnow():
            return None
        return session

    def verify_csrf(self, session: ServerSession, raw_csrf: str | None) -> bool:
        if not raw_csrf:
            return False
        return hmac.compare_digest(session.csrf_hash, keyed_hash(raw_csrf, self.settings.csrf_secret))

    def revoke(self, db: Session, raw_id: str | None) -> None:
        if raw_id:
            db.execute(delete(ServerSession).where(
                ServerSession.id_hash == keyed_hash(raw_id, self.settings.session_secret)
            ))

    def purge_expired(self, db: Session) -> int:
        result = db.execute(delete(ServerSession).where(ServerSession.expires_at <= datetime.utcnow()))
        return result.rowcount or 0
