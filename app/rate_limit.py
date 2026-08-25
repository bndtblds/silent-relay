from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address, ip_network
from math import ceil

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import RateLimitBucket
from app.security.core import keyed_hash
from app.time import as_utc, utc_now


@dataclass(frozen=True)
class RateLimitPolicy:
    action: str
    window_seconds: int
    client_limit: int
    subject_limit: int | None
    global_limit: int


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int


def _positive(value: int) -> int:
    return max(1, value)


def policy_for_path(path: str, settings: Settings) -> tuple[RateLimitPolicy, str | None]:
    parts = [part for part in path.split("/") if part]
    if path == "/admin/login":
        return RateLimitPolicy(
            "admin_login", settings.rate_limit_login_window_seconds,
            settings.rate_limit_login_attempts, None,
            settings.rate_limit_login_attempts * 40,
        ), None
    if path == "/account/create":
        return RateLimitPolicy(
            "account_creation", settings.rate_limit_account_creation_window_seconds,
            settings.rate_limit_account_creation_attempts, None,
            settings.rate_limit_account_creation_attempts * 40,
        ), None
    if len(parts) == 3 and parts[:2] == ["account", "setup"]:
        return RateLimitPolicy(
            "account_setup", settings.rate_limit_login_window_seconds,
            settings.rate_limit_login_attempts, settings.rate_limit_login_attempts * 2,
            settings.rate_limit_login_attempts * 100,
        ), parts[2]
    if len(parts) == 3 and parts[0] == "account" and parts[2] == "login":
        return RateLimitPolicy(
            "account_login", settings.rate_limit_login_window_seconds,
            settings.rate_limit_login_attempts, settings.rate_limit_login_attempts * 2,
            settings.rate_limit_login_attempts * 100,
        ), parts[1]
    if len(parts) == 2 and parts[0] == "verify-contact":
        return RateLimitPolicy(
            "contact_confirmation", settings.rate_limit_contact_confirmation_window_seconds,
            settings.rate_limit_contact_confirmation_attempts,
            settings.rate_limit_contact_confirmation_attempts,
            settings.rate_limit_contact_confirmation_attempts * 100,
        ), parts[1]
    if len(parts) == 3 and parts[0] == "notify" and parts[2] in {"setup", "login"}:
        return RateLimitPolicy(
            f"trusted_pin_{parts[2]}", settings.rate_limit_login_window_seconds,
            settings.rate_limit_login_attempts,
            settings.rate_limit_login_attempts * 2,
            settings.rate_limit_login_attempts * 100,
        ), parts[1]
    if len(parts) == 4 and parts[:2] == ["partner", "access"] and parts[3] in {"setup", "login"}:
        return RateLimitPolicy(
            f"partner_{parts[3]}", settings.rate_limit_login_window_seconds,
            settings.rate_limit_login_attempts,
            settings.rate_limit_login_attempts * 2,
            settings.rate_limit_login_attempts * 100,
        ), parts[2]
    if len(parts) in {2, 3} and parts[0] == "notify":
        return RateLimitPolicy(
            "notification_submission", settings.rate_limit_notification_window_seconds,
            settings.rate_limit_notification_attempts * 2,
            settings.rate_limit_notification_attempts * 2,
            settings.rate_limit_notification_attempts * 200,
        ), parts[1]
    return RateLimitPolicy(
        "authenticated_action", settings.rate_limit_window_seconds,
        settings.rate_limit_default, None, settings.rate_limit_default * 100,
    ), None


def anonymized_client(client_host: str) -> str:
    try:
        address = ip_address(client_host)
    except ValueError:
        return client_host
    if address.version == 6:
        return str(ip_network(f"{address}/64", strict=False))
    return address.compressed


class PersistentRateLimiter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def check(
        self,
        db: Session,
        policy: RateLimitPolicy,
        client_host: str,
        subject: str | None = None,
        now: datetime | None = None,
    ) -> RateLimitDecision:
        now = as_utc(now or utc_now())
        window_seconds = _positive(policy.window_seconds)
        utc_epoch = datetime(1970, 1, 1, tzinfo=UTC)
        window_epoch = (
            int((now - utc_epoch).total_seconds()) // window_seconds * window_seconds
        )
        window_start = utc_epoch + timedelta(seconds=window_epoch)
        expires_at = window_start + timedelta(seconds=window_seconds)
        db.execute(delete(RateLimitBucket).where(RateLimitBucket.expires_at <= now))

        scopes = [
            ("global", policy.global_limit, True),
            (f"client:{anonymized_client(client_host)}", policy.client_limit, False),
        ]
        if subject is not None and policy.subject_limit is not None:
            scopes.append((f"subject:{subject}", policy.subject_limit, False))

        allowed = True
        for scope, limit, is_global in scopes:
            bucket_hash = keyed_hash(
                f"{policy.action}:{scope}:{window_epoch}",
                self.settings.fingerprint_hmac_key,
            )
            existing = db.get(RateLimitBucket, bucket_hash)
            if existing is None:
                bucket_count = db.scalar(select(func.count()).select_from(RateLimitBucket)) or 0
                if bucket_count >= self.settings.rate_limit_max_buckets:
                    allowed = False
                    if is_global:
                        break
                    continue
            statement = sqlite_insert(RateLimitBucket).values(
                id_hash=bucket_hash,
                action=policy.action,
                request_count=1,
                window_started_at=window_start,
                expires_at=expires_at,
            ).on_conflict_do_update(
                index_elements=[RateLimitBucket.id_hash],
                set_={"request_count": RateLimitBucket.request_count + 1},
            ).returning(RateLimitBucket.request_count)
            count = db.scalar(statement)
            if count is None or count > _positive(limit):
                allowed = False
                if is_global:
                    break

        retry_after = max(1, ceil((expires_at - now).total_seconds()))
        return RateLimitDecision(allowed, retry_after)


def purge_expired_rate_limits(db: Session, now: datetime | None = None) -> int:
    result = db.execute(
        delete(RateLimitBucket).where(RateLimitBucket.expires_at <= (now or utc_now()))
    )
    return result.rowcount or 0
