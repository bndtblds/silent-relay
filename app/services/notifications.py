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

class NotificationService:
    def __init__(self, settings: Settings, cipher: FieldCipher):
        self.settings, self.cipher = settings, cipher

    def change_pin(
        self, db: Session, person_id: str, current_pin: str, new_pin: str
    ) -> bool:
        record = db.get(TrustedPersonToken, person_id)
        person = db.get(TrustedPerson, person_id)
        if not record or not person or not verify_pin(record.pin_hash, current_pin):
            return False
        if verify_pin(record.pin_hash, new_pin):
            raise ValueError
        record.pin_hash = hash_pin(new_pin)
        record.failed_pin_attempts = 0
        record.locked_until = None
        SessionManager(self.settings).revoke_trusted_person_sessions(db, person_id)
        audit(db, "trusted_person_pin_changed", person.account_id)
        db.commit()
        return True

    def eligible_contacts(self, db: Session, person: TrustedPerson) -> list[ContactMethod]:
        active_partner_ids = select(Partner.id).where(
            Partner.account_id == person.account_id,
            Partner.is_active.is_(True),
            Partner.id.in_(select(PartnerCredential.partner_id).where(
                PartnerCredential.enrolled_at.is_not(None),
                PartnerCredential.password_hash.is_not(None),
            )),
        )
        return list(db.scalars(select(ContactMethod).where(
            ContactMethod.account_id == person.account_id,
            ContactMethod.is_verified.is_(True),
            ContactMethod.is_active.is_(True),
            or_(
                and_(
                    ContactMethod.owner_type == "account",
                    ContactMethod.owner_id == person.account_id,
                ),
                and_(
                    ContactMethod.owner_type == "partner",
                    ContactMethod.owner_id.in_(active_partner_ids),
                ),
            ),
        )))

    def resolve_access(
        self, db: Session, token: str
    ) -> tuple[TrustedPerson, TrustedPersonToken] | None:
        record = db.scalar(select(TrustedPersonToken).where(
            TrustedPersonToken.token_hash == keyed_hash(token, self.settings.token_hmac_key),
            TrustedPersonToken.revoked_at.is_(None),
        ))
        if not record:
            return None
        person = db.get(TrustedPerson, record.trusted_person_id)
        if not person or not person.is_active:
            return None
        account = db.get(Account, person.account_id)
        if not account or not account.allows_access:
            return None
        if person.owner_type == "account":
            if person.owner_id != account.id:
                return None
        elif person.owner_type == "partner":
            partner = db.scalar(select(Partner).where(
                Partner.id == person.owner_id,
                Partner.account_id == account.id,
                Partner.is_active.is_(True),
            ))
            if not partner:
                return None
        else:
            return None
        record.last_used_at = utc_now()
        return person, record

    def resolve_person(self, db: Session, token: str) -> TrustedPerson | None:
        access = self.resolve_access(db, token)
        if not access:
            return None
        person, _ = access
        return person

    @staticmethod
    def normalize_message(message: str, language: str = "de") -> str:
        value = unicodedata.normalize("NFC", message).strip()
        if not 10 <= len(value) <= 5000:
            raise ValueError(translate(language, "error.message_length"))
        return value

    def stage(self, db: Session, person: TrustedPerson, message: str) -> str:
        account = db.get(Account, person.account_id)
        if not account or not account.allows_access:
            raise LookupError
        value, raw = self.normalize_message(
            message, account.language_code
        ), generate_token()
        db.add(Submission(
            id_hash=keyed_hash(raw, self.settings.token_hmac_key),
            trusted_person_id=person.id,
            encrypted_message=self.cipher.encrypt(value),
            expires_at=utc_now() + timedelta(minutes=15),
        ))
        db.commit()
        return raw

    def accept(
        self, db: Session, submission_token: str, expected_person_id: str
    ) -> Notification:
        person = db.get(TrustedPerson, expected_person_id)
        account = db.get(Account, person.account_id) if person else None
        if not person or not account or not account.allows_access:
            raise LookupError
        submission_id = keyed_hash(submission_token, self.settings.token_hmac_key)
        now = utc_now()
        consumed = db.execute(
            update(Submission).where(
                Submission.id_hash == submission_id,
                Submission.trusted_person_id == expected_person_id,
                Submission.consumed_at.is_(None),
                Submission.expires_at > now,
            ).values(consumed_at=now)
        )
        if consumed.rowcount != 1:
            db.rollback()
            raise LookupError
        submission = db.get(Submission, submission_id)
        message = self.cipher.decrypt(submission.encrypted_message)
        config = system_configuration(db)
        release_at = now + timedelta(minutes=config.notification_delay_minutes)
        notification = Notification(
            account_id=account.id, trusted_person_id=person.id, status=NotificationStatus.queued,
            encrypted_message_payload=self.cipher.encrypt(message),
            release_at=release_at,
            expires_at=release_at + timedelta(days=config.message_retention_days),
            deduplication_key=submission.id_hash,
        )
        db.add(notification)
        db.flush()
        audit(db, "notification_accepted", account.id)
        db.commit()
        if release_at <= utc_now():
            DeliveryService._freeze_recipients(db, utc_now())
        return notification

    @staticmethod
    def pending_for_person(db: Session, person_id: str, now: datetime | None = None) -> list[Notification]:
        now = now or utc_now()
        return list(db.scalars(select(Notification).where(
            Notification.trusted_person_id == person_id,
            Notification.status == NotificationStatus.queued,
            Notification.cancelled_at.is_(None),
            Notification.release_at > now,
        ).order_by(Notification.release_at)))

    def cancel(self, db: Session, person_id: str, notification_id: str, now: datetime | None = None) -> bool:
        now = now or utc_now()
        delivery_started = select(Delivery.id).where(
            Delivery.notification_id == Notification.id,
            Delivery.status != DeliveryStatus.pending,
        ).exists()
        cancelled = db.execute(
            update(Notification).where(
                Notification.id == notification_id,
                Notification.trusted_person_id == person_id,
                Notification.status == NotificationStatus.queued,
                Notification.cancelled_at.is_(None),
                Notification.release_at > now,
                ~delivery_started,
            ).values(
                status=NotificationStatus.discarded,
                cancelled_at=now,
                encrypted_message_payload=None,
            )
        )
        if cancelled.rowcount != 1:
            db.rollback()
            return False
        for delivery in db.scalars(select(Delivery).where(
            Delivery.notification_id == notification_id,
            Delivery.status == DeliveryStatus.pending,
        )):
            delivery.status = DeliveryStatus.cancelled
            delivery.encrypted_error_detail = self.cipher.encrypt("notification_cancelled")
        notification = db.get(Notification, notification_id)
        audit(db, "notification_cancelled", notification.account_id)
        db.commit()
        return True


