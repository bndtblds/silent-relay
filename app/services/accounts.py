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

class AccountService:
    def __init__(self, settings: Settings, cipher: FieldCipher):
        self.settings, self.cipher = settings, cipher

    def create(self, db: Session, language_code: str = "de") -> tuple[Account, str, str]:
        if not system_configuration(db).account_creation_enabled:
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
            setup_expires_at=utc_now() + timedelta(hours=24),
        ))
        audit(db, "account_created", account.id)
        db.commit()
        return account, account_owner_token, setup_token

    @staticmethod
    def normalize_owner_name(name: str, language: str) -> str:
        normalized = name.strip()
        if not 1 <= len(normalized) <= 200:
            raise ValueError(translate(language, "error.owner_name"))
        return normalized

    def setup(
        self, db: Session, setup_token: str, password: str, email: str,
        owner_name: str = "Account holder",
    ) -> tuple[Account, str]:
        token_hash = keyed_hash(setup_token, self.settings.token_hmac_key)
        credential = db.scalar(select(AccountOwnerCredential).where(AccountOwnerCredential.setup_token_hash == token_hash))
        if not credential or not credential.setup_expires_at or credential.setup_expires_at <= utc_now():
            raise LookupError(translate(self.settings.default_language, "error.setup_link"))
        language = credential.account.language_code
        normalized_name = self.normalize_owner_name(owner_name, language)
        try:
            normalized_email = normalize_email_address(email)
        except ValueError as exc:
            raise ValueError(translate(language, "error.email")) from exc
        try:
            credential.password_hash = hash_password(password)
        except ValueError as exc:
            raise ValueError(translate(language, "error.password_invalid")) from exc
        credential.password_changed_at = utc_now()
        credential.account.encrypted_owner_name = self.cipher.encrypt(normalized_name)
        credential.setup_token_hash = None
        credential.setup_expires_at = None
        verification_token = generate_token()
        db.add(ContactMethod(
            account_id=credential.account_id, owner_type="account", owner_id=credential.account_id,
            encrypted_value=self.cipher.encrypt(normalized_email),
            value_fingerprint=fingerprint(normalized_email, self.settings.fingerprint_hmac_key),
            verification_token_hash=keyed_hash(verification_token, self.settings.token_hmac_key),
            verification_expires_at=utc_now() + timedelta(hours=24),
        ))
        audit(db, "account_setup", credential.account_id)
        db.commit()
        return credential.account, verification_token

    def contact_confirmation_account(self, db: Session, token: str) -> Account | None:
        now = utc_now()
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
        now = utc_now()
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
            config = system_configuration(db)
            self._create_review(db, account, now + timedelta(days=config.account_review_interval_days))
        elif account and current_contact_review:
            self._finish_review_if_complete(db, account, current_contact_review.account_review_id, now)
        audit(db, "contact_verified", contact.account_id)
        db.commit()
        return account

    def _create_review(self, db: Session, account: Account, due: datetime) -> AccountReview:
        review = AccountReview(account_id=account.id, review_due_at=due)
        db.add(review)
        db.flush()
        config = system_configuration(db)
        for day in review_reminder_days(config):
            db.add(ReviewReminder(account_review_id=review.id, relative_day=day, scheduled_at=due + timedelta(days=day)))
        account.next_review_due_at = due
        account.review_grace_due_at = due + timedelta(days=config.account_review_grace_days)
        return review

    def confirm_review(self, db: Session, account: Account) -> bool:
        now = utc_now()
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
            db, account, current.id, utc_now()
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
            db, account, now + timedelta(days=system_configuration(db).account_review_interval_days)
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
        now = utc_now()
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
        SessionManager(self.settings).revoke_account_owner_sessions(db, account_id)
        audit(db, "account_owner_token_rotated", account_id)
        db.commit()
        return token

    def change_password(
        self, db: Session, account_id: str, current_password: str, new_password: str
    ) -> bool:
        credential = db.get(AccountOwnerCredential, account_id)
        if not credential or not verify_password(credential.password_hash, current_password):
            return False
        if verify_password(credential.password_hash, new_password):
            raise ValueError
        credential.password_hash = hash_password(new_password)
        credential.password_changed_at = utc_now()
        SessionManager(self.settings).revoke_account_owner_sessions(db, account_id)
        audit(db, "account_owner_password_changed", account_id)
        db.commit()
        return True
class PartnerAuthenticationService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def resolve_access(self, db: Session, token: str) -> tuple[Partner, PartnerCredential] | None:
        credential = db.scalar(select(PartnerCredential).where(
            PartnerCredential.token_hash == keyed_hash(token, self.settings.token_hmac_key)
        ))
        partner = db.get(Partner, credential.partner_id) if credential else None
        account = db.get(Account, partner.account_id) if partner else None
        if (
            not credential or not partner or not partner.is_active or not account
            or account.status not in {AccountStatus.active, AccountStatus.overdue}
            or account.is_admin_locked
        ):
            return None
        return partner, credential

    def enroll(self, db: Session, credential: PartnerCredential, password: str) -> None:
        now = utc_now()
        if credential.password_hash or credential.enrollment_expires_at <= now:
            raise LookupError
        credential.password_hash = hash_password(password)
        credential.enrolled_at = now
        credential.password_changed_at = now
        credential.failed_login_count = 0
        credential.locked_until = None
        db.commit()

    def login(self, db: Session, credential: PartnerCredential, password: str) -> bool:
        now = utc_now()
        if (
            not credential.password_hash
            or (credential.locked_until and credential.locked_until > now)
            or not verify_password(credential.password_hash, password)
        ):
            credential.failed_login_count += 1
            if credential.failed_login_count >= 5:
                credential.locked_until = now + timedelta(
                    minutes=min(30, credential.failed_login_count)
                )
            db.commit()
            return False
        credential.failed_login_count = 0
        credential.locked_until = None
        db.commit()
        return True

    def change_password(
        self, db: Session, partner_id: str, current_password: str, new_password: str
    ) -> bool:
        credential = db.get(PartnerCredential, partner_id)
        if not credential or not verify_password(credential.password_hash, current_password):
            return False
        if verify_password(credential.password_hash, new_password):
            raise ValueError
        credential.password_hash = hash_password(new_password)
        credential.password_changed_at = utc_now()
        SessionManager(self.settings).revoke_partner_sessions(db, partner_id)
        audit(db, "partner_password_changed", db.get(Partner, partner_id).account_id)
        db.commit()
        return True
