from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email import message_from_bytes, policy
from email.message import Message
from email.utils import getaddresses
from imaplib import IMAP4_SSL

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    Account,
    ContactMethod,
    Delivery,
    DeliveryStatus,
    EmailDeliveryTracking,
    Notification,
    NotificationStatus,
)
from app.providers.base import DeliveryResult, NotificationProvider
from app.time import utc_now
from app.security.core import FieldCipher, generate_token, keyed_hash
from app.smtp_config import ImapNdrConfig, load_ndr_config


TRACKING_RETENTION_DAYS = 30
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def send_tracked_email(
    db: Session,
    settings: Settings,
    cipher: FieldCipher,
    provider: NotificationProvider,
    recipient: str,
    subject: str,
    body: str,
    *,
    contact_method_id: str | None = None,
    delivery_id: str | None = None,
) -> DeliveryResult:
    token: str | None = None
    tracking: EmailDeliveryTracking | None = None
    if load_ndr_config(db, settings, cipher):
        token = generate_token()
        tracking = EmailDeliveryTracking(
            token_hash=keyed_hash(token, settings.token_hmac_key),
            contact_method_id=contact_method_id,
            delivery_id=delivery_id,
            expires_at=utc_now() + timedelta(days=TRACKING_RETENTION_DAYS),
        )
        db.add(tracking)
        db.flush()

    result = provider.send(
        recipient, subject, body, envelope_token=token
    )
    if result.permanent_failure:
        record_permanent_delivery_failure(
            db,
            cipher,
            contact_method_id=contact_method_id,
            delivery_id=delivery_id,
            error_class=result.error_class or "permanent_delivery_failure",
        )
    if tracking and not result.successful:
        db.delete(tracking)
    return result


def record_permanent_delivery_failure(
    db: Session,
    cipher: FieldCipher,
    *,
    contact_method_id: str | None,
    delivery_id: str | None,
    error_class: str,
    now: datetime | None = None,
) -> None:
    now = now or utc_now()
    contact = db.get(ContactMethod, contact_method_id) if contact_method_id else None
    if contact:
        contact.permanent_failure_count += 1
        contact.last_permanent_failure_at = now
        contact.is_verified = False
        contact.verified_at = None
        account = db.get(Account, contact.account_id)
        if account:
            account.last_contact_problem_reminder_at = None
    delivery = db.get(Delivery, delivery_id) if delivery_id else None
    if delivery:
        delivery.status = DeliveryStatus.permanent_failure
        delivery.encrypted_error_detail = cipher.encrypt(error_class)
        update_notification_status(db, delivery.notification_id)


@dataclass(frozen=True)
class DsnReport:
    action: str
    status_code: str


