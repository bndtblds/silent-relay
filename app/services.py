from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.email_tracking import send_tracked_email
from app.i18n import email_body, normalize_language, translate
from app.models import (
    Account, AccountOwnerCredential, AccountReview, AccountStatus, AuditLog, ContactMethod,
    ContactReview, ContactReviewToken, Delivery, DeliveryStatus, Notification,
    NotificationStatus, Partner, ReviewReminder, Submission, TrustedPerson,
    TrustedPersonToken,
)
from app.providers.base import NotificationProvider
from app.security.core import FieldCipher, fingerprint, generate_token, hash_password, keyed_hash, verify_password


def audit(db: Session, event: str, account_id: str | None = None, **metadata: object) -> None:
    allowed = {key: value for key, value in metadata.items() if key in {"provider", "error_class", "count"}}
    db.add(AuditLog(account_id=account_id, event_type=event, technical_metadata=json.dumps(allowed)))


class AccountService:
    def __init__(self, settings: Settings, cipher: FieldCipher):
        self.settings, self.cipher = settings, cipher

    def create(self, db: Session, language_code: str = "de") -> tuple[Account, str, str]:
        if not self.settings.account_creation_enabled:
            raise PermissionError(translate(language_code, "home.closed"))
        account, account_owner_token, setup_token = (
            Account(language_code=normalize_language(language_code, self.settings.default_language)),
            generate_token(),
            generate_token(),
        )
        db.add(account)
        db.flush()
        db.add(AccountOwnerCredential(
            account_id=account.id,
            account_owner_token_hash=keyed_hash(account_owner_token, self.settings.token_hmac_key),
            setup_token_hash=keyed_hash(setup_token, self.settings.token_hmac_key),
            setup_expires_at=datetime.utcnow() + timedelta(hours=24),
        ))
        audit(db, "account_created", account.id)
        db.commit()
        return account, account_owner_token, setup_token

    def setup(self, db: Session, setup_token: str, password: str, email: str) -> tuple[Account, str]:
        token_hash = keyed_hash(setup_token, self.settings.token_hmac_key)
        credential = db.scalar(select(AccountOwnerCredential).where(AccountOwnerCredential.setup_token_hash == token_hash))
        if not credential or not credential.setup_expires_at or credential.setup_expires_at <= datetime.utcnow():
            raise LookupError(translate(self.settings.default_language, "error.setup_link"))
        language = credential.account.language_code
        normalized_email = email.strip().casefold()
        if "@" not in normalized_email or len(normalized_email) > 320:
            raise ValueError(translate(language, "error.email"))
        try:
            credential.password_hash = hash_password(password)
        except ValueError as exc:
            raise ValueError(translate(language, "error.password_length")) from exc
        credential.password_changed_at = datetime.utcnow()
        credential.setup_token_hash = None
        credential.setup_expires_at = None
        verification_token = generate_token()
        db.add(ContactMethod(
            account_id=credential.account_id, owner_type="account", owner_id=credential.account_id,
            encrypted_value=self.cipher.encrypt(normalized_email),
            value_fingerprint=fingerprint(normalized_email, self.settings.fingerprint_hmac_key),
            verification_token_hash=keyed_hash(verification_token, self.settings.token_hmac_key),
            verification_expires_at=datetime.utcnow() + timedelta(hours=24),
        ))
        audit(db, "account_setup", credential.account_id)
        db.commit()
        return credential.account, verification_token

    def contact_confirmation_account(self, db: Session, token: str) -> Account | None:
        now = datetime.utcnow()
        token_hash = keyed_hash(token, self.settings.token_hmac_key)
        contact = db.scalar(select(ContactMethod).where(
            ContactMethod.verification_token_hash == token_hash,
            ContactMethod.verification_expires_at > now,
        ))
        if contact:
            return db.get(Account, contact.account_id)
        review_token = db.get(ContactReviewToken, token_hash)
        if not review_token or review_token.expires_at <= now:
            return None
        contact_review = db.get(ContactReview, review_token.contact_review_id)
        if not contact_review or contact_review.confirmed_at:
            return None
        contact = db.get(ContactMethod, contact_review.contact_method_id)
        if not contact or not contact.is_active:
            return None
        return db.get(Account, contact.account_id)

    def verify_contact(self, db: Session, token: str) -> Account | None:
        now = datetime.utcnow()
        token_hash = keyed_hash(token, self.settings.token_hmac_key)
        contact_id = db.execute(update(ContactMethod).where(
            ContactMethod.verification_token_hash == token_hash,
            ContactMethod.verification_expires_at > now,
        ).values(
            verification_token_hash=None,
            verification_expires_at=None,
        ).returning(ContactMethod.id)).scalar_one_or_none()
        if contact_id is None:
            review_token = db.get(ContactReviewToken, token_hash)
            if not review_token or review_token.expires_at <= now:
                return None
            contact_review = db.get(ContactReview, review_token.contact_review_id)
            if not contact_review or contact_review.confirmed_at:
                return None
            contact = db.get(ContactMethod, contact_review.contact_method_id)
            if not contact or not contact.is_active:
                return None
            consumed_review_id = db.execute(delete(ContactReviewToken).where(
                ContactReviewToken.token_hash == token_hash,
                ContactReviewToken.expires_at > now,
            ).returning(ContactReviewToken.contact_review_id)).scalar_one_or_none()
            if consumed_review_id != contact_review.id:
                return None
            contact_review.confirmed_at = now
            db.execute(delete(ContactReviewToken).where(
                ContactReviewToken.contact_review_id == contact_review.id
            ))
            account = db.get(Account, contact.account_id)
            contact.is_verified, contact.verified_at = True, now
            contact.permanent_failure_count = 0
            contact.last_permanent_failure_at = None
            contact.last_review_expired_at = None
            if account:
                self._finish_review_if_complete(db, account, contact_review.account_review_id, now)
            audit(db, "periodic_contact_confirmed", contact.account_id)
            db.commit()
            return account

        contact = db.get(ContactMethod, contact_id)
        if not contact:
            db.rollback()
            return None
        contact.is_verified, contact.verified_at = True, now
        contact.permanent_failure_count = 0
        contact.last_permanent_failure_at = None
        contact.last_review_expired_at = None
        account = db.get(Account, contact.account_id)
        current_contact_review = db.scalar(select(ContactReview).where(
            ContactReview.contact_method_id == contact.id,
            ContactReview.confirmed_at.is_(None),
        ).order_by(ContactReview.created_at.desc()))
        if current_contact_review:
            current_contact_review.confirmed_at = now
            db.execute(delete(ContactReviewToken).where(
                ContactReviewToken.contact_review_id == current_contact_review.id
            ))
        if account and account.status == AccountStatus.pending_verification:
            account.status, account.activated_at, account.last_reviewed_at = AccountStatus.active, now, now
            self._create_review(db, account, now + timedelta(days=self.settings.account_review_interval_days))
        elif account and current_contact_review:
            self._finish_review_if_complete(db, account, current_contact_review.account_review_id, now)
        audit(db, "contact_verified", contact.account_id)
        db.commit()
        return account

    def _create_review(self, db: Session, account: Account, due: datetime) -> AccountReview:
        review = AccountReview(account_id=account.id, review_due_at=due)
        db.add(review)
        db.flush()
        for day in sorted(set(self.settings.account_review_reminder_days)):
            db.add(ReviewReminder(account_review_id=review.id, relative_day=day, scheduled_at=due + timedelta(days=day)))
        account.next_review_due_at = due
        account.review_grace_due_at = due + timedelta(days=self.settings.account_review_grace_days)
        return review

    def confirm_review(self, db: Session, account: Account) -> bool:
        now = datetime.utcnow()
        current = db.scalar(select(AccountReview).where(
            AccountReview.account_id == account.id, AccountReview.confirmed_at.is_(None)
        ).order_by(AccountReview.review_due_at.desc()))
        if not current or current.review_due_at > now:
            return False
        current.details_confirmed_at = now
        completed = self._finish_review_if_complete(db, account, current.id, now)
        audit(db, "review_details_confirmed", account.id)
        db.commit()
        return completed

    def finish_current_review_if_complete(
        self, db: Session, account: Account
    ) -> bool:
        current = db.scalar(select(AccountReview).where(
            AccountReview.account_id == account.id,
            AccountReview.confirmed_at.is_(None),
        ).order_by(AccountReview.review_due_at.desc()))
        if not current:
            return False
        completed = self._finish_review_if_complete(
            db, account, current.id, datetime.utcnow()
        )
        db.commit()
        return completed

    def _finish_review_if_complete(
        self, db: Session, account: Account, review_id: str, now: datetime
    ) -> bool:
        review = db.get(AccountReview, review_id)
        if not review or review.confirmed_at or not review.details_confirmed_at:
            return False
        pending = db.scalar(select(func.count()).select_from(ContactReview).where(
            ContactReview.account_review_id == review.id,
            ContactReview.confirmed_at.is_(None),
        ))
        unresolved = db.scalar(select(func.count()).select_from(ContactMethod).where(
            ContactMethod.account_id == account.id,
            ContactMethod.is_active.is_(True),
            ContactMethod.is_verified.is_(False),
        ))
        if pending or unresolved:
            return False
        review.confirmed_at = now
        account.status, account.disabled_at, account.deletion_due_at = (
            AccountStatus.active, None, None
        )
        account.last_reviewed_at = now
        account.last_contact_problem_reminder_at = None
        self._create_review(
            db, account, now + timedelta(days=self.settings.account_review_interval_days)
        )
        audit(db, "review_confirmed", account.id)
        return True


