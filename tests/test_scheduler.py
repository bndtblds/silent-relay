from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ReviewReminder
from app.providers.base import DeliveryResult
from app.scheduler import jobs
from app.services import AccountService


class RecordingProvider:
    channel = "email"
    sent = []

    def __init__(self, settings):
        pass

    def send(self, recipient, subject, body, *, envelope_token=None):
        self.sent.append((recipient, subject))
        return DeliveryResult(True)


def test_review_reminder_is_not_sent_twice(db, settings, cipher, monkeypatch):
    service = AccountService(settings, cipher)
    _, _, setup = service.create(db)
    _, verification = service.setup(db, setup, "correct horse battery staple", "owner@example.org")
    service.verify_contact(db, verification)
    reminder = db.scalar(select(ReviewReminder).order_by(ReviewReminder.scheduled_at))
    reminder.scheduled_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()

    RecordingProvider.sent = []
    monkeypatch.setattr(jobs, "load_email_provider", lambda db, settings, cipher: RecordingProvider(settings))
    monkeypatch.setattr(jobs, "SessionLocal", lambda: Session(db.get_bind(), expire_on_commit=False))
    jobs.run_jobs(settings)
    jobs.run_jobs(settings)

    assert RecordingProvider.sent == [("owner@example.org", "SilentRelay: Kontoprüfung")]
