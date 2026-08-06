from sqlalchemy.orm import Session

from app.models import SystemConfiguration


DEFAULT_NOTIFICATION_DELAY_MINUTES = 10
MAX_NOTIFICATION_DELAY_MINUTES = 1440


def notification_delay_minutes(db: Session) -> int:
    stored = db.get(SystemConfiguration, "default")
    return (
        stored.notification_delay_minutes
        if stored else DEFAULT_NOTIFICATION_DELAY_MINUTES
    )


def save_notification_delay(db: Session, minutes: int) -> SystemConfiguration:
    if not 0 <= minutes <= MAX_NOTIFICATION_DELAY_MINUTES:
        raise ValueError
    stored = db.get(SystemConfiguration, "default")
    if stored:
        stored.notification_delay_minutes = minutes
    else:
        stored = SystemConfiguration(
            id="default", notification_delay_minutes=minutes
        )
        db.add(stored)
    return stored
