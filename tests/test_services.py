from __future__ import annotations

from datetime import timedelta
import threading
import uuid

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import app.services as services_module
from app.database import Base
from app.i18n import email_body, translate
from app.models import (
    Account, AccountReview, AccountStatus, AuditLog, ContactMethod, ContactReview,
    ContactReviewToken, Delivery, DeliveryStatus, Notification,
    NotificationRecipient, NotificationStatus, Partner, PartnerCredential,
    ReviewReminder, ServerSession, Submission, SystemConfiguration,
    TrustedPersonToken,
)
from app.providers.base import DeliveryResult
from app.security.core import SessionManager, hash_password, hash_pin, keyed_hash, verify_pin
from app.services import (
    AccountService, AuthenticationService, DeliveryService, LifecycleService,
    ManagementService, NotificationService, audit,
)
from app.time import utc_now


_ManagementService = ManagementService


def test_audit_without_http_request_has_no_request_id(db):
    audit(db, "scheduler_event")
    db.commit()

    event = db.scalar(select(AuditLog).where(AuditLog.event_type == "scheduler_event"))

    assert event is not None
    assert event.request_id is None


class ManagementService(_ManagementService):
    """Create activated partners for legacy delivery tests."""

    def add_partner(self, db, account_id, name):
        partner = super().add_partner(db, account_id, name)
        credential = db.get(PartnerCredential, partner.id)
        credential.password_hash = hash_password("partner test password")
        credential.enrolled_at = utc_now()
        db.commit()
        return partner


class SuccessfulProvider:
    channel = "email"

    def __init__(self):
        self.recipients = []
        self.messages = []

    def send(self, recipient, subject, body, *, envelope_token=None):
        self.recipients.append(recipient)
        self.messages.append((recipient, subject, body))
        return DeliveryResult(True, message_id="test-id")


class TemporaryFailureProvider:
    channel = "email"

    def send(self, recipient, subject, body, *, envelope_token=None):
        return DeliveryResult(False, error_class="temporary_smtp_error")


class PermanentFailureProvider:
    channel = "email"

    def send(self, recipient, subject, body, *, envelope_token=None):
        return DeliveryResult(
            False,
            permanent_failure=True,
            error_class="recipient_rejected",
        )


class FailIfCalledProvider:
    channel = "email"

    def __init__(self):
        self.calls = 0

    def send(self, recipient, subject, body, *, envelope_token=None):
        self.calls += 1
        raise AssertionError("provider must not be called")


def active_account(db, settings, cipher):
    service = AccountService(settings, cipher)
    account, account_owner_token, setup_token = service.create(db)
    _, verification = service.setup(db, setup_token, "correct horse battery staple", "owner@example.org")
    assert service.verify_contact(db, verification)
    return account


def verify_token_for_contact(db, settings, account_service, token):
    assert account_service.verify_contact(db, token)


def test_account_creation_stores_no_clear_tokens(db, settings, cipher):
    account, account_owner_token, setup_token = AccountService(settings, cipher).create(db)
    credential = account.credential
    assert account_owner_token not in credential.account_owner_token_hash
    assert setup_token not in credential.setup_token_hash
    assert uuid.UUID(account.id).version == 7
    assert "account_owner_credentials" in db.get_bind().dialect.get_table_names(db.connection())
    assert "admin_credentials" not in db.get_bind().dialect.get_table_names(db.connection())


def test_account_setup_encrypts_owner_name(db, settings, cipher):
    service = AccountService(settings, cipher)
    account, _, setup_token = service.create(db)
    service.setup(
        db,
        setup_token,
        "correct horse battery staple",
        "owner@example.org",
        owner_name="Erika Beispiel",
    )
    db.refresh(account)
    assert b"Erika Beispiel" not in account.encrypted_owner_name
    assert cipher.decrypt(account.encrypted_owner_name) == "Erika Beispiel"


def owner_account_with_token(db, settings, cipher, email="owner@example.org"):
    accounts = AccountService(settings, cipher)
    account, owner_token, setup_token = accounts.create(db)
    _, verification = accounts.setup(
        db, setup_token, "correct horse battery staple", email
    )
    accounts.verify_contact(db, verification)
    return account, owner_token


def account_sessions(db, settings, account_id):
    manager = SessionManager(settings)
    first, _ = manager.create(db, "account_owner", account_id)
    second, _ = manager.create(db, "account_owner", account_id)
    public, _ = manager.create(db, "public", account_id)
    db.commit()
    return first, second, public


