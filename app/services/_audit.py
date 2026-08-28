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

def audit(db: Session, event: str, account_id: str | None = None, **metadata: object) -> None:
    allowed = {key: value for key, value in metadata.items() if key in {"provider", "error_class", "count"}}
    db.add(AuditLog(
        account_id=account_id,
        event_type=event,
        technical_metadata=json.dumps(allowed),
        request_id=current_request_id(),
    ))
