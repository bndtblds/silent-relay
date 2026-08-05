from __future__ import annotations

from datetime import datetime, timedelta
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Account, AccountReview, AccountStatus, ContactMethod, ContactReview,
    ContactReviewToken, Delivery, DeliveryStatus, Notification,
    NotificationStatus, Partner, ReviewReminder, ServerSession,
    TrustedPersonToken,
)
from app.providers.base import DeliveryResult
from app.security.core import SessionManager, hash_pin, keyed_hash
from app.services import (
    AccountService, AuthenticationService, DeliveryService, LifecycleService,
    ManagementService, NotificationService,
)


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
    settings.account_review_reminder_days = [30, -3, -3, 0]
    account = active_account(db, settings, cipher)
    reminders = list(db.scalars(select(ReviewReminder)))
    assert sorted(r.relative_day for r in reminders) == [-3, 0, 30]
    assert account.status == AccountStatus.active


def test_recipient_selection_excludes_origin_partner(db, settings, cipher):
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
    notification = notifications.accept(db, submission)
    contacts = list(db.scalars(
        select(ContactMethod).join(Delivery, Delivery.contact_method_id == ContactMethod.id)
        .where(Delivery.notification_id == notification.id)
    ))
    values = {cipher.decrypt(contact.encrypted_value) for contact in contacts}
    assert values == {"owner@example.org", "other@example.org"}


def test_recipient_selection_excludes_account_owner_for_owner_trusted_person(db, settings, cipher):
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
        db, notifications.stage(db, person, "A sufficiently long confidential message.")
    )
    contacts = list(db.scalars(
        select(ContactMethod).join(Delivery, Delivery.contact_method_id == ContactMethod.id)
        .where(Delivery.notification_id == notification.id)
    ))
    values = {cipher.decrypt(contact.encrypted_value) for contact in contacts}
    assert values == {"partner@example.org"}


def test_trusted_person_is_unavailable_without_another_recipient(db, settings, cipher):
    account = active_account(db, settings, cipher)
    management = ManagementService(settings, cipher)
    _, trusted_token = management.add_trusted_person(
        db, account.id, "account", account.id, "Trusted"
    )
    assert NotificationService(settings, cipher).resolve_person(db, trusted_token) is None


def test_submission_is_one_time(db, settings, cipher):
    account = active_account(db, settings, cipher)
    management = ManagementService(settings, cipher)
    partner = management.add_partner(db, account.id, "Origin")
    _, trusted_token = management.add_trusted_person(db, account.id, "partner", partner.id, "")
    service = NotificationService(settings, cipher)
    submission = service.stage(db, service.resolve_person(db, trusted_token), "A sufficiently long message.")
    service.accept(db, submission)
    try:
        service.accept(db, submission)
    except LookupError:
        pass
    else:
        raise AssertionError("submission accepted twice")


def test_delivery_success_removes_message(db, settings, cipher):
    account = active_account(db, settings, cipher)
    management = ManagementService(settings, cipher)
    origin = management.add_partner(db, account.id, "Origin")
    _, token = management.add_trusted_person(db, account.id, "partner", origin.id, "")
    service = NotificationService(settings, cipher)
    submission = service.stage(db, service.resolve_person(db, token), "A sufficiently long message.")
    notification = service.accept(db, submission)
    provider = SuccessfulProvider()
    assert DeliveryService(settings, cipher, {"email": provider}).process_due(db) == 1
    db.refresh(notification)
    assert notification.status == NotificationStatus.delivered
    assert notification.encrypted_message_payload is None
    assert provider.recipients == ["owner@example.org"]
    delivery = db.scalar(select(Delivery).where(Delivery.notification_id == notification.id))
    assert delivery.processing_started_at is None
    assert delivery.processing_until is None


def _pending_notification(db, settings, cipher):
    account = active_account(db, settings, cipher)
    management = ManagementService(settings, cipher)
    origin = management.add_partner(db, account.id, "Origin")
    _, token = management.add_trusted_person(
        db, account.id, "partner", origin.id, ""
    )
    service = NotificationService(settings, cipher)
    notification = service.accept(
        db,
        service.stage(
            db,
            service.resolve_person(db, token),
            "A sufficiently long confidential message.",
        ),
    )
    delivery = db.scalar(
        select(Delivery).where(Delivery.notification_id == notification.id)
    )
    contact = db.get(ContactMethod, delivery.contact_method_id)
    return account, notification, delivery, contact