def test_password_change_revokes_only_affected_account_owner_sessions(
    db, settings, cipher
):
    account, owner_token = owner_account_with_token(db, settings, cipher)
    other, _ = owner_account_with_token(
        db, settings, cipher, "other-owner@example.org"
    )
    first, second, public = account_sessions(db, settings, account.id)
    other_session, _ = SessionManager(settings).create(
        db, "account_owner", other.id
    )
    db.commit()

    service = AuthenticationService(settings)
    assert service.change_password(
        db,
        account.id,
        "correct horse battery staple",
        "new correct horse battery staple",
    )

    manager = SessionManager(settings)
    assert manager.resolve(db, first, "account_owner") is None
    assert manager.resolve(db, second, "account_owner") is None
    assert manager.resolve(db, public, "public") is not None
    assert manager.resolve(db, other_session, "account_owner") is not None
    assert service.login(db, owner_token, "correct horse battery staple") is None
    assert service.login(db, owner_token, "new correct horse battery staple") == account


def test_owner_link_rotation_revokes_only_affected_account_owner_sessions(
    db, settings, cipher
):
    account, old_token = owner_account_with_token(db, settings, cipher)
    other, _ = owner_account_with_token(
        db, settings, cipher, "other-owner@example.org"
    )
    first, second, public = account_sessions(db, settings, account.id)
    other_session, _ = SessionManager(settings).create(
        db, "account_owner", other.id
    )
    db.commit()

    service = AuthenticationService(settings)
    new_token = service.rotate_account_owner_token(db, account.id)

    manager = SessionManager(settings)
    assert manager.resolve(db, first, "account_owner") is None
    assert manager.resolve(db, second, "account_owner") is None
    assert manager.resolve(db, public, "public") is not None
    assert manager.resolve(db, other_session, "account_owner") is not None
    assert service.login(db, old_token, "correct horse battery staple") is None
    assert service.login(db, new_token, "correct horse battery staple") == account


def test_review_days_are_sorted_and_deduplicated(db, settings, cipher):
    config = db.get(SystemConfiguration, "default")
    config.account_review_reminder_days = "30,-3,-3,0"
    account = active_account(db, settings, cipher)
    reminders = list(db.scalars(select(ReviewReminder)))
    assert sorted(r.relative_day for r in reminders) == [-3, 0, 30]
    assert account.status == AccountStatus.active


def test_recipient_selection_includes_assigned_partner(db, settings, cipher):
    account = active_account(db, settings, cipher)
    accounts = AccountService(settings, cipher)
    management = ManagementService(settings, cipher)
    origin = management.add_partner(db, account.id, "Origin")
    other = management.add_partner(db, account.id, "Other")
    token_origin_contact = management.add_contact(db, account.id, "partner", origin.id, "origin@example.org")
    token_other_contact = management.add_contact(db, account.id, "partner", other.id, "other@example.org")
    accounts.verify_contact(db, token_origin_contact)
    accounts.verify_contact(db, token_other_contact)
    _, trusted_token = management.add_trusted_person(db, account.id, "partner", origin.id, "Trusted")
    notifications = NotificationService(settings, cipher)
    person = notifications.resolve_person(db, trusted_token)
    submission = notifications.stage(db, person, "A sufficiently long confidential message.")
    notification = notifications.accept(db, submission, person.id)
    contacts = list(db.scalars(
        select(ContactMethod).join(Delivery, Delivery.contact_method_id == ContactMethod.id)
        .where(Delivery.notification_id == notification.id)
    ))
    values = {cipher.decrypt(contact.encrypted_value) for contact in contacts}
    assert values == {"owner@example.org", "origin@example.org", "other@example.org"}


def test_recipient_selection_includes_account_owner_for_owner_trusted_person(db, settings, cipher):
    account = active_account(db, settings, cipher)
    accounts = AccountService(settings, cipher)
    management = ManagementService(settings, cipher)
    partner = management.add_partner(db, account.id, "Partner")
    partner_contact = management.add_contact(
        db, account.id, "partner", partner.id, "partner@example.org"
    )
    accounts.verify_contact(db, partner_contact)
    _, trusted_token = management.add_trusted_person(
        db, account.id, "account", account.id, "Trusted"
    )
    notifications = NotificationService(settings, cipher)
    person = notifications.resolve_person(db, trusted_token)
    notification = notifications.accept(
        db, notifications.stage(db, person, "A sufficiently long confidential message."),
        person.id,
    )
    contacts = list(db.scalars(
        select(ContactMethod).join(Delivery, Delivery.contact_method_id == ContactMethod.id)
        .where(Delivery.notification_id == notification.id)
    ))
    values = {cipher.decrypt(contact.encrypted_value) for contact in contacts}
    assert values == {"owner@example.org", "partner@example.org"}


def test_account_owner_is_sufficient_recipient_for_own_trusted_person(db, settings, cipher):
    account = active_account(db, settings, cipher)
    management = ManagementService(settings, cipher)
    _, trusted_token = management.add_trusted_person(
        db, account.id, "account", account.id, "Trusted"
    )
    assert NotificationService(settings, cipher).resolve_person(db, trusted_token) is not None


