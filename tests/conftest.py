from __future__ import annotations

import os

from cryptography.fernet import Fernet

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_BASE_URL", "http://testserver")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_http.db")
os.environ.setdefault("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("TOKEN_HMAC_KEY", "token-key-that-is-at-least-thirty-two-bytes")
os.environ.setdefault("FINGERPRINT_HMAC_KEY", "fingerprint-key-at-least-thirty-two-bytes")
os.environ.setdefault("SESSION_SECRET", "session-secret-at-least-thirty-two-bytes")
os.environ.setdefault("CSRF_SECRET", "csrf-secret-that-is-at-least-thirty-two")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQxMjM0NTY3OA$g0mYyNn5iO1Hc4gYz0j/IFakeHashNotUsed")
os.environ.setdefault("SECURE_COOKIES", "false")
os.environ.setdefault("HSTS_ENABLED", "false")
os.environ.setdefault("DEFAULT_LANGUAGE", "de")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.database import Base
from app.database import engine as app_engine
from app.models import *  # noqa: F403
from app.security.core import FieldCipher


@pytest.fixture
def settings():
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def cipher(settings):
    return FieldCipher(settings.field_encryption_key)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def application_schema():
    Base.metadata.create_all(app_engine)
    yield
    Base.metadata.drop_all(app_engine)
    app_engine.dispose()
