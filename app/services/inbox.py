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

class InboxService:
    def __init__(self, cipher: FieldCipher):
        self.cipher = cipher

    @staticmethod
    def recipient(
        db: Session, notification_id: str, owner_type: str, owner_id: str
    ) -> NotificationRecipient | None:
        recipient = db.scalar(select(NotificationRecipient).where(
            NotificationRecipient.notification_id == notification_id,
            NotificationRecipient.owner_type == owner_type,
            NotificationRecipient.owner_id == owner_id,
        ))
        if not recipient:
            return None
        notification = db.get(Notification, notification_id)
        if not notification or not notification.encrypted_message_payload or notification.expires_at <= utc_now():
            return None
        if owner_type == "account":
            account = db.get(Account, owner_id)
            return recipient if account and account.id == notification.account_id and account.allows_access else None
        if owner_type == "partner":
            partner = db.get(Partner, owner_id)
            credential = db.get(PartnerCredential, owner_id)
            account = db.get(Account, partner.account_id) if partner else None
            return recipient if partner and account and account.id == notification.account_id and account.allows_access and partner.is_active and credential and credential.enrolled_at and credential.password_hash else None
        return None

    def messages(self, db: Session, owner_type: str, owner_id: str) -> list[tuple[NotificationRecipient, Notification]]:
        if owner_type == "account":
            account = db.get(Account, owner_id)
            if not account or not account.allows_access:
                return []
            account_id = account.id
        elif owner_type == "partner":
            partner = db.get(Partner, owner_id)
            credential = db.get(PartnerCredential, owner_id)
            account = db.get(Account, partner.account_id) if partner else None
            if not partner or not account or not account.allows_access or not partner.is_active or not credential or not credential.enrolled_at or not credential.password_hash:
                return []
            account_id = partner.account_id
        else:
            return []
        rows = list(db.execute(select(NotificationRecipient, Notification).join(
            Notification, Notification.id == NotificationRecipient.notification_id
        ).where(
            NotificationRecipient.owner_type == owner_type,
            NotificationRecipient.owner_id == owner_id,
            Notification.account_id == account_id,
            Notification.encrypted_message_payload.is_not(None),
            Notification.expires_at > utc_now(),
        ).order_by(Notification.release_at.desc())))
        return [(recipient, notification) for recipient, notification in rows]

    def confirm_read(self, db: Session, notification_id: str, owner_type: str, owner_id: str) -> bool:
        existing = db.scalar(select(NotificationRecipient).where(
            NotificationRecipient.notification_id == notification_id,
            NotificationRecipient.owner_type == owner_type,
            NotificationRecipient.owner_id == owner_id,
        ))
        if existing and existing.read_at is not None:
            if owner_type == "account":
                account = db.get(Account, owner_id)
                return bool(account and account.allows_access)
            partner = db.get(Partner, owner_id) if owner_type == "partner" else None
            account = db.get(Account, partner.account_id) if partner else None
            return bool(partner and account and account.allows_access and partner.is_active)
        if not self.recipient(db, notification_id, owner_type, owner_id):
            db.rollback()
            db.refresh(existing)
            return existing.read_at is not None
        now = utc_now()
        result = db.execute(update(NotificationRecipient).where(
            NotificationRecipient.notification_id == notification_id,
            NotificationRecipient.owner_type == owner_type,
            NotificationRecipient.owner_id == owner_id,
            NotificationRecipient.read_at.is_(None),
        ).values(read_at=now))
        db.commit()
        if result.rowcount not in {0, 1}:
            return False
        self.erase_if_complete(db, notification_id, now)
        return True

    @staticmethod
    def erase_if_complete(db: Session, notification_id: str, now: datetime) -> None:
        notification = db.get(Notification, notification_id)
        if not notification or not notification.encrypted_message_payload:
            return
        blocking = 0
        recipients = list(db.scalars(select(NotificationRecipient).where(
            NotificationRecipient.notification_id == notification_id,
        )))
        if not recipients:
            return
        for recipient in recipients:
            if recipient.read_at is not None:
                continue
            if recipient.owner_type == "account":
                account = db.get(Account, recipient.owner_id)
                blocking += int(bool(account and account.allows_access))
            elif recipient.owner_type == "partner":
                partner = db.get(Partner, recipient.owner_id)
                account = db.get(Account, partner.account_id) if partner else None
                blocking += int(bool(partner and account and account.allows_access and partner.is_active))
        if blocking == 0:
            db.execute(update(Notification).where(
                Notification.id == notification_id,
                Notification.encrypted_message_payload.is_not(None),
            ).values(encrypted_message_payload=None, expires_at=now))
            audit(db, "notification_content_erased", notification.account_id)
            db.commit()