def test_submission_is_one_time(db, settings, cipher):
    account = active_account(db, settings, cipher)
    management = ManagementService(settings, cipher)
    partner = management.add_partner(db, account.id, "Origin")
    _, trusted_token = management.add_trusted_person(db, account.id, "partner", partner.id, "")
    service = NotificationService(settings, cipher)
    person = service.resolve_person(db, trusted_token)
    submission = service.stage(db, person, "A sufficiently long message.")
    service.accept(db, submission, person.id)
    try:
        service.accept(db, submission, person.id)
    except LookupError:
        pass
    else:
        raise AssertionError("submission accepted twice")


def test_foreign_submission_is_not_consumed(db, settings, cipher):
    account = active_account(db, settings, cipher)
    management = ManagementService(settings, cipher)
    person_a, token_a = management.add_trusted_person(
        db, account.id, "account", account.id, "Trusted A"
    )
    person_b, _ = management.add_trusted_person(
        db, account.id, "account", account.id, "Trusted B"
    )
    service = NotificationService(settings, cipher)
    submission_token = service.stage(
        db, service.resolve_person(db, token_a), "A sufficiently long private message."
    )

    with pytest.raises(LookupError):
        service.accept(db, submission_token, person_b.id)

    submission = db.get(
        Submission, keyed_hash(submission_token, settings.token_hmac_key)
    )
    assert submission.trusted_person_id == person_a.id
    assert submission.consumed_at is None
    assert db.scalar(select(func.count()).select_from(Notification)) == 0
    assert db.scalar(select(func.count()).select_from(NotificationRecipient)) == 0
    assert db.scalar(select(func.count()).select_from(Delivery)) == 0