class NdrMailboxProcessor:
    def __init__(
        self,
        settings: Settings,
        cipher: FieldCipher,
        *,
        imap_factory=IMAP4_SSL,
    ):
        self.settings = settings
        self.cipher = cipher
        self.imap_factory = imap_factory

    def process(self, db: Session, now: datetime | None = None) -> int:
        now = now or utc_now()
        db.execute(delete(EmailDeliveryTracking).where(
            EmailDeliveryTracking.expires_at <= now
        ))
        db.commit()
        config = load_ndr_config(db, self.settings, self.cipher)
        if not config:
            return 0

        processed_reports = 0
        with self.imap_factory(config.host, config.port, timeout=15) as imap:
            imap.login(config.username, config.password)
            status, _ = imap.select("INBOX")
            if status != "OK":
                raise OSError("Unable to select the technical mailbox.")
            status, data = imap.search(None, "ALL")
            if status != "OK":
                raise OSError("Unable to list the technical mailbox.")
            message_ids = data[0].split() if data and data[0] else []
            for message_id in message_ids:
                status, payload = imap.fetch(message_id, "(RFC822)")
                if status != "OK":
                    continue
                raw = _message_bytes(payload)
                if raw is None:
                    imap.store(message_id, "+FLAGS", "\\Deleted")
                    continue
                try:
                    message = message_from_bytes(raw, policy=policy.default)
                except Exception:
                    imap.store(message_id, "+FLAGS", "\\Deleted")
                    continue
                try:
                    token = _tracking_token(message, config)
                    reports = _dsn_reports(message)
                    if token and reports:
                        processed_reports += self._apply_reports(
                            db, token, reports, now
                        )
                    db.commit()
                except Exception:
                    db.rollback()
                    continue
                imap.store(message_id, "+FLAGS", "\\Deleted")
            imap.expunge()
        return processed_reports

    def _apply_reports(
        self,
        db: Session,
        token: str,
        reports: list[DsnReport],
        now: datetime,
    ) -> int:
        tracking = db.get(
            EmailDeliveryTracking,
            keyed_hash(token, self.settings.token_hmac_key),
        )
        if not tracking or tracking.expires_at <= now:
            return 0

        final = next((report for report in reports if report.action == "failed"), None)
        delivered = next(
            (report for report in reports if report.action == "delivered"), None
        )
        delayed = next(
            (report for report in reports if report.action == "delayed"), None
        )
        report = final or delivered or delayed
        if not report:
            return 0
        if tracking.completed_at:
            return 0

        tracking.last_reported_at = now
        tracking.result = report.action
        tracking.status_code = report.status_code[:32]
        if report.action in {"failed", "delivered"}:
            tracking.completed_at = now

        if report.action == "failed":
            record_permanent_delivery_failure(
                db,
                self.cipher,
                contact_method_id=tracking.contact_method_id,
                delivery_id=tracking.delivery_id,
                error_class=f"dsn_{report.status_code}",
                now=now,
            )
        return 1


def _message_bytes(payload: list[object]) -> bytes | None:
    for item in payload:
        if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
            return item[1]
    return None


def _tracking_token(message: Message, config: ImapNdrConfig) -> str | None:
    local, separator, domain = config.from_address.strip().partition("@")
    if not separator:
        return None
    prefix = f"{local}+"
    values = [
        str(value)
        for value in (
            message.get("To"),
            message.get("Delivered-To"),
            message.get("X-Original-To"),
            message.get("Envelope-To"),
        )
        if value
    ]
    for _, address in getaddresses(values):
        candidate_local, candidate_separator, candidate_domain = address.strip().partition(
            "@"
        )
        if (
            candidate_separator
            and candidate_domain.casefold() == domain.casefold()
            and candidate_local.casefold().startswith(prefix.casefold())
        ):
            token = candidate_local[len(prefix):]
            if _TOKEN_PATTERN.fullmatch(token):
                return token
    return None


def _dsn_reports(message: Message) -> list[DsnReport]:
    reports: list[DsnReport] = []
    for part in message.walk():
        if part.get_content_type() != "message/delivery-status":
            continue
        payload = part.get_payload()
        blocks = payload if isinstance(payload, list) else []
        for block in blocks:
            if not isinstance(block, Message):
                continue
            action = str(block.get("Action", "")).strip().casefold()
            status_code = str(block.get("Status", "")).strip()
            if action in {"failed", "delayed", "delivered"} and re.fullmatch(
                r"[245]\.\d{1,3}\.\d{1,3}", status_code
            ):
                reports.append(DsnReport(action, status_code))
    return reports


def update_notification_status(db: Session, notification_id: str) -> None:
    notification = db.get(Notification, notification_id)
    if not notification:
        return
    statuses = list(db.scalars(
        select(Delivery.status).where(Delivery.notification_id == notification_id)
    ))
    terminal = {
        DeliveryStatus.delivered,
        DeliveryStatus.permanent_failure,
        DeliveryStatus.cancelled,
    }
    if not statuses or all(status == DeliveryStatus.cancelled for status in statuses):
        notification.status = NotificationStatus.discarded
    elif all(status == DeliveryStatus.delivered for status in statuses):
        notification.status = NotificationStatus.delivered
    elif any(status == DeliveryStatus.delivered for status in statuses):
        notification.status = NotificationStatus.partially_delivered
    elif all(status in terminal for status in statuses):
        notification.status = NotificationStatus.failed
    else:
        notification.status = NotificationStatus.queued
