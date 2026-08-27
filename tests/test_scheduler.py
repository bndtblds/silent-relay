from datetime import timedelta
import json
import logging
import threading
import time

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import (
    Account,
    AccountReview,
    ContactMethod,
    ContactReview,
    Delivery,
    DeliveryStatus,
    NotificationStatus,
    PartnerCredential,
    ReviewReminder,
    ReviewReminderDelivery,
    ReviewReminderDeliveryStatus,
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


def _create_due_review_reminder(engine) -> str:
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        account = Account(encrypted_owner_name=None)
        session.add(account)
        session.flush()
        review = AccountReview(
            account_id=account.id,
            review_due_at=utc_now() - timedelta(days=1),
        )
        session.add(review)
        session.flush()
        reminder = ReviewReminder(
            account_review_id=review.id,
            relative_day=0,
            scheduled_at=utc_now() - timedelta(seconds=1),
        )
        session.add(reminder)
        session.commit()
        return reminder.id


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


def test_two_database_sessions_cannot_claim_same_review_reminder(
    tmp_path,
):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'claims.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    reminder_id = _create_due_review_reminder(engine)
    now = utc_now()
    barrier = threading.Barrier(2)
    claims = []

    def claim() -> None:
        with Session(engine, expire_on_commit=False) as session:
            barrier.wait()
            claims.append(
                jobs._claim_review_reminder(session, reminder_id, now) is not None
            )

    workers = [threading.Thread(target=claim) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert sorted(claims) == [False, True]
    with Session(engine) as session:
        reminder = session.get(ReviewReminder, reminder_id)
        assert reminder.processing_started_at == now
        assert reminder.processing_until == (
            now + jobs.REVIEW_REMINDER_PROCESSING_LEASE
        )
    engine.dispose()


def test_review_reminder_lease_recovery_has_exactly_one_new_owner(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'recovery.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    reminder_id = _create_due_review_reminder(engine)
    initial_now = utc_now()
    with Session(engine, expire_on_commit=False) as first_session:
        assert jobs._claim_review_reminder(
            first_session, reminder_id, initial_now
        ) is not None

    with Session(engine, expire_on_commit=False) as fresh_session:
        assert jobs._claim_review_reminder(
            fresh_session, reminder_id, initial_now + timedelta(minutes=1)
        ) is None

    recovery_now = initial_now + jobs.REVIEW_REMINDER_PROCESSING_LEASE
    with Session(engine, expire_on_commit=False) as first_recovery_session:
        recovered = jobs._claim_review_reminder(
            first_recovery_session, reminder_id, recovery_now
        )
        assert recovered is not None
    with Session(engine, expire_on_commit=False) as second_recovery_session:
        assert jobs._claim_review_reminder(
            second_recovery_session, reminder_id, recovery_now
        ) is None
    engine.dispose()


def test_completed_review_reminder_cannot_be_claimed_again(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'completed-claim.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    reminder_id = _create_due_review_reminder(engine)
    now = utc_now()
    with Session(engine, expire_on_commit=False) as first_session:
        reminder = jobs._claim_review_reminder(first_session, reminder_id, now)
        assert reminder is not None

    with Session(engine, expire_on_commit=False) as second_session:
        assert jobs._claim_review_reminder(
            second_session, reminder_id, now + timedelta(minutes=1)
        ) is None

    with Session(engine, expire_on_commit=False) as first_session:
        reminder = first_session.get(ReviewReminder, reminder_id)
        reminder.sent_at = now + timedelta(minutes=2)
        jobs._clear_review_reminder_lease(reminder)
        first_session.commit()

    with Session(engine, expire_on_commit=False) as later_session:
        assert jobs._claim_review_reminder(
            later_session,
            reminder_id,
            now + jobs.REVIEW_REMINDER_PROCESSING_LEASE + timedelta(minutes=1),
        ) is None
    engine.dispose()


def test_review_reminder_retries_only_temporarily_failed_recipient(
    db, settings, cipher, monkeypatch
):
    service = AccountService(settings, cipher)
    account, _, setup = service.create(db)
    _, verification = service.setup(
        db, setup, "correct horse battery staple", "first@example.org"
    )
    service.verify_contact(db, verification)
    second_verification = ManagementService(settings, cipher).add_contact(
        db, account.id, "account", account.id, "second@example.org"
    )
    service.verify_contact(db, second_verification)
    reminder = db.scalar(select(ReviewReminder).order_by(ReviewReminder.scheduled_at))
    reminder.scheduled_at = utc_now() - timedelta(seconds=1)
    db.commit()

    class TemporarilyFailingProvider(RecordingProvider):
        attempts = []

        def send(self, recipient, subject, body, *, envelope_token=None):
            self.attempts.append(recipient)
            if recipient == "second@example.org" and self.attempts.count(recipient) == 1:
                return DeliveryResult(False, error_class="temporary_smtp_error")
            return DeliveryResult(True)

    monkeypatch.setattr(
        jobs,
        "load_email_provider",
        lambda db, settings, cipher: TemporarilyFailingProvider(settings),
    )
    monkeypatch.setattr(
        jobs, "SessionLocal", lambda: Session(db.get_bind(), expire_on_commit=False)
    )

    first_run = jobs.run_jobs(settings)
    assert first_run["reminders"] == 0
    assert reminder.sent_at is None
    assert TemporarilyFailingProvider.attempts == [
        "first@example.org",
        "second@example.org",
    ]

    second_run = jobs.run_jobs(settings)
    db.refresh(reminder)
    assert second_run["reminders"] == 1
    assert reminder.sent_at is not None
    assert TemporarilyFailingProvider.attempts == [
        "first@example.org",
        "second@example.org",
        "second@example.org",
    ]
    statuses = set(db.scalars(select(ReviewReminderDelivery.status)))
    assert statuses == {ReviewReminderDeliveryStatus.successful}


def test_permanent_review_reminder_failure_is_terminal(
    db, settings, cipher, monkeypatch
):
    service = AccountService(settings, cipher)
    _, _, setup = service.create(db)
    _, verification = service.setup(
        db, setup, "correct horse battery staple", "owner@example.org"
    )
    service.verify_contact(db, verification)
    reminder = db.scalar(select(ReviewReminder).order_by(ReviewReminder.scheduled_at))
    reminder.scheduled_at = utc_now() - timedelta(seconds=1)
    db.commit()

    class PermanentlyFailingProvider(RecordingProvider):
        attempts = []

        def send(self, recipient, subject, body, *, envelope_token=None):
            self.attempts.append(recipient)
            return DeliveryResult(
                False,
                permanent_failure=True,
                error_class="recipient_rejected",
            )

    monkeypatch.setattr(
        jobs,
        "load_email_provider",
        lambda db, settings, cipher: PermanentlyFailingProvider(settings),
    )
    monkeypatch.setattr(
        jobs, "SessionLocal", lambda: Session(db.get_bind(), expire_on_commit=False)
    )

    first_run = jobs.run_jobs(settings)
    second_run = jobs.run_jobs(settings)
    db.refresh(reminder)
    delivery = db.scalar(select(ReviewReminderDelivery))

    assert first_run["reminders"] == 1
    assert second_run["reminders"] == 0
    assert reminder.sent_at is not None
    assert delivery.status == ReviewReminderDeliveryStatus.permanent_failure
    assert PermanentlyFailingProvider.attempts == ["owner@example.org"]


def test_pending_review_reminder_delivery_is_cancelled_for_disabled_contact(
    db, settings, cipher, monkeypatch
):
    service = AccountService(settings, cipher)
    account, _, setup = service.create(db)
    _, verification = service.setup(
        db, setup, "correct horse battery staple", "first@example.org"
    )
    service.verify_contact(db, verification)
    second_verification = ManagementService(settings, cipher).add_contact(
        db, account.id, "account", account.id, "second@example.org"
    )
    service.verify_contact(db, second_verification)
    reminder = db.scalar(select(ReviewReminder).order_by(ReviewReminder.scheduled_at))
    reminder.scheduled_at = utc_now() - timedelta(seconds=1)
    db.commit()

    class FailingSecondRecipientProvider(RecordingProvider):
        attempts = []

        def send(self, recipient, subject, body, *, envelope_token=None):
            self.attempts.append(recipient)
            if recipient == "second@example.org":
                return DeliveryResult(False, error_class="temporary_smtp_error")
            return DeliveryResult(True)

    monkeypatch.setattr(
        jobs,
        "load_email_provider",
        lambda db, settings, cipher: FailingSecondRecipientProvider(settings),
    )
    monkeypatch.setattr(
        jobs, "SessionLocal", lambda: Session(db.get_bind(), expire_on_commit=False)
    )

    jobs.run_jobs(settings)
    second_contact = next(
        contact for contact in db.scalars(select(ContactMethod).where(
            ContactMethod.account_id == account.id
        ))
        if cipher.decrypt(contact.encrypted_value) == "second@example.org"
    )
    second_contact.is_active = False
    db.commit()

    result = jobs.run_jobs(settings)
    db.refresh(reminder)
    deliveries = list(db.scalars(select(ReviewReminderDelivery)))
    statuses_by_recipient = {
        cipher.decrypt(db.get(ContactMethod, delivery.contact_method_id).encrypted_value): delivery.status
        for delivery in deliveries
    }
    assert result["reminders"] == 1
    assert reminder.sent_at is not None
    assert FailingSecondRecipientProvider.attempts == [
        "first@example.org",
        "second@example.org",
    ]
    assert statuses_by_recipient == {
        "first@example.org": ReviewReminderDeliveryStatus.successful,
        "second@example.org": ReviewReminderDeliveryStatus.cancelled,
    }


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


@pytest.mark.parametrize("release_before_timeout", [True, False])
def test_slow_smtp_holds_sqlite_write_lock_until_release_or_timeout(
    tmp_path, settings, cipher, release_before_timeout
):
    sqlite_timeout = 0.25
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'slow-smtp-lock.db').as_posix()}",
        connect_args={
            "check_same_thread": False,
            "timeout": sqlite_timeout,
        },
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
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
    writer_started = threading.Event()
    writer_finished = threading.Event()
    delivery_results = []
    delivery_errors = []
    writer_results = []

    class BlockingProvider(RecordingProvider):
        def send(self, *args, **kwargs):
            entered_provider.set()
            assert release_provider.wait(timeout=5)
            return super().send(*args, **kwargs)

    def deliver() -> None:
        try:
            with Session(engine, expire_on_commit=False) as worker_db:
                delivery_results.append(
                    DeliveryService(
                        settings, cipher, {"email": BlockingProvider(settings)}
                    ).process_due(worker_db)
                )
        except Exception as error:  # pragma: no cover - asserted below
            delivery_errors.append(error)

    def write_independently() -> None:
        started_at = time.monotonic()
        writer_started.set()
        try:
            with Session(engine) as writer_db:
                writer_db.execute(
                    update(SystemConfiguration)
                    .where(SystemConfiguration.id == "default")
                    .values(account_creation_enabled=False)
                )
                writer_db.commit()
            outcome = "committed"
        except OperationalError as error:
            outcome = "locked"
            assert "database is locked" in str(error).lower()
        finally:
            writer_results.append((outcome, time.monotonic() - started_at))
            writer_finished.set()

    RecordingProvider.sent = []
    delivery_thread = threading.Thread(target=deliver)
    delivery_thread.start()
    assert entered_provider.wait(timeout=5)

    writer_thread = threading.Thread(target=write_independently)
    writer_thread.start()
    assert writer_started.wait(timeout=5)

    if release_before_timeout:
        assert not writer_finished.wait(timeout=sqlite_timeout / 2)
        release_provider.set()
    else:
        assert writer_finished.wait(timeout=sqlite_timeout * 4)
        release_provider.set()

    writer_thread.join(timeout=5)
    delivery_thread.join(timeout=5)

    assert not writer_thread.is_alive()
    assert not delivery_thread.is_alive()
    assert delivery_errors == []
    assert delivery_results == [1]
    assert len(RecordingProvider.sent) == 1

    outcome, blocked_for = writer_results[0]
    if release_before_timeout:
        assert outcome == "committed"
        assert blocked_for >= sqlite_timeout / 2
    else:
        assert outcome == "locked"
        assert sqlite_timeout * 0.5 <= blocked_for < sqlite_timeout * 4

    with Session(engine) as verification_db:
        delivery = verification_db.scalar(select(Delivery))
        configuration = verification_db.get(SystemConfiguration, "default")
        assert delivery.status == DeliveryStatus.delivered
        if release_before_timeout:
            assert configuration.account_creation_enabled is False
        else:
            assert configuration.account_creation_enabled is True
    engine.dispose()