def test_parallel_submission_acceptance_succeeds_at_most_once(
    tmp_path, settings, cipher
):
    engine = create_engine(f"sqlite:///{(tmp_path / 'submission.db').as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as setup_db:
        setup_db.add(SystemConfiguration(id="default", notification_delay_minutes=60))
        setup_db.commit()
        account = active_account(setup_db, settings, cipher)
        person, token = ManagementService(settings, cipher).add_trusted_person(
            setup_db, account.id, "account", account.id, "Trusted"
        )
        service = NotificationService(settings, cipher)
        submission_token = service.stage(
            setup_db, service.resolve_person(setup_db, token),
            "A sufficiently long private message.",
        )
        person_id = person.id

    barrier = threading.Barrier(2)
    results = []

    def accept():
        with Session(engine, expire_on_commit=False) as worker:
            barrier.wait(timeout=5)
            try:
                NotificationService(settings, cipher).accept(
                    worker, submission_token, person_id
                )
            except LookupError:
                results.append("rejected")
            else:
                results.append("accepted")

    threads = [threading.Thread(target=accept) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) == ["accepted", "rejected"]
    with Session(engine) as check_db:
        assert check_db.scalar(select(func.count()).select_from(Notification)) == 1
        assert check_db.scalar(select(func.count()).select_from(Submission).where(
            Submission.consumed_at.is_not(None)
        )) == 1
    engine.dispose()


def test_notification_waits_for_fixed_release_time_and_can_be_cancelled(
    db, settings, cipher
):
    config = db.get(SystemConfiguration, "default")
    config.notification_delay_minutes = 60
    account = active_account(db, settings, cipher)
    management = ManagementService(settings, cipher)
    origin = management.add_partner(db, account.id, "Origin")
    _, token = management.add_trusted_person(
        db, account.id, "partner", origin.id, "Trusted"
    )
    service = NotificationService(settings, cipher)
    person = service.resolve_person(db, token)
    before = utc_now()
    notification = service.accept(
        db, service.stage(db, person, "A sufficiently long queued message."), person.id
    )
    original_release_at = notification.release_at
    assert before + timedelta(minutes=59) < original_release_at

    config.notification_delay_minutes = 0
    db.commit()
    provider = SuccessfulProvider()
    assert DeliveryService(settings, cipher, {"email": provider}).process_due(db) == 0
    assert provider.recipients == []
    assert service.pending_for_person(db, person.id) == [notification]
    assert notification.release_at == original_release_at

    assert service.cancel(db, person.id, notification.id)
    db.refresh(notification)
    assert notification.status == NotificationStatus.discarded
    assert notification.cancelled_at is not None
    assert notification.encrypted_message_payload is None
    assert db.scalar(select(Delivery.status).where(
        Delivery.notification_id == notification.id
    )) is None
    assert not service.cancel(db, person.id, notification.id)


def test_notification_is_delivered_after_release_time(db, settings, cipher):
    config = db.get(SystemConfiguration, "default")
    config.notification_delay_minutes = 60
    account = active_account(db, settings, cipher)
    management = ManagementService(settings, cipher)
    origin = management.add_partner(db, account.id, "Origin")
    _, token = management.add_trusted_person(
        db, account.id, "partner", origin.id, "Trusted"
    )
    service = NotificationService(settings, cipher)
    person = service.resolve_person(db, token)
    notification = service.accept(
        db,
        service.stage(db, person, "A sufficiently long queued message."),
        person.id,
    )
    provider = SuccessfulProvider()
    delivery = DeliveryService(settings, cipher, {"email": provider})
    assert delivery.process_due(db, notification.release_at - timedelta(seconds=1)) == 0
    assert not service.cancel(
        db, service.resolve_person(db, token).id, notification.id,
        now=notification.release_at,
    )
    assert delivery.process_due(db, notification.release_at) == 1
    assert provider.recipients == ["owner@example.org"]


def test_delivery_success_keeps_message_for_protected_inbox(db, settings, cipher):
    account = active_account(db, settings, cipher)
    management = ManagementService(settings, cipher)
    origin = management.add_partner(db, account.id, "Origin")
    _, token = management.add_trusted_person(db, account.id, "partner", origin.id, "")
    service = NotificationService(settings, cipher)
    person = service.resolve_person(db, token)
    submission = service.stage(db, person, "A sufficiently long message.")
    notification = service.accept(db, submission, person.id)
    provider = SuccessfulProvider()
    assert DeliveryService(settings, cipher, {"email": provider}).process_due(db) == 1
    db.refresh(notification)
    assert notification.status == NotificationStatus.delivered
    assert notification.encrypted_message_payload is not None
    assert provider.recipients == ["owner@example.org"]
    delivery = db.scalar(select(Delivery).where(Delivery.notification_id == notification.id))
    assert delivery.processing_started_at is None
    assert delivery.processing_until is None


def _pending_notification(
    db,
    settings,
    cipher,
    *,
    message="A sufficiently long confidential message.",
    owner_name=None,
    partner_name="Origin",
    trusted_name="",
):
    account = active_account(db, settings, cipher)
    if owner_name:
        account.encrypted_owner_name = cipher.encrypt(owner_name)
        db.commit()
    management = ManagementService(settings, cipher)
    origin = management.add_partner(db, account.id, partner_name)
    _, token = management.add_trusted_person(
        db, account.id, "partner", origin.id, trusted_name
    )
    service = NotificationService(settings, cipher)
    person = service.resolve_person(db, token)
    notification = service.accept(
        db,
        service.stage(
            db,
            person,
            message,
        ),
        person.id,
    )
    delivery = db.scalar(
        select(Delivery).where(Delivery.notification_id == notification.id)
    )
    contact = db.get(ContactMethod, delivery.contact_method_id)
    return account, notification, delivery, contact


def test_valid_delivery_lease_prevents_second_claim(db, settings, cipher):
    _, _, delivery, _ = _pending_notification(db, settings, cipher)
    now = utc_now()
    delivery.status = DeliveryStatus.processing
    delivery.processing_started_at = now
    delivery.processing_until = now + timedelta(minutes=1)
    db.commit()

    provider = SuccessfulProvider()
    assert DeliveryService(settings, cipher, {"email": provider}).process_due(db, now) == 0
    assert provider.recipients == []


def test_expired_delivery_lease_is_reclaimed_after_restart(db, settings, cipher):
    _, _, delivery, _ = _pending_notification(db, settings, cipher)
    now = utc_now()
    delivery.status = DeliveryStatus.processing
    delivery.processing_started_at = now - timedelta(minutes=3)
    delivery.processing_until = now - timedelta(minutes=1)
    db.commit()

    provider = SuccessfulProvider()
    with Session(db.get_bind(), expire_on_commit=False) as restarted_db:
        assert DeliveryService(settings, cipher, {"email": provider}).process_due(restarted_db, now) == 1
    db.refresh(delivery)
    assert delivery.status == DeliveryStatus.delivered
    assert delivery.processing_until is None


def test_crash_after_claim_before_provider_leaves_recoverable_lease(
    db, settings, cipher, monkeypatch
):
    _, _, delivery, _ = _pending_notification(db, settings, cipher)
    now = utc_now()
    service = DeliveryService(settings, cipher, {"email": FailIfCalledProvider()})

    monkeypatch.setattr(service, "_authorized_delivery", lambda *args: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        service.process_due(db, now)
    db.rollback()
    db.refresh(delivery)
    assert delivery.status == DeliveryStatus.processing
    assert delivery.processing_until == now + service.PROCESSING_LEASE

    monkeypatch.undo()
    provider = SuccessfulProvider()
    assert DeliveryService(settings, cipher, {"email": provider}).process_due(
        db, delivery.processing_until + timedelta(microseconds=1)
    ) == 1


def test_crash_after_provider_acceptance_can_duplicate_neutral_notice_on_recovery(
    db, settings, cipher, monkeypatch
):
    confidential_message = "CONFIDENTIAL-MESSAGE-7f3b8d must stay in the inbox"
    owner_name = "OWNER-NAME-91c7a2"
    partner_name = "PARTNER-RELATIONSHIP-42d5e8"
    trusted_name = "TRUSTED-PERSON-SECRET-68a1f4"
    account, _, delivery, _ = _pending_notification(
        db,
        settings,
        cipher,
        message=confidential_message,
        owner_name=owner_name,
        partner_name=partner_name,
        trusted_name=trusted_name,
    )
    now = utc_now()
    provider = SuccessfulProvider()
    service = DeliveryService(settings, cipher, {"email": provider})
    real_send_tracked_email = services_module.send_tracked_email
    real_authorized_delivery = DeliveryService._authorized_delivery
    authorization_checks = []

    def record_authorization(*args):
        authorization_checks.append(args[1].id)
        return real_authorized_delivery(*args)

    def crash_after_provider_success(*args, **kwargs):
        result = real_send_tracked_email(*args, **kwargs)
        assert result.successful
        raise SystemExit

    monkeypatch.setattr(
        DeliveryService, "_authorized_delivery", staticmethod(record_authorization)
    )
    monkeypatch.setattr(
        services_module, "send_tracked_email", crash_after_provider_success
    )
    with pytest.raises(SystemExit):
        service.process_due(db, now)
    db.rollback()
    db.refresh(delivery)
    assert len(provider.recipients) == 1
    assert delivery.status == DeliveryStatus.processing
    assert delivery.attempt_count == 0
    assert delivery.delivered_at is None
    assert delivery.provider_message_id is None
    assert delivery.processing_started_at == now
    assert delivery.processing_until == now + service.PROCESSING_LEASE

    monkeypatch.setattr(
        services_module, "send_tracked_email", real_send_tracked_email
    )
    assert service.process_due(
        db, delivery.processing_until - timedelta(microseconds=1)
    ) == 0
    assert len(provider.recipients) == 1

    assert service.process_due(
        db, delivery.processing_until + timedelta(microseconds=1)
    ) == 1
    db.refresh(delivery)
    assert len(provider.recipients) == 2
    assert authorization_checks == [delivery.id, delivery.id]
    assert delivery.status == DeliveryStatus.delivered
    assert delivery.attempt_count == 1
    assert delivery.delivered_at == now + service.PROCESSING_LEASE + timedelta(microseconds=1)
    assert delivery.provider_message_id == "test-id"
    assert delivery.processing_started_at is None
    assert delivery.processing_until is None

    expected_subject = translate(account.language_code, "email.notification_subject")
    expected_body = email_body(account.language_code, "email.notification_body")
    assert provider.messages == [
        ("owner@example.org", expected_subject, expected_body),
        ("owner@example.org", expected_subject, expected_body),
    ]
    forbidden_values = (
        confidential_message,
        owner_name,
        partner_name,
        trusted_name,
        "partner",
    )
    for _, subject, body in provider.messages:
        rendered_notice = f"{subject}\n{body}"
        assert all(value not in rendered_notice for value in forbidden_values)


@pytest.mark.parametrize("revocation", ["expired_message", "blocked_contact", "locked_account"])
def test_reclaimed_delivery_rechecks_current_authorization(
    db, settings, cipher, revocation
):
    account, notification, delivery, contact = _pending_notification(db, settings, cipher)
    now = utc_now()
    delivery.status = DeliveryStatus.processing
    delivery.processing_started_at = now - timedelta(minutes=3)
    delivery.processing_until = now - timedelta(minutes=1)
    if revocation == "expired_message":
        notification.expires_at = now - timedelta(seconds=1)
    elif revocation == "blocked_contact":
        contact.is_active = False
    else:
        account.is_admin_locked = True
    db.commit()

    provider = FailIfCalledProvider()
    assert DeliveryService(settings, cipher, {"email": provider}).process_due(db, now) == 1
    db.refresh(delivery)
    assert provider.calls == 0
    assert delivery.status == DeliveryStatus.cancelled
    assert delivery.processing_started_at is None
    assert delivery.processing_until is None


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("unverified", "contact_unverified"),
        ("inactive_contact", "contact_inactive"),
        ("disabled_account", "account_not_eligible"),
        ("locked_account", "account_locked"),
        ("other_account", "contact_account_mismatch"),
        ("invalid_owner", "contact_owner_invalid"),
        ("expired_notification", "notification_expired"),
        ("missing_payload", "notification_payload_missing"),
    ],
)
def test_delivery_rechecks_authorization_before_provider(
    db, settings, cipher, change, reason
):
    account, notification, delivery, contact = _pending_notification(
        db, settings, cipher
    )
    if change == "unverified":
        contact.is_verified = False
    elif change == "inactive_contact":
        contact.is_active = False
    elif change == "disabled_account":
        account.status = AccountStatus.disabled
    elif change == "locked_account":
        account.is_admin_locked = True
    elif change == "other_account":
        other = Account(status=AccountStatus.active)
        db.add(other)
        db.flush()
        contact.account_id = other.id
    elif change == "invalid_owner":
        contact.owner_id = str(uuid.uuid4())
    elif change == "expired_notification":
        notification.expires_at = utc_now() - timedelta(seconds=1)
    elif change == "missing_payload":
        notification.encrypted_message_payload = None
    db.commit()

    provider = FailIfCalledProvider()
    service = DeliveryService(settings, cipher, {"email": provider})
    assert service.process_due(db) == 1
    assert service.process_due(db) == 0

    db.refresh(delivery)
    db.refresh(notification)
    assert provider.calls == 0
    assert delivery.status == DeliveryStatus.cancelled
    assert delivery.attempt_count == 0
    assert delivery.next_retry_at is None
    assert cipher.decrypt(delivery.encrypted_error_detail) == reason
    assert notification.status == NotificationStatus.discarded
    if change == "missing_payload":
        assert notification.encrypted_message_payload is None
    else:
        assert notification.encrypted_message_payload is not None


