from datetime import timedelta
import json
import logging
import threading

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountReview,
    ContactMethod,
    ContactReview,
    Delivery,
    DeliveryStatus,
    NotificationStatus,
    PartnerCredential,
    ReviewReminder,
    SystemConfiguration,
    TrustedPersonToken,
)
from app.logging_config import JsonFormatter
from app.model_base import Base
from app.providers.base import DeliveryResult
from app.scheduler import jobs
from app.scheduler import main as scheduler_main
from app.security.core import hash_pin
from app.services import AccountService, DeliveryService, ManagementService, NotificationService, PartnerAuthenticationService
from app.time import utc_now


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


def test_scheduler_process_configures_shared_logging(settings, monkeypatch):
    class SchedulerStopped(Exception):
        pass

    configured_levels = []
    monkeypatch.setattr(scheduler_main, "get_settings", lambda: settings)
    monkeypatch.setattr(
        scheduler_main, "configure_logging", configured_levels.append
    )
    monkeypatch.setattr(
        scheduler_main, "run_jobs", lambda settings: (_ for _ in ()).throw(SchedulerStopped)
    )

    with pytest.raises(SchedulerStopped):
        scheduler_main.main()

    assert configured_levels == [settings.log_level]


def test_ndr_failure_is_logged_safely_and_later_jobs_continue(
    db, settings, cipher, monkeypatch, caplog
):
    secret = "owner@example.org tracking-token imap-password"
    provider_loaded = False

    class FailingNdrProcessor:
        def __init__(self, settings, cipher):
            pass

        def process(self, db):
            raise TimeoutError(secret)

    def load_provider(db, settings, cipher):
        nonlocal provider_loaded
        provider_loaded = True
        return RecordingProvider(settings)

    monkeypatch.setattr(jobs, "NdrMailboxProcessor", FailingNdrProcessor)
    monkeypatch.setattr(jobs, "load_email_provider", load_provider)
    monkeypatch.setattr(
        jobs, "SessionLocal", lambda: Session(db.get_bind(), expire_on_commit=False)
    )

    with caplog.at_level(logging.ERROR, logger="silent_relay"):
        result = jobs.run_jobs(settings)

    records = [
        record for record in caplog.records
        if record.getMessage() == "ndr_processing_failed"
    ]
    assert len(records) == 1
    payload = json.loads(JsonFormatter().format(records[0]))
    assert payload["event"] == "ndr_processing_failed"
    assert payload["error_class"] == "TimeoutError"
    assert secret not in json.dumps(payload)
    assert records[0].exc_info is None
    assert result["ndr_reports"] == 0
    assert provider_loaded


def test_account_owner_is_notified_about_trusted_pin_setup_and_expiry(
    db, settings, cipher, monkeypatch
):
    service = AccountService(settings, cipher)
    account, _, setup = service.create(db)
    _, verification = service.setup(
        db, setup, "correct horse battery staple", "owner@example.org"
    )
    service.verify_contact(db, verification)
    management = ManagementService(settings, cipher)
    first, _ = management.add_trusted_person(
        db, account.id, "account", account.id, "First"
    )
    first_token = db.get(TrustedPersonToken, first.id)
    first_token.pin_hash = hash_pin("472915")
    first_token.enrolled_at = utc_now()
    second, _ = management.add_trusted_person(
        db, account.id, "account", account.id, "Second"
    )
    second_token = db.get(TrustedPersonToken, second.id)
    second_token.enrollment_expires_at = utc_now() - timedelta(seconds=1)
    db.commit()

    RecordingProvider.sent = []
    RecordingProvider.messages = []
    monkeypatch.setattr(
        jobs, "load_email_provider",
        lambda db, settings, cipher: RecordingProvider(settings),
    )
    monkeypatch.setattr(
        jobs, "SessionLocal", lambda: Session(db.get_bind(), expire_on_commit=False)
    )
    first_run = jobs.run_jobs(settings)
    second_run = jobs.run_jobs(settings)

    subjects = [message[1] for message in RecordingProvider.messages]
    assert first_run["trusted_access_notices"] == 2
    assert second_run["trusted_access_notices"] == 0
    assert subjects == [
        "SilentRelay: PIN für Vertrauensperson eingerichtet",
        "SilentRelay: Zugang einer Vertrauensperson abgelaufen",
    ]
    assert all("472915" not in message[2] for message in RecordingProvider.messages)


def test_account_owner_is_neutrally_notified_about_partner_setup(
    db, settings, cipher, monkeypatch
):
    service = AccountService(settings, cipher)
    account, _, setup = service.create(db)
    _, verification = service.setup(
        db, setup, "correct horse battery staple", "owner@example.org"
    )
    service.verify_contact(db, verification)
    partner, _ = ManagementService(settings, cipher).add_partner_with_access(
        db, account.id, "Private Partner Name"
    )
    PartnerAuthenticationService(settings).enroll(
        db, db.get(PartnerCredential, partner.id), "partner secure password"
    )
    RecordingProvider.sent = []
    RecordingProvider.messages = []
    monkeypatch.setattr(jobs, "load_email_provider", lambda db, settings, cipher: RecordingProvider(settings))
    monkeypatch.setattr(jobs, "SessionLocal", lambda: Session(db.get_bind(), expire_on_commit=False))
    result = jobs.run_jobs(settings)
    assert result["partner_access_notices"] == 1
    body = next(message[2] for message in RecordingProvider.messages if "Partnerzugang eingerichtet" in message[1])
    assert "Private Partner Name" not in body
    assert "/partner/access/" not in body


def test_review_reminder_is_not_sent_twice(db, settings, cipher, monkeypatch):
    service = AccountService(settings, cipher)
    _, _, setup = service.create(db)
    _, verification = service.setup(db, setup, "correct horse battery staple", "owner@example.org")
    service.verify_contact(db, verification)
    reminder = db.scalar(select(ReviewReminder).order_by(ReviewReminder.scheduled_at))
    reminder.scheduled_at = utc_now() - timedelta(seconds=1)
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
    review.review_due_at = utc_now() - timedelta(seconds=1)
    account.next_review_due_at = review.review_due_at
    account.review_grace_due_at = utc_now() + timedelta(days=60)
    for reminder in db.scalars(select(ReviewReminder).where(
        ReviewReminder.account_review_id == review.id
    )):
        reminder.scheduled_at = (
            utc_now() - timedelta(seconds=1)
            if reminder.relative_day == 0
            else utc_now() + timedelta(days=90)
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
    broken.last_permanent_failure_at = utc_now()
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
    person = notifications.resolve_person(db, trusted_token)
    notification = notifications.accept(
        db,
        notifications.stage(
            db,
            person,
            "A sufficiently long confidential message.",
        ),
        person.id,
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
        setup_db.add(SystemConfiguration(id="default", notification_delay_minutes=0))
        setup_db.commit()
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
        person = notifications.resolve_person(setup_db, token)
        notifications.accept(
            setup_db,
            notifications.stage(
                setup_db,
                person,
                "A sufficiently long confidential message.",
            ),
            person.id,
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