class AuthenticationService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def credential_for_token(self, db: Session, token: str) -> AccountOwnerCredential | None:
        return db.scalar(select(AccountOwnerCredential).where(
            AccountOwnerCredential.account_owner_token_hash == keyed_hash(token, self.settings.token_hmac_key)
        ))

    def login(self, db: Session, token: str, password: str) -> Account | None:
        credential = self.credential_for_token(db, token)
        now = datetime.utcnow()
        if not credential or (credential.locked_until and credential.locked_until > now):
            return None
        if not verify_password(credential.password_hash, password):
            credential.failed_login_count += 1
            if credential.failed_login_count >= 5:
                credential.locked_until = now + timedelta(minutes=min(30, credential.failed_login_count))
            db.commit()
            return None
        credential.failed_login_count, credential.locked_until = 0, None
        db.commit()
        return credential.account

    def rotate_account_owner_token(self, db: Session, account_id: str) -> str:
        credential = db.get(AccountOwnerCredential, account_id)
        if not credential:
            raise LookupError
        token = generate_token()
        credential.account_owner_token_hash = keyed_hash(token, self.settings.token_hmac_key)
        audit(db, "account_owner_token_rotated", account_id)
        db.commit()
        return token


class ManagementService:
    def __init__(self, settings: Settings, cipher: FieldCipher):
        self.settings, self.cipher = settings, cipher

    def add_contact(self, db: Session, account_id: str, owner_type: str, owner_id: str, value: str) -> str:
        if owner_type not in {"account", "partner"}:
            raise ValueError
        token, normalized = generate_token(), value.strip().casefold()
        db.add(ContactMethod(
            account_id=account_id, owner_type=owner_type, owner_id=owner_id,
            encrypted_value=self.cipher.encrypt(normalized),
            value_fingerprint=fingerprint(normalized, self.settings.fingerprint_hmac_key),
            verification_token_hash=keyed_hash(token, self.settings.token_hmac_key),
            verification_expires_at=datetime.utcnow() + timedelta(hours=24),
        ))
        db.commit()
        return token

    def add_partner(self, db: Session, account_id: str, name: str) -> Partner:
        partner = Partner(account_id=account_id, encrypted_name=self.cipher.encrypt(name.strip()))
        db.add(partner)
        db.commit()
        return partner

    def add_trusted_person(
        self, db: Session, account_id: str, owner_type: str, owner_id: str, name: str,
    ) -> tuple[TrustedPerson, str]:
        if owner_type == "account":
            if owner_id != account_id or not db.get(Account, account_id):
                raise LookupError
        elif owner_type == "partner":
            partner = db.scalar(select(Partner).where(Partner.id == owner_id, Partner.account_id == account_id))
            if not partner:
                raise LookupError
        else:
            raise ValueError
        person = TrustedPerson(
            account_id=account_id,
            owner_type=owner_type,
            owner_id=owner_id,
            encrypted_display_name=self.cipher.encrypt(name.strip()) if name else None,
        )
        db.add(person)
        db.flush()
        token = generate_token()
        db.add(TrustedPersonToken(
            trusted_person_id=person.id, token_hash=keyed_hash(token, self.settings.token_hmac_key)
        ))
        audit(db, "trusted_person_created", account_id)
        db.commit()
        return person, token

    def rotate_trusted_token(self, db: Session, account_id: str, person_id: str) -> str:
        person = db.scalar(select(TrustedPerson).where(
            TrustedPerson.id == person_id, TrustedPerson.account_id == account_id
        ))
        if not person:
            raise LookupError
        token = generate_token()
        record = db.get(TrustedPersonToken, person_id)
        record.token_hash, record.rotated_at, record.revoked_at = (
            keyed_hash(token, self.settings.token_hmac_key), datetime.utcnow(), None
        )
        audit(db, "trusted_token_rotated", account_id)
        db.commit()
        return token