def test_overdue_account_remains_authorized_for_delivery(db, settings, cipher):
    account, notification, delivery, _ = _pending_notification(db, settings, cipher)
    account.status = AccountStatus.overdue
    db.commit()

    provider = SuccessfulProvider()
    assert DeliveryService(settings, cipher, {"email": provider}).process_due(db) == 1
    db.refresh(delivery)
    db.refresh(notification)
    assert provider.recipients == ["owner@example.org"]
    assert delivery.status == DeliveryStatus.delivered
    assert notification.status == NotificationStatus.delivered


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("inactive", "partner_inactive"),
        ("deleted", "partner_missing"),
    ],
)
def test_partner_must_still_exist_and_be_active_before_delivery(
    db, settings, cipher, change, reason
):
    account = active_account(db, settings, cipher)
    accounts = AccountService(settings, cipher)
    management = ManagementService(settings, cipher)
    recipient_partner = management.add_partner(db, account.id, "Recipient")
    recipient_token = management.add_contact(
        db,
        account.id,
        "partner",
        recipient_partner.id,
        "partner@example.org",
    )
    accounts.verify_contact(db, recipient_token)
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
    owner_delivery = db.scalar(
        select(Delivery)
        .join(ContactMethod, Delivery.contact_method_id == ContactMethod.id)
        .where(
            Delivery.notification_id == notification.id,
            ContactMethod.owner_type == "account",
        )
    )
    owner_delivery.status = DeliveryStatus.cancelled
    partner_delivery = db.scalar(
        select(Delivery)
        .join(ContactMethod, Delivery.contact_method_id == ContactMethod.id)
        .where(
            Delivery.notification_id == notification.id,
            ContactMethod.owner_type == "partner",
        )
    )
    if change == "inactive":
        recipient_partner.is_active = False
    else:
        db.delete(recipient_partner)
    db.commit()

    provider = FailIfCalledProvider()
    assert DeliveryService(settings, cipher, {"email": provider}).process_due(db) == 1
    db.refresh(partner_delivery)
    db.refresh(notification)
    assert provider.calls == 0
    assert partner_delivery.status == DeliveryStatus.cancelled
    assert cipher.decrypt(partner_delivery.encrypted_error_detail) == reason
    assert notification.status == NotificationStatus.discarded


