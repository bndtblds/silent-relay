from datetime import datetime, timedelta
import threading

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountReview,
    ContactMethod,
    ContactReview,
    Delivery,
    DeliveryStatus,
    NotificationStatus,
    ReviewReminder,
)
from app.model_base import Base
from app.providers.base import DeliveryResult
from app.scheduler import jobs
from app.services import AccountService, DeliveryService, ManagementService, NotificationService


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


def test_scheduler_does_not_retry_delivery_after_authorization_is_revoked(
    db, settings, cipher, monkeypatch
):
    service = AccountService(settings, cipher)
    account, _, setup = service.create(db)
    _, verification = service.setup(
        db, setup, "correct horse battery staple", "owner@example.org"
    )
    service.verify_contact(db, verification)
    management = ManagementService(settings, cipher)
    origin = management.add_partner(db, account.id, "Origin")
    _, trusted_token = management.add_trusted_person(
        db, account.id, "partner", origin.id, ""
    )
    notifications = NotificationService(settings, cipher)
    notification = notifications.accept(
        db,
        notifications.stage(
            db,
            notifications.resolve_person(db, trusted_token),
            "A sufficiently long confidential message.",
        ),
    )
    contact = db.scalar(select(ContactMethod).where(
        ContactMethod.owner_type == "account",
        ContactMethod.account_id == account.id,
    ))
    contact.is_verified = False
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

    first = jobs.run_jobs(settings)
    second = jobs.run_jobs(settings)

    delivery = db.scalar(select(Delivery).where(
        Delivery.notification_id == notification.id
    ))
    db.refresh(notification)
    assert first["deliveries"] == 1
    assert second["deliveries"] == 0
    assert RecordingProvider.sent == []
    assert delivery.status == DeliveryStatus.cancelled
    assert notification.status == NotificationStatus.discarded


def test_overlapping_scheduler_run_cannot_take_valid_claim(
    tmp_path, settings, cipher
):
    engine = create_engine(f"sqlite:///{(tmp_path / 'overlap.db').as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as setup_db:
        service = AccountService(settings, cipher)
        account, _, setup = service.create(setup_db)
        _, verification = service.setup(
            setup_db, setup, "correct horse battery staple", "owner@example.org"
        )
        service.verify_contact(setup_db, verification)
        management = ManagementService(settings, cipher)
        origin = management.add_partner(setup_db, account.id, "Origin")
        _, token = management.add_trusted_person(
            setup_db, account.id, "partner", origin.id, ""
        )
        notifications = NotificationService(settings, cipher)
        notifications.accept(
            setup_db,
            notifications.stage(
                setup_db,
                notifications.resolve_person(setup_db, token),
                "A sufficiently long confidential message.",
            ),
        )

    entered_provider = threading.Event()
    release_provider = threading.Event()
    results = []

    class BlockingProvider(RecordingProvider):
        def send(self, *args, **kwargs):
            entered_provider.set()
            assert release_provider.wait(timeout=5)
            return super().send(*args, **kwargs)

    def run(provider):
        with Session(engine, expire_on_commit=False) as worker_db:
            results.append(
                DeliveryService(settings, cipher, {"email": provider}).process_due(worker_db)
            )

    RecordingProvider.sent = []
    first = threading.Thread(target=run, args=(BlockingProvider(settings),))
    first.start()
    assert entered_provider.wait(timeout=5)
    second = threading.Thread(target=run, args=(RecordingProvider(settings),))
    second.start()
    second.join(timeout=5)
    release_provider.set()
    first.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert sorted(results) == [0, 1]
    assert len(RecordingProvider.sent) == 1
    engine.dispose()