class NotificationService:
    def __init__(self, settings: Settings, cipher: FieldCipher):
        self.settings, self.cipher = settings, cipher

    def eligible_contacts(self, db: Session, person: TrustedPerson) -> list[ContactMethod]:
        excluded_owner_type = person.owner_type
        excluded_owner_id = person.owner_id
        active_partner_ids = select(Partner.id).where(
            Partner.account_id == person.account_id, Partner.is_active.is_(True)
        )
        return list(db.scalars(select(ContactMethod).where(
            ContactMethod.account_id == person.account_id,
            ContactMethod.is_verified.is_(True),
            ContactMethod.is_active.is_(True),
            or_(
                and_(
                    ContactMethod.owner_type == "account",
                    ContactMethod.owner_id == person.account_id,
                    or_(excluded_owner_type != "account", ContactMethod.owner_id != excluded_owner_id),
                ),
                and_(
                    ContactMethod.owner_type == "partner",
                    ContactMethod.owner_id.in_(active_partner_ids),
                    or_(excluded_owner_type != "partner", ContactMethod.owner_id != excluded_owner_id),
                ),
            ),
        )))

    def resolve_person(self, db: Session, token: str) -> TrustedPerson | None:
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
        if not account or account.status not in {AccountStatus.active, AccountStatus.overdue}:
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
        if not self.eligible_contacts(db, person):
            return None
        record.last_used_at = datetime.utcnow()
        return person

    @staticmethod
    def normalize_message(message: str, language: str = "de") -> str:
        value = unicodedata.normalize("NFC", message).strip()
        if not 10 <= len(value) <= 5000:
            raise ValueError(translate(language, "error.message_length"))
        return value

    def stage(self, db: Session, person: TrustedPerson, message: str) -> str:
        account = db.get(Account, person.account_id)
        value, raw = self.normalize_message(
            message, account.language_code if account else self.settings.default_language
        ), generate_token()
        db.add(Submission(
            id_hash=keyed_hash(raw, self.settings.token_hmac_key),
            trusted_person_id=person.id,
            encrypted_message=self.cipher.encrypt(value),
            expires_at=datetime.utcnow() + timedelta(minutes=15),
        ))
        db.commit()
        return raw

    def accept(self, db: Session, submission_token: str) -> Notification:
        submission = db.get(Submission, keyed_hash(submission_token, self.settings.token_hmac_key))
        if not submission or submission.consumed_at or submission.expires_at <= datetime.utcnow():
            raise LookupError
        person = db.get(TrustedPerson, submission.trusted_person_id)
        account = db.get(Account, person.account_id) if person else None
        if not person or not account or account.status not in {AccountStatus.active, AccountStatus.overdue}:
            raise LookupError
        message = self.cipher.decrypt(submission.encrypted_message)
        digest = keyed_hash(message, self.settings.fingerprint_hmac_key)
        notification = Notification(
            account_id=account.id, trusted_person_id=person.id, status=NotificationStatus.queued,
            message_digest=digest, encrypted_message_payload=self.cipher.encrypt(message),
            expires_at=datetime.utcnow() + timedelta(hours=self.settings.message_retention_hours),
            deduplication_key=submission.id_hash,
        )
        db.add(notification)
        db.flush()
        contacts = self.eligible_contacts(db, person)
        if not contacts:
            raise LookupError
        for contact in contacts:
            db.add(Delivery(notification_id=notification.id, contact_method_id=contact.id, provider=contact.channel))
        submission.consumed_at = datetime.utcnow()
        audit(db, "notification_accepted", account.id, count=len(contacts))
        db.commit()
        return notification


