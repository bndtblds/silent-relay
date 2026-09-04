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
from app.services.accounts import AccountService
from app.services.inbox import InboxService

class ManagementService:
    def __init__(self, settings: Settings, cipher: FieldCipher):
        self.settings, self.cipher = settings, cipher

    def update_owner_name(self, db: Session, account: Account, name: str) -> None:
        normalized = AccountService.normalize_owner_name(name, account.language_code)
        account.encrypted_owner_name = self.cipher.encrypt(normalized)
        audit(db, "account_owner_name_changed", account.id)
        db.commit()

    def add_contact(self, db: Session, account_id: str, owner_type: str, owner_id: str, value: str) -> str:
        if owner_type == "account":
            if owner_id != account_id or not db.get(Account, account_id):
                raise LookupError
        elif owner_type == "partner":
            partner = db.scalar(select(Partner).where(
                Partner.id == owner_id, Partner.account_id == account_id
            ))
            if not partner:
                raise LookupError
        else:
            raise ValueError
        token, normalized = generate_token(), normalize_email_address(value)
        db.add(ContactMethod(
            account_id=account_id, owner_type=owner_type, owner_id=owner_id,
            encrypted_value=self.cipher.encrypt(normalized),
            value_fingerprint=fingerprint(normalized, self.settings.fingerprint_hmac_key),
            verification_token_hash=keyed_hash(token, self.settings.token_hmac_key),
            verification_expires_at=utc_now() + timedelta(hours=24),
        ))
        db.commit()
        return token
    def add_partner(self, db: Session, account_id: str, name: str) -> Partner:
        partner, _ = self.add_partner_with_access(db, account_id, name)
        return partner

    def add_partner_with_access(
        self, db: Session, account_id: str, name: str
    ) -> tuple[Partner, str]:
        partner = Partner(account_id=account_id, encrypted_name=self.cipher.encrypt(name.strip()))
        db.add(partner)
        db.flush()
        token = generate_token()
        db.add(PartnerCredential(
            partner_id=partner.id,
            token_hash=keyed_hash(token, self.settings.token_hmac_key),
            enrollment_expires_at=utc_now() + timedelta(days=14),
        ))
        audit(db, "partner_created", account_id)
        db.commit()
        return partner, token

    def rotate_partner_access(self, db: Session, account_id: str, partner_id: str) -> str:
        partner = db.scalar(select(Partner).where(
            Partner.id == partner_id, Partner.account_id == account_id
        ))
        credential = db.get(PartnerCredential, partner_id) if partner else None
        if not partner:
            raise LookupError
        token = generate_token()
        now = utc_now()
        token_hash = keyed_hash(token, self.settings.token_hmac_key)
        if credential is None:
            credential = PartnerCredential(
                partner_id=partner.id,
                token_hash=token_hash,
                enrollment_expires_at=now + timedelta(days=14),
                rotated_at=now,
            )
            db.add(credential)
        else:
            credential.token_hash = token_hash
            credential.password_hash = None
            credential.enrolled_at = None
            credential.enrollment_expires_at = now + timedelta(days=14)
            credential.password_changed_at = None
            credential.rotated_at = now
            credential.failed_login_count = 0
            credential.locked_until = None
            credential.setup_notified_at = None
            credential.expiry_notified_at = None
        SessionManager(self.settings).revoke_partner_sessions(db, partner_id)
        audit(db, "partner_access_rotated", account_id)
        db.commit()
        return token

    def set_partner_active(
        self, db: Session, account_id: str, partner_id: str, active: bool
    ) -> None:
        partner = db.scalar(select(Partner).where(
            Partner.id == partner_id, Partner.account_id == account_id
        ))
        if not partner:
            raise LookupError
        partner.is_active = active
        if not active:
            SessionManager(self.settings).revoke_partner_sessions(db, partner_id)
        audit(db, "partner_enabled" if active else "partner_disabled", account_id)
        db.commit()
        if not active:
            for notification_id in db.scalars(select(NotificationRecipient.notification_id).where(
                NotificationRecipient.owner_type == "partner",
                NotificationRecipient.owner_id == partner_id,
            )):
                InboxService.erase_if_complete(db, notification_id, utc_now())

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
            trusted_person_id=person.id,
            token_hash=keyed_hash(token, self.settings.token_hmac_key),
            enrollment_expires_at=utc_now() + timedelta(days=14),
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
            keyed_hash(token, self.settings.token_hmac_key), utc_now(), None
        )
        record.pin_hash = None
        record.enrollment_expires_at = utc_now() + timedelta(days=14)
        record.enrolled_at = None
        record.failed_pin_attempts = 0
        record.locked_until = None
        record.setup_notified_at = None
        record.expiry_notified_at = None
        db.execute(delete(ServerSession).where(
            ServerSession.trusted_person_id == person_id
        ))
        audit(db, "trusted_token_rotated", account_id)
        db.commit()
        return token