def test_valid_delivery_lease_prevents_second_claim(db, settings, cipher):
    _, _, delivery, _ = _pending_notification(db, settings, cipher)
    now = datetime.utcnow()
    delivery.status = DeliveryStatus.processing
    delivery.processing_started_at = now
    delivery.processing_until = now + timedelta(minutes=1)
    db.commit()

    provider = SuccessfulProvider()
    assert DeliveryService(settings, cipher, {"email": provider}).process_due(db, now) == 0
    assert provider.recipients == []


def test_expired_delivery_lease_is_reclaimed_after_restart(db, settings, cipher):
    _, _, delivery, _ = _pending_notification(db, settings, cipher)
    now = datetime.utcnow()
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
    now = datetime.utcnow()
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


def test_crash_after_provider_acceptance_can_duplicate_on_recovery(
    db, settings, cipher
):
    _, _, delivery, _ = _pending_notification(db, settings, cipher)
    now = datetime.utcnow()

    class AcceptedThenCrashedProvider(SuccessfulProvider):
        def send(self, *args, **kwargs):
            super().send(*args, **kwargs)
            raise SystemExit

    first_provider = AcceptedThenCrashedProvider()
    with pytest.raises(SystemExit):
        DeliveryService(settings, cipher, {"email": first_provider}).process_due(db, now)
    db.rollback()
    db.refresh(delivery)
    assert len(first_provider.recipients) == 1
    assert delivery.status == DeliveryStatus.processing

    second_provider = SuccessfulProvider()
    assert DeliveryService(settings, cipher, {"email": second_provider}).process_due(
        db, delivery.processing_until + timedelta(microseconds=1)
    ) == 1
    assert len(second_provider.recipients) == 1


@pytest.mark.parametrize("revocation", ["expired_message", "blocked_contact", "locked_account"])
def test_reclaimed_delivery_rechecks_current_authorization(
    db, settings, cipher, revocation
):
    account, notification, delivery, contact = _pending_notification(db, settings, cipher)
    now = datetime.utcnow()
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
        notification.expires_at = datetime.utcnow() - timedelta(seconds=1)
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
    assert notification.encrypted_message_payload is None


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
    notification = notifications.accept(
        db,
        notifications.stage(
            db,
            notifications.resolve_person(db, trusted_token),
            "A sufficiently long confidential message.",
        ),
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
    notification = notifications.accept(
        db,
        notifications.stage(
            db,
            notifications.resolve_person(db, trusted_token),
            "A sufficiently long confidential message.",
        ),
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
    assert notification.encrypted_message_payload is None


def test_delivery_email_uses_account_language(db, settings, cipher):
    account = active_account(db, settings, cipher)
    account.language_code = "en"
    management = ManagementService(settings, cipher)
    origin = management.add_partner(db, account.id, "Origin")
    _, token = management.add_trusted_person(db, account.id, "partner", origin.id, "")
    service = NotificationService(settings, cipher)
    notification = service.accept(
        db, service.stage(db, service.resolve_person(db, token), "A sufficiently long message.")
    )
    provider = SuccessfulProvider()
    DeliveryService(settings, cipher, {"email": provider}).process_due(db)
    assert provider.messages[0][1] == "Confidential notification"
    assert "A trusted contact sent" in provider.messages[0][2]
    assert "Replies are not read and are deleted automatically" in (
        provider.messages[0][2]
    )
    db.refresh(notification)
    assert notification.encrypted_message_payload is None


def test_temporary_delivery_failure_schedules_retry(db, settings, cipher):
    account = active_account(db, settings, cipher)
    management = ManagementService(settings, cipher)
    origin = management.add_partner(db, account.id, "Origin")
    _, token = management.add_trusted_person(db, account.id, "partner", origin.id, "")
    service = NotificationService(settings, cipher)
    notification = service.accept(db, service.stage(db, service.resolve_person(db, token), "A sufficiently long message."))
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
    notification = service.accept(
        db,
        service.stage(
            db,
            service.resolve_person(db, token),
            "A sufficiently long message.",
        ),
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
    now = datetime.utcnow()
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
    now = datetime.utcnow()
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
    now = datetime.utcnow()
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
    notification = service.accept(
        db, service.stage(db, service.resolve_person(db, token), "A sufficiently long message.")
    )
    notification.expires_at = datetime.utcnow() - timedelta(seconds=1)
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
    record.enrolled_at = datetime.utcnow()
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
    assert record.enrollment_expires_at > datetime.utcnow() + timedelta(days=13)
    assert SessionManager(settings).resolve(
        db, raw_session, "trusted_person"
    ) is None
    assert db.scalar(select(ServerSession).where(
        ServerSession.trusted_person_id == person.id
    )) is None
