from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AccountReview, ContactMethod, ContactReview, ReviewReminder
from app.providers.base import DeliveryResult
from app.scheduler import jobs
from app.services import AccountService, ManagementService


class RecordingProvider:
    channel = "email"
    sent = []
    messages = []

    def __init__(self, settings):
        pass

    def send(self, recipient, subject, body, *, envelope_token=None):
        self.sent.append((recipient, subject))
        self.messages.append((recipient, subject, body))
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
    RecordingProvider.messages = []
    monkeypatch.setattr(jobs, "load_email_provider", lambda db, settings, cipher: RecordingProvider(settings))
    monkeypatch.setattr(jobs, "SessionLocal", lambda: Session(db.get_bind(), expire_on_commit=False))
    jobs.run_jobs(settings)
    jobs.run_jobs(settings)

    assert RecordingProvider.sent == [
        ("owner@example.org", "SilentRelay: Regelmäßige Kontoprüfung")
    ]
    assert "/account/" not in RecordingProvider.messages[0][2]


def test_due_review_sends_one_time_confirmation_to_every_contact(
    db, settings, cipher, monkeypatch
):
    service = AccountService(settings, cipher)
    account, _, setup = service.create(db)
    _, verification = service.setup(
        db, setup, "correct horse battery staple", "owner@example.org"
    )
    service.verify_contact(db, verification)
    management = ManagementService(settings, cipher)
    partner = management.add_partner(db, account.id, "Partner")
    partner_token = management.add_contact(
        db, account.id, "partner", partner.id, "partner@example.org"
    )
    service.verify_contact(db, partner_token)
    review = db.scalar(select(AccountReview).where(
        AccountReview.account_id == account.id,
        AccountReview.confirmed_at.is_(None),
    ))
    review.review_due_at = datetime.utcnow() - timedelta(seconds=1)
    account.next_review_due_at = review.review_due_at
    account.review_grace_due_at = datetime.utcnow() + timedelta(days=60)
    for reminder in db.scalars(select(ReviewReminder).where(
        ReviewReminder.account_review_id == review.id
    )):
        reminder.scheduled_at = (
            datetime.utcnow() - timedelta(seconds=1)
            if reminder.relative_day == 0
            else datetime.utcnow() + timedelta(days=90)
        )
    db.commit()

    RecordingProvider.sent = []
    RecordingProvider.messages = []
    monkeypatch.setattr(
        jobs,
        "load_email_provider",
        lambda db, settings, cipher: RecordingProvider(settings),
    )
    monkeypatch.setattr(
        jobs, "SessionLocal", lambda: Session(db.get_bind(), expire_on_commit=False)
    )
    jobs.run_jobs(settings)
    jobs.run_jobs(settings)

    confirmation_messages = [
        message for message in RecordingProvider.messages
        if message[1] == "SilentRelay: E-Mail-Adresse erneut bestätigen"
    ]
    assert {message[0] for message in confirmation_messages} == {
        "owner@example.org",
        "partner@example.org",
    }
    assert all("/verify-contact/" in message[2] for message in confirmation_messages)
    assert db.scalar(select(func.count()).select_from(ContactReview)) == 2


def test_account_owner_is_reminded_about_broken_partner_contact(
    db, settings, cipher, monkeypatch
):
    service = AccountService(settings, cipher)
    account, _, setup = service.create(db)
    _, verification = service.setup(
        db, setup, "correct horse battery staple", "owner@example.org"
    )
    service.verify_contact(db, verification)
    management = ManagementService(settings, cipher)
    partner = management.add_partner(db, account.id, "Partner")
    partner_token = management.add_contact(
        db, account.id, "partner", partner.id, "broken@example.org"
    )
    service.verify_contact(db, partner_token)
    broken = db.scalar(select(ContactMethod).where(
        ContactMethod.owner_type == "partner",
        ContactMethod.owner_id == partner.id,
    ))
    broken.is_verified = False
    broken.last_permanent_failure_at = datetime.utcnow()
    db.commit()

    RecordingProvider.sent = []
    RecordingProvider.messages = []
    monkeypatch.setattr(
        jobs,
        "load_email_provider",
        lambda db, settings, cipher: RecordingProvider(settings),
    )
    monkeypatch.setattr(
        jobs, "SessionLocal", lambda: Session(db.get_bind(), expire_on_commit=False)
    )
    jobs.run_jobs(settings)
    jobs.run_jobs(settings)

    problem_messages = [
        message for message in RecordingProvider.messages
        if message[1] == "SilentRelay: Kontaktwege müssen geprüft werden"
    ]
    assert len(problem_messages) == 1
    assert problem_messages[0][0] == "owner@example.org"
    assert "Partner: broken@example.org" in problem_messages[0][2]