def test_notification_aggregate_is_consistent_when_authorization_is_partly_revoked(
    db, settings, cipher
):
    account = active_account(db, settings, cipher)
    accounts = AccountService(settings, cipher)
    management = ManagementService(settings, cipher)
    recipient_partner = management.add_partner(db, account.id, "Recipient")
    recipient_token = management.add_contact(
        db, account.id, "partner", recipient_partner.id, "partner@example.org"
    )
    accounts.verify_contact(db, recipient_token)
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
    partner_contact = db.scalar(select(ContactMethod).where(
        ContactMethod.owner_type == "partner",
        ContactMethod.owner_id == recipient_partner.id,
    ))
    partner_contact.is_active = False
    db.commit()

    provider = SuccessfulProvider()
    assert DeliveryService(settings, cipher, {"email": provider}).process_due(db) == 2
    db.refresh(notification)
    statuses = set(db.scalars(select(Delivery.status).where(
        Delivery.notification_id == notification.id
    )))
    assert provider.recipients == ["owner@example.org"]
    assert statuses == {DeliveryStatus.delivered, DeliveryStatus.cancelled}
    assert notification.status == NotificationStatus.partially_delivered
    assert notification.encrypted_message_payload is not None


def test_delivery_email_uses_account_language(db, settings, cipher):
    account = active_account(db, settings, cipher)
    account.language_code = "en"
    management = ManagementService(settings, cipher)
    origin = management.add_partner(db, account.id, "Origin")
    _, token = management.add_trusted_person(db, account.id, "partner", origin.id, "")
    service = NotificationService(settings, cipher)
    person = service.resolve_person(db, token)
    notification = service.accept(
        db, service.stage(db, person, "A sufficiently long message."), person.id
    )
    provider = SuccessfulProvider()
    DeliveryService(settings, cipher, {"email": provider}).process_due(db)
    assert provider.messages[0][1] == "New confidential message in SilentRelay"
    assert "A sufficiently long message." not in provider.messages[0][2]
    assert "personal SilentRelay access" in provider.messages[0][2]
    assert "Replies are not read and are deleted automatically" in (
        provider.messages[0][2]
    )
    db.refresh(notification)
    assert notification.encrypted_message_payload is not None


