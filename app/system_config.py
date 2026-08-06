from sqlalchemy.orm import Session

from app.models import SystemConfiguration


DEFAULT_NOTIFICATION_DELAY_MINUTES = 10
MAX_NOTIFICATION_DELAY_MINUTES = 1440


DEFAULTS = {
    "notification_delay_minutes": 10,
    "account_creation_enabled": True,
    "account_pending_retention_days": 7,
    "account_review_interval_days": 180,
    "account_review_reminder_days": "-30,-15,-3,0,30",
    "account_review_grace_days": 60,
    "contact_problem_reminder_days": 7,
    "account_retention_after_disable_days": 365,
    "message_retention_hours": 48,
    "audit_retention_days": 90,
}


def system_configuration(db: Session) -> SystemConfiguration:
    stored = db.get(SystemConfiguration, "default")
    if stored:
        return stored
    stored = SystemConfiguration(id="default", **DEFAULTS)
    db.add(stored)
    db.flush()
    return stored


def notification_delay_minutes(db: Session) -> int:
    return system_configuration(db).notification_delay_minutes


def save_notification_delay(db: Session, minutes: int) -> SystemConfiguration:
    if not 0 <= minutes <= MAX_NOTIFICATION_DELAY_MINUTES:
        raise ValueError
    stored = system_configuration(db)
    stored.notification_delay_minutes = minutes
    return stored


def review_reminder_days(config: SystemConfiguration) -> list[int]:
    return sorted({int(value) for value in config.account_review_reminder_days.split(",")})


def save_account_creation(
    db: Session, enabled: bool
) -> SystemConfiguration:
    stored = system_configuration(db)
    stored.account_creation_enabled = enabled
    return stored


def save_retention_settings(
    db: Session,
    *,
    account_pending_retention_days: int,
    account_review_interval_days: int,
    account_review_reminder_days: str,
    account_review_grace_days: int,
    contact_problem_reminder_days: int,
    account_retention_after_disable_days: int,
    message_retention_hours: int,
    audit_retention_days: int,
) -> SystemConfiguration:
    try:
        reminder_days = sorted({
            int(value.strip())
            for value in account_review_reminder_days.split(",")
            if value.strip()
        })
    except ValueError as exc:
        raise ValueError from exc
    if (
        not reminder_days
        or any(abs(value) > 3650 for value in reminder_days)
        or account_pending_retention_days < 1
        or account_review_interval_days < 1
        or account_review_grace_days < 0
        or contact_problem_reminder_days < 1
        or account_retention_after_disable_days < 0
        or message_retention_hours < 1
        or audit_retention_days < 1
    ):
        raise ValueError
    stored = system_configuration(db)
    stored.account_pending_retention_days = account_pending_retention_days
    stored.account_review_interval_days = account_review_interval_days
    stored.account_review_reminder_days = ",".join(str(value) for value in reminder_days)
    stored.account_review_grace_days = account_review_grace_days
    stored.contact_problem_reminder_days = contact_problem_reminder_days
    stored.account_retention_after_disable_days = account_retention_after_disable_days
    stored.message_retention_hours = message_retention_hours
    stored.audit_retention_days = audit_retention_days
    return stored
