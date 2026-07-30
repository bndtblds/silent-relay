from __future__ import annotations

from datetime import datetime, timedelta
import uuid

from sqlalchemy import select

from app.models import (
    Account, AccountStatus, ContactMethod, Delivery, DeliveryStatus, Notification,
    NotificationStatus, Partner, ReviewReminder,
)
from app.providers.base import DeliveryResult
from app.services import (
    AccountService, DeliveryService, LifecycleService, ManagementService, NotificationService,
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
    assert "A trusted person submitted" in provider.messages[0][2]
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
    assert delivery.status == DeliveryStatus.permanent_failure