class DeliveryService:
    BACKOFF_MINUTES = (1, 5, 30, 120, 720, 1440)

    def __init__(self, settings: Settings, cipher: FieldCipher, providers: dict[str, NotificationProvider]):
        self.settings, self.cipher, self.providers = settings, cipher, providers

    def process_due(self, db: Session, now: datetime | None = None) -> int:
        now = now or datetime.utcnow()
        deliveries = list(db.scalars(select(Delivery).where(
            Delivery.status.in_([DeliveryStatus.pending, DeliveryStatus.retry_scheduled]),
            or_(Delivery.next_retry_at.is_(None), Delivery.next_retry_at <= now),
        ).order_by(Delivery.created_at).limit(100)))
        processed = 0
        for delivery in deliveries:
            delivery.status, delivery.attempt_count, delivery.last_attempt_at = (
                DeliveryStatus.processing, delivery.attempt_count + 1, now
            )
            db.commit()
            contact, notification = db.get(ContactMethod, delivery.contact_method_id), db.get(Notification, delivery.notification_id)
            if not contact or not contact.is_active or not notification or not notification.encrypted_message_payload:
                delivery.status = DeliveryStatus.permanent_failure
            else:
                provider = self.providers.get(delivery.provider)
                if not provider:
                    delivery.status = DeliveryStatus.permanent_failure
                else:
                    account = db.get(Account, notification.account_id)
                    language = account.language_code if account else self.settings.default_language
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
                            message=self.cipher.decrypt(notification.encrypted_message_payload),
                        ),
                        contact_method_id=contact.id,
                        delivery_id=delivery.id,
                    )
                    if result.successful:
                        delivery.status, delivery.delivered_at = DeliveryStatus.delivered, now
                        delivery.provider_message_id = result.message_id
                    elif result.permanent_failure or delivery.attempt_count >= self.settings.delivery_max_attempts:
                        delivery.status = DeliveryStatus.permanent_failure
                    else:
                        delivery.status = DeliveryStatus.retry_scheduled
                        delay = self.BACKOFF_MINUTES[min(delivery.attempt_count - 1, len(self.BACKOFF_MINUTES) - 1)]
                        delivery.next_retry_at = now + timedelta(minutes=delay)
                    if result.error_class:
                        delivery.encrypted_error_detail = self.cipher.encrypt(result.error_class)
            processed += 1
            self._update_notification(db, delivery.notification_id)
            db.commit()
        return processed

    def _update_notification(self, db: Session, notification_id: str) -> None:
        notification = db.get(Notification, notification_id)
        statuses = list(db.scalars(select(Delivery.status).where(Delivery.notification_id == notification_id)))
        if not notification:
            return
        if not statuses or all(s == DeliveryStatus.permanent_failure for s in statuses):
            notification.status = NotificationStatus.failed
        elif all(s == DeliveryStatus.delivered for s in statuses):
            notification.status = NotificationStatus.delivered
            notification.encrypted_message_payload = None
        elif any(s == DeliveryStatus.delivered for s in statuses):
            notification.status = NotificationStatus.partially_delivered


class LifecycleService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self, db: Session, now: datetime | None = None) -> None:
        now = now or datetime.utcnow()
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
            account.deletion_due_at = now + timedelta(days=self.settings.account_retention_after_disable_days)
        for account in db.scalars(select(Account).where(Account.status == AccountStatus.disabled, Account.deletion_due_at <= now)):
            account.status = AccountStatus.scheduled_for_deletion
        expired = list(db.scalars(select(Account).where(
            Account.status == AccountStatus.pending_verification,
            Account.created_at <= now - timedelta(days=self.settings.account_pending_retention_days),
        )))
        for account in expired:
            db.delete(account)
        for account in list(db.scalars(select(Account).where(
            Account.status == AccountStatus.scheduled_for_deletion, Account.deletion_due_at <= now
        ))):
            db.delete(account)
        db.execute(delete(Submission).where(Submission.expires_at <= now))
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
                delivery.status = DeliveryStatus.permanent_failure
        db.execute(delete(AuditLog).where(AuditLog.created_at <= now - timedelta(days=self.settings.audit_retention_days)))
        db.commit()