class DeliveryService:
    BACKOFF_MINUTES = (1, 5, 30, 120, 720, 1440)
    PROCESSING_LEASE = timedelta(minutes=2)

    def __init__(self, settings: Settings, cipher: FieldCipher, providers: dict[str, NotificationProvider]):
        self.settings, self.cipher, self.providers = settings, cipher, providers

    def process_due(self, db: Session, now: datetime | None = None) -> int:
        now = now or utc_now()
        self._freeze_recipients(db, now)
        released_notifications = select(Notification.id).where(
            Notification.release_at <= now,
            Notification.cancelled_at.is_(None),
        )
        delivery_ids = list(db.scalars(select(Delivery.id).where(
            Delivery.notification_id.in_(released_notifications),
            or_(
                and_(
                    Delivery.status.in_([DeliveryStatus.pending, DeliveryStatus.retry_scheduled]),
                    or_(Delivery.next_retry_at.is_(None), Delivery.next_retry_at <= now),
                ),
                and_(
                    Delivery.status == DeliveryStatus.processing,
                    or_(Delivery.processing_until.is_(None), Delivery.processing_until <= now),
                ),
            ),
        ).order_by(Delivery.created_at).limit(100)))
        db.rollback()
        processed = 0
        for delivery_id in delivery_ids:
            claimed = db.execute(
                update(Delivery)
                .where(
                    Delivery.id == delivery_id,
                    Delivery.notification_id.in_(released_notifications),
                    or_(
                        and_(
                            Delivery.status.in_([DeliveryStatus.pending, DeliveryStatus.retry_scheduled]),
                            or_(Delivery.next_retry_at.is_(None), Delivery.next_retry_at <= now),
                        ),
                        and_(
                            Delivery.status == DeliveryStatus.processing,
                            or_(Delivery.processing_until.is_(None), Delivery.processing_until <= now),
                        ),
                    ),
                )
                .values(
                    status=DeliveryStatus.processing,
                    processing_started_at=now,
                    processing_until=now + self.PROCESSING_LEASE,
                )
            )
            db.commit()
            if claimed.rowcount != 1:
                continue
            with db.begin():
                delivery = db.get(Delivery, delivery_id, with_for_update=True)
                if (
                    not delivery
                    or delivery.status != DeliveryStatus.processing
                    or delivery.processing_started_at != now
                ):
                    continue

                contact, notification, reason = self._authorized_delivery(
                    db, delivery, now
                )
                if reason:
                    delivery.status = DeliveryStatus.cancelled
                    delivery.next_retry_at = None
                    self._clear_lease(delivery)
                    delivery.encrypted_error_detail = self.cipher.encrypt(reason)
                    update_notification_status(db, delivery.notification_id)
                    processed += 1
                    continue

                provider = self.providers.get(delivery.provider)
                if not provider:
                    delivery.status = DeliveryStatus.permanent_failure
                    delivery.next_retry_at = None
                    self._clear_lease(delivery)
                    delivery.encrypted_error_detail = self.cipher.encrypt(
                        "provider_unavailable"
                    )
                else:
                    delivery.attempt_count += 1
                    delivery.last_attempt_at = now
                    # Establish the SQLite write lock before the external call.
                    db.flush()
                    account = db.get(Account, notification.account_id)
                    language = account.language_code
                    result = send_tracked_email(
                        db,
                        self.settings,
                        self.cipher,
                        provider,
                        self.cipher.decrypt(contact.encrypted_value),
                        translate(language, "email.notification_subject"),
                        email_body(
                            language,
                            "email.notification_body",
                        ),
                        contact_method_id=contact.id,
                        delivery_id=delivery.id,
                    )
                    if result.successful:
                        delivery.status, delivery.delivered_at = DeliveryStatus.delivered, now
                        delivery.next_retry_at = None
                        delivery.provider_message_id = result.message_id
                    elif result.permanent_failure or delivery.attempt_count >= self.settings.delivery_max_attempts:
                        delivery.status = DeliveryStatus.permanent_failure
                    else:
                        delivery.status = DeliveryStatus.retry_scheduled
                        delay = self.BACKOFF_MINUTES[min(delivery.attempt_count - 1, len(self.BACKOFF_MINUTES) - 1)]
                        delivery.next_retry_at = now + timedelta(minutes=delay)
                    self._clear_lease(delivery)
                    if result.error_class:
                        delivery.encrypted_error_detail = self.cipher.encrypt(result.error_class)
                processed += 1
                update_notification_status(db, delivery.notification_id)
        return processed

    @staticmethod
    def _clear_lease(delivery: Delivery) -> None:
        delivery.processing_started_at = None
        delivery.processing_until = None

    @staticmethod
    def _authorized_delivery(
        db: Session, delivery: Delivery, now: datetime
    ) -> tuple[ContactMethod | None, Notification | None, str | None]:
        notification = db.get(Notification, delivery.notification_id, with_for_update=True)
        if not notification:
            return None, None, "notification_missing"
        if notification.expires_at and notification.expires_at <= now:
            return None, notification, "notification_expired"
        if not notification.encrypted_message_payload:
            return None, notification, "notification_payload_missing"
        account = db.get(Account, notification.account_id, with_for_update=True)
        if not account:
            return None, notification, "account_missing"
        if account.is_admin_locked:
            return None, notification, "account_locked"
        if account.status not in {AccountStatus.active, AccountStatus.overdue}:
            return None, notification, "account_not_eligible"
        contact = db.get(ContactMethod, delivery.contact_method_id, with_for_update=True) if delivery.contact_method_id else None
        if not contact:
            return None, notification, "contact_missing"
        if contact.account_id != notification.account_id:
            return contact, notification, "contact_account_mismatch"
        if not contact.is_active:
            return contact, notification, "contact_inactive"
        if not contact.is_verified:
            return contact, notification, "contact_unverified"
        if contact.owner_type == "account":
            if contact.owner_id != account.id:
                return contact, notification, "contact_owner_invalid"
        elif contact.owner_type == "partner":
            partner = db.get(Partner, contact.owner_id, with_for_update=True)
            if not partner:
                return contact, notification, "partner_missing"
            if partner.account_id != account.id:
                return contact, notification, "partner_account_mismatch"
            if not partner.is_active:
                return contact, notification, "partner_inactive"
        else:
            return contact, notification, "contact_owner_invalid"
        if not db.scalar(select(NotificationRecipient.id).where(
            NotificationRecipient.notification_id == notification.id,
            NotificationRecipient.owner_type == contact.owner_type,
            NotificationRecipient.owner_id == contact.owner_id,
        )):
            return contact, notification, "recipient_not_fixed"
        return contact, notification, None

    @staticmethod
    def _freeze_recipients(db: Session, now: datetime) -> None:
        notification_ids = list(db.scalars(select(Notification.id).where(
            Notification.release_at <= now,
            Notification.cancelled_at.is_(None),
            Notification.encrypted_message_payload.is_not(None),
            Notification.expires_at > now,
            Notification.recipients_frozen_at.is_(None),
        )))
        for notification_id in notification_ids:
            claimed = db.execute(update(Notification).where(
                Notification.id == notification_id,
                Notification.release_at <= now,
                Notification.cancelled_at.is_(None),
                Notification.encrypted_message_payload.is_not(None),
                Notification.recipients_frozen_at.is_(None),
            ).values(status=NotificationStatus.queued, recipients_frozen_at=now))
            if claimed.rowcount != 1:
                db.rollback()
                continue
            db.flush()
            notification = db.get(Notification, notification_id)
            if not notification:
                continue
            contacts = list(db.scalars(select(ContactMethod).where(
                ContactMethod.account_id == notification.account_id,
                ContactMethod.is_verified.is_(True),
                ContactMethod.is_active.is_(True),
            )))
            partner_ids = {
                partner_id for partner_id in db.scalars(select(Partner.id).where(
                    Partner.account_id == notification.account_id,
                    Partner.is_active.is_(True),
                    Partner.id.in_(select(PartnerCredential.partner_id).where(
                        PartnerCredential.enrolled_at.is_not(None),
                        PartnerCredential.password_hash.is_not(None),
                    )),
                ))
            }
            recipients = {("account", notification.account_id)} | {
                ("partner", partner_id) for partner_id in partner_ids
            }
            for owner_type, owner_id in recipients:
                db.add(NotificationRecipient(
                    notification_id=notification.id,
                    owner_type=owner_type,
                    owner_id=owner_id,
                ))
            for contact in contacts:
                if (contact.owner_type, contact.owner_id) in recipients:
                    db.add(Delivery(
                        notification_id=notification.id,
                        contact_method_id=contact.id,
                        provider=contact.channel,
                    ))
            db.commit()
