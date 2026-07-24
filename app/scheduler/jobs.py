from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.config import Settings
from app.database import SessionLocal
from app.models import AccountReview, ContactMethod, ReviewReminder
from app.security.core import FieldCipher, SessionManager
from app.services import DeliveryService, LifecycleService
from app.smtp_config import load_email_provider


def run_jobs(settings: Settings) -> dict[str, int]:
    cipher = FieldCipher(settings.field_encryption_key)
    with SessionLocal() as db:
        provider = load_email_provider(db, settings, cipher)
        deliveries = DeliveryService(settings, cipher, {"email": provider}).process_due(db)
        now = datetime.utcnow()
        reminders = 0
        due = list(db.scalars(select(ReviewReminder).where(
            ReviewReminder.sent_at.is_(None), ReviewReminder.scheduled_at <= now
        )))
        for reminder in due:
            review = db.get(AccountReview, reminder.account_review_id)
            if not review or review.confirmed_at:
                reminder.sent_at = now
                continue
            contacts = list(db.scalars(select(ContactMethod).where(
                ContactMethod.account_id == review.account_id,
                ContactMethod.owner_type == "account",
                ContactMethod.is_verified.is_(True),
                ContactMethod.is_active.is_(True),
            )))
            all_sent = bool(contacts)
            for contact in contacts:
                result = provider.send(
                    cipher.decrypt(contact.encrypted_value), "SilentRelay: Kontoprüfung",
                    f"Bitte prüfen und bestätigen Sie Ihr SilentRelay-Konto. Fälligkeit: {review.review_due_at.date().isoformat()}",
                )
                all_sent = all_sent and result.successful
            if all_sent:
                reminder.sent_at = now
                reminders += 1
            db.commit()
        LifecycleService(settings).run(db, now)
        sessions = SessionManager(settings).purge_expired(db)
        db.commit()
    return {"deliveries": deliveries, "reminders": reminders, "sessions": sessions}
