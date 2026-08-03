from __future__ import annotations

import html
import re
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main
from app.models import RateLimitBucket
from app.rate_limit import (
    PersistentRateLimiter, RateLimitPolicy, policy_for_path,
    purge_expired_rate_limits,
)


def csrf_from(response_text: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', response_text)
    assert match
    return html.unescape(match.group(1))


def test_action_policies_are_separate(settings):
    registration, registration_subject = policy_for_path("/account/create", settings)
    login, login_subject = policy_for_path("/account/secret/login", settings)
    confirmation, confirmation_subject = policy_for_path(
        "/verify-contact/confirmation-secret", settings
    )
    notification, notification_subject = policy_for_path(
        "/notify/notification-secret/confirm", settings
    )

    assert registration.action == "account_creation"
    assert registration_subject is None
    assert login.action == "account_login"
    assert login_subject == "secret"
    assert confirmation.action == "contact_confirmation"
    assert confirmation_subject == "confirmation-secret"
    assert notification.action == "notification_submission"
    assert notification_subject == "notification-secret"


def test_limit_is_persistent_and_subject_scope_blocks_multiple_clients(db, settings):
    policy = RateLimitPolicy("test", 60, client_limit=5, subject_limit=2, global_limit=20)
    now = datetime(2026, 8, 3, 12, 0, 1)

    first = PersistentRateLimiter(settings).check(
        db, policy, "198.51.100.1", "secret-token", now
    )
    db.commit()
    second = PersistentRateLimiter(settings).check(
        db, policy, "198.51.100.2", "secret-token", now
    )
    db.commit()
    third = PersistentRateLimiter(settings).check(
        db, policy, "198.51.100.3", "secret-token", now
    )
    db.commit()

    assert first.allowed
    assert second.allowed
    assert not third.allowed


def test_buckets_store_no_plain_client_or_secret_and_expire(db, settings):
    policy = RateLimitPolicy("test", 60, client_limit=5, subject_limit=5, global_limit=20)
    now = datetime(2026, 8, 3, 12, 0, 1)
    limiter = PersistentRateLimiter(settings)
    limiter.check(db, policy, "2001:db8::1234", "secret-token", now)
    db.commit()

    rows = list(db.scalars(select(RateLimitBucket)))
    assert len(rows) == 3
    serialized = " ".join(
        f"{row.id_hash} {row.action}" for row in rows
    )
    assert "2001:db8" not in serialized
    assert "secret-token" not in serialized

    limiter.check(
        db, policy, "198.51.100.1", "new-token", now + timedelta(seconds=61)
    )
    db.commit()
    assert all(row.expires_at > now + timedelta(seconds=61) for row in db.scalars(select(RateLimitBucket)))


def test_bucket_capacity_fails_closed(db, settings):
    settings.rate_limit_max_buckets = 2
    policy = RateLimitPolicy("test", 60, client_limit=5, subject_limit=5, global_limit=20)
    decision = PersistentRateLimiter(settings).check(
        db, policy, "198.51.100.1", "secret-token", datetime(2026, 8, 3, 12, 0, 1)
    )
    db.commit()

    assert not decision.allowed
    assert len(list(db.scalars(select(RateLimitBucket)))) == 2


def test_scheduler_cleanup_removes_only_expired_buckets(db, settings):
    policy = RateLimitPolicy("test", 60, client_limit=5, subject_limit=None, global_limit=20)
    now = datetime(2026, 8, 3, 12, 0, 1)
    limiter = PersistentRateLimiter(settings)
    limiter.check(db, policy, "198.51.100.1", now=now)
    db.commit()

    assert purge_expired_rate_limits(db, now + timedelta(seconds=30)) == 0
    assert purge_expired_rate_limits(db, now + timedelta(seconds=61)) == 2
    db.commit()
    assert list(db.scalars(select(RateLimitBucket))) == []


def test_global_limit_stops_new_client_buckets(db, settings):
    policy = RateLimitPolicy("test", 60, client_limit=5, subject_limit=None, global_limit=2)
    now = datetime(2026, 8, 3, 12, 0, 1)
    limiter = PersistentRateLimiter(settings)
    assert limiter.check(db, policy, "198.51.100.1", now=now).allowed
    db.commit()
    assert limiter.check(db, policy, "198.51.100.2", now=now).allowed
    db.commit()
    assert not limiter.check(db, policy, "198.51.100.3", now=now).allowed
    db.commit()

    rows = list(db.scalars(select(RateLimitBucket)))
    assert len(rows) == 3


def test_get_requests_do_not_consume_login_limit_and_restart_does_not_clear_it():
    previous_attempts = main.settings.rate_limit_login_attempts
    main.settings.rate_limit_login_attempts = 2
    try:
        with TestClient(main.app) as client:
            for _ in range(20):
                assert client.get("/admin/login").status_code == 200
            assert client.post(
                "/admin/login", data={"username": "wrong", "password": "wrong"}
            ).status_code == 401
        with TestClient(main.app) as client:
            assert client.post(
                "/admin/login", data={"username": "wrong", "password": "wrong"}
            ).status_code == 401
            blocked = client.post(
                "/admin/login", data={"username": "wrong", "password": "wrong"}
            )
    finally:
        main.settings.rate_limit_login_attempts = previous_attempts

    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"]
    assert blocked.headers["Cache-Control"] == "no-store"