def test_temporary_delivery_failure_schedules_retry(db, settings, cipher):
    account = active_account(db, settings, cipher)
    management = ManagementService(settings, cipher)
    origin = management.add_partner(db, account.id, "Origin")
    _, token = management.add_trusted_person(db, account.id, "partner", origin.id, "")
    service = NotificationService(settings, cipher)
    person = service.resolve_person(db, token)
    notification = service.accept(
        db, service.stage(db, person, "A sufficiently long message."), person.id
    )
    DeliveryService(settings, cipher, {"email": TemporaryFailureProvider()}).process_due(db)
    delivery = db.scalar(select(Delivery).where(Delivery.notification_id == notification.id))
    assert delivery.status == DeliveryStatus.retry_scheduled
    assert delivery.next_retry_at is not None
    assert delivery.processing_started_at is None
    assert delivery.processing_until is None


def test_immediate_permanent_delivery_failure_invalidates_contact(
    db, settings, cipher
):
    account = active_account(db, settings, cipher)
    management = ManagementService(settings, cipher)
    origin = management.add_partner(db, account.id, "Origin")
    _, token = management.add_trusted_person(
        db, account.id, "partner", origin.id, ""
    )
    service = NotificationService(settings, cipher)
    person = service.resolve_person(db, token)
    notification = service.accept(
        db,
        service.stage(
            db,
            person,
            "A sufficiently long message.",
        ),
        person.id,
    )
    assert DeliveryService(
        settings, cipher, {"email": PermanentFailureProvider()}
    ).process_due(db) == 1

    delivery = db.scalar(
        select(Delivery).where(Delivery.notification_id == notification.id)
    )
    contact = db.get(ContactMethod, delivery.contact_method_id)
    db.refresh(notification)
    assert delivery.status == DeliveryStatus.permanent_failure
    assert delivery.attempt_count == 1
    assert delivery.next_retry_at is None
    assert delivery.processing_started_at is None
    assert delivery.processing_until is None
    assert cipher.decrypt(delivery.encrypted_error_detail) == "recipient_rejected"
    assert notification.status == NotificationStatus.failed
    assert not contact.is_verified
    assert contact.permanent_failure_count == 1
    assert contact.last_permanent_failure_at is not None


def test_lifecycle_transitions(db, settings, cipher):
    account = active_account(db, settings, cipher)
    now = utc_now()
    account.next_review_due_at = now - timedelta(days=1)
    account.review_grace_due_at = now + timedelta(days=1)
    db.commit()
    LifecycleService(settings).run(db, now)
    assert account.status == AccountStatus.overdue
    account.review_grace_due_at = now - timedelta(seconds=1)
    db.commit()
    LifecycleService(settings).run(db, now)
    assert account.status == AccountStatus.disabled


def test_periodic_contact_confirmation_and_owner_review_complete_cycle(
    db, settings, cipher
):
    account = active_account(db, settings, cipher)
    contact = db.scalar(select(ContactMethod).where(
        ContactMethod.account_id == account.id,
        ContactMethod.owner_type == "account",
    ))
    review = db.scalar(select(AccountReview).where(
        AccountReview.account_id == account.id,
        AccountReview.confirmed_at.is_(None),
    ))
    now = utc_now()
    review.review_due_at = now - timedelta(seconds=1)
    account.next_review_due_at = review.review_due_at
    token = "periodic-confirmation-token"
    contact_review = ContactReview(
        account_review_id=review.id,
        contact_method_id=contact.id,
        confirmation_due_at=now + timedelta(days=60),
    )
    db.add(contact_review)
    db.flush()
    db.add(ContactReviewToken(
        token_hash=keyed_hash(token, settings.token_hmac_key),
        contact_review_id=contact_review.id,
        expires_at=contact_review.confirmation_due_at,
    ))
    db.add(ContactReviewToken(
        token_hash=keyed_hash("older-unused-token", settings.token_hmac_key),
        contact_review_id=contact_review.id,
        expires_at=contact_review.confirmation_due_at,
    ))
    db.commit()

    service = AccountService(settings, cipher)
    assert not service.confirm_review(db, account)
    assert review.details_confirmed_at is not None
    assert review.confirmed_at is None

    assert service.verify_contact(db, token) == account
    assert review.confirmed_at is not None
    assert account.status == AccountStatus.active
    assert account.next_review_due_at > now
    assert db.scalar(select(func.count()).select_from(ContactReviewToken)) == 0


