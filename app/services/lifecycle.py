from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.email_address import normalize_email_address
from app.email_tracking import send_tracked_email, update_notification_status
from app.i18n import email_body, normalize_language, translate
from app.models import (
    Account, AccountOwnerCredential, AccountReview, AccountStatus, AuditLog, ContactMethod,
    ContactReview, ContactReviewToken, Delivery, DeliveryStatus, Notification,
    NotificationRecipient, NotificationStatus, Partner, PartnerCredential, ReviewReminder,
    ServerSession, Submission, TrustedPerson, TrustedPersonToken,
)
from app.providers.base import NotificationProvider
from app.request_context import current_request_id
from app.security.core import (
    FieldCipher, SessionManager, fingerprint, generate_token, hash_password,
    hash_pin, keyed_hash, verify_password, verify_pin,
)
from app.system_config import review_reminder_days, system_configuration
from app.time import utc_now

from app.services._audit import audit
from app.services.inbox import InboxService

class LifecycleService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.cipher = FieldCipher(settings.field_encryption_key)

    def disable_administratively(
        self, db: Session, account: Account, now: datetime | None = None
    ) -> None:
        now = now or utc_now()
        account.is_admin_locked = True
        if account.status != AccountStatus.disabled:
            account.status = AccountStatus.disabled
        if account.disabled_at is None:
            account.disabled_at = now
        if account.deletion_due_at is None:
            retention_days = system_configuration(
                db
            ).account_retention_after_disable_days
            account.deletion_due_at = account.disabled_at + timedelta(
                days=retention_days
            )
        SessionManager(self.settings).revoke_account_sessions(db, account.id)
        audit(db, "account_administratively_disabled", account.id)

    def run(self, db: Session, now: datetime | None = None) -> None:
        now = now or utc_now()
        db.execute(delete(ContactReviewToken).where(
            ContactReviewToken.expires_at <= now
        ))
        expired_contact_reviews = list(db.scalars(select(ContactReview).where(
            ContactReview.confirmed_at.is_(None),
            ContactReview.confirmation_due_at <= now,
        )))
        for contact_review in expired_contact_reviews:
            contact = db.get(ContactMethod, contact_review.contact_method_id)
            if contact and contact.is_active:
                contact.is_verified = False
                contact.verified_at = None
                contact.last_review_expired_at = now
                account = db.get(Account, contact.account_id)
                if account:
                    account.last_contact_problem_reminder_at = None
            db.execute(delete(ContactReviewToken).where(
                ContactReviewToken.contact_review_id == contact_review.id
            ))
        for account in db.scalars(select(Account).where(Account.status == AccountStatus.active, Account.next_review_due_at <= now)):
            account.status = AccountStatus.overdue
        for account in db.scalars(select(Account).where(Account.status == AccountStatus.overdue, Account.review_grace_due_at <= now)):
            account.status, account.disabled_at = AccountStatus.disabled, now
            config = system_configuration(db)
            account.deletion_due_at = now + timedelta(days=config.account_retention_after_disable_days)
        incomplete_disabled_accounts = list(db.scalars(select(Account).where(
            Account.status == AccountStatus.disabled,
            Account.deletion_due_at.is_(None),
        )))
        if incomplete_disabled_accounts:
            retention_days = system_configuration(
                db
            ).account_retention_after_disable_days
            for account in incomplete_disabled_accounts:
                if account.disabled_at is None:
                    account.disabled_at = now
                account.deletion_due_at = account.disabled_at + timedelta(
                    days=retention_days
                )
                audit(db, "disabled_account_retention_initialized", account.id)
        expired = list(db.scalars(select(Account).where(
            Account.status == AccountStatus.pending_verification,
            Account.created_at <= now - timedelta(days=system_configuration(db).account_pending_retention_days),
        )))
        for account in expired:
            db.delete(account)
        for account in list(db.scalars(select(Account).where(
            Account.status == AccountStatus.scheduled_for_deletion, Account.deletion_due_at <= now
        ))):
            db.delete(account)
        for account in db.scalars(select(Account).where(
            Account.status == AccountStatus.disabled,
            Account.deletion_due_at <= now,
        )):
            account.status = AccountStatus.scheduled_for_deletion
        db.execute(delete(Submission).where(Submission.expires_at <= now))
        for notification_id in db.scalars(select(Notification.id).where(
            Notification.encrypted_message_payload.is_not(None),
            Notification.release_at <= now,
        )):
            InboxService.erase_if_complete(db, notification_id, now)
        expired_notifications = list(db.scalars(select(Notification).where(
            Notification.encrypted_message_payload.is_not(None),
            Notification.expires_at.is_not(None),
            Notification.expires_at <= now,
        )))
        for notification in expired_notifications:
            notification.encrypted_message_payload = None
            notification.status = NotificationStatus.discarded
            for delivery in db.scalars(select(Delivery).where(
                Delivery.notification_id == notification.id,
                Delivery.status.in_([DeliveryStatus.pending, DeliveryStatus.processing, DeliveryStatus.retry_scheduled]),
            )):
                delivery.status = DeliveryStatus.cancelled
                delivery.next_retry_at = None
                delivery.encrypted_error_detail = self.cipher.encrypt(
                    "notification_expired"
                )
        db.execute(delete(AuditLog).where(AuditLog.created_at <= now - timedelta(days=system_configuration(db).audit_retention_days)))
        db.commit()