def test_expired_periodic_confirmation_excludes_contact(
    db, settings, cipher
):
    account = active_account(db, settings, cipher)
    contact = db.scalar(select(ContactMethod).where(
        ContactMethod.account_id == account.id,
        ContactMethod.owner_type == "account",
    ))
    review = db.scalar(select(AccountReview).where(
        AccountReview.account_id == account.id,
        AccountReview.confirmed_at.is_(None),
    ))
    now = utc_now()
    contact_review = ContactReview(
        account_review_id=review.id,
        contact_method_id=contact.id,
        confirmation_due_at=now - timedelta(seconds=1),
    )
    db.add(contact_review)
    db.flush()
    db.add(ContactReviewToken(
        token_hash=keyed_hash("expired-review-token", settings.token_hmac_key),
        contact_review_id=contact_review.id,
        expires_at=contact_review.confirmation_due_at,
    ))
    db.commit()

    LifecycleService(settings).run(db, now)

    assert not contact.is_verified
    assert contact.verified_at is None
    assert contact.last_review_expired_at == now


def test_expired_message_is_discarded(db, settings, cipher):
    account = active_account(db, settings, cipher)
    management = ManagementService(settings, cipher)
    origin = management.add_partner(db, account.id, "Origin")
    _, token = management.add_trusted_person(db, account.id, "partner", origin.id, "")
    service = NotificationService(settings, cipher)
    person = service.resolve_person(db, token)
    notification = service.accept(
        db, service.stage(db, person, "A sufficiently long message."), person.id
    )
    notification.expires_at = utc_now() - timedelta(seconds=1)
    db.commit()
    LifecycleService(settings).run(db)
    assert notification.status == NotificationStatus.discarded
    assert notification.encrypted_message_payload is None
    delivery = db.scalar(select(Delivery).where(Delivery.notification_id == notification.id))
    assert delivery.status == DeliveryStatus.cancelled
    assert cipher.decrypt(delivery.encrypted_error_detail) == "notification_expired"


def test_rotating_trusted_access_revokes_pin_and_sessions(db, settings, cipher):
    account, _, setup = AccountService(settings, cipher).create(db)
    AccountService(settings, cipher).setup(
        db, setup, "correct horse battery staple", "owner@example.org"
    )
    person, old_token = ManagementService(settings, cipher).add_trusted_person(
        db, account.id, "account", account.id, "Trusted"
    )
    record = db.get(TrustedPersonToken, person.id)
    record.pin_hash = hash_pin("472915")
    record.enrolled_at = utc_now()
    raw_session, _ = SessionManager(settings).create(
        db, "trusted_person", account.id, person.id
    )
    db.commit()

    new_token = ManagementService(settings, cipher).rotate_trusted_token(
        db, account.id, person.id
    )

    db.refresh(record)
    assert new_token != old_token
    assert record.pin_hash is None
    assert record.enrolled_at is None
    assert record.enrollment_expires_at > utc_now() + timedelta(days=13)
    assert SessionManager(settings).resolve(
        db, raw_session, "trusted_person"
    ) is None
    assert db.scalar(select(ServerSession).where(
        ServerSession.trusted_person_id == person.id
    )) is None


def test_changing_trusted_pin_requires_current_pin_and_revokes_sessions(
    db, settings, cipher
):
    account, _, setup = AccountService(settings, cipher).create(db)
    AccountService(settings, cipher).setup(
        db, setup, "correct horse battery staple", "owner@example.org"
    )
    person, _ = ManagementService(settings, cipher).add_trusted_person(
        db, account.id, "account", account.id, "Trusted"
    )
    record = db.get(TrustedPersonToken, person.id)
    record.pin_hash = hash_pin("472915")
    record.enrolled_at = utc_now()
    raw_session, _ = SessionManager(settings).create(
        db, "trusted_person", account.id, person.id
    )
    db.commit()

    service = NotificationService(settings, cipher)
    assert not service.change_pin(db, person.id, "472916", "583026")
    assert service.change_pin(db, person.id, "472915", "583026")
    assert verify_pin(record.pin_hash, "583026")
    assert SessionManager(settings).resolve(db, raw_session, "trusted_person") is None
