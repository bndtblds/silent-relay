from datetime import timedelta
import json
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.email_tracking import NdrMailboxProcessor, send_tracked_email
from app.logging_config import JsonFormatter
from app.models import (
    Account,
    ContactMethod,
    Delivery,
    DeliveryStatus,
    EmailDeliveryTracking,
    Notification,
    NotificationStatus,
)
from app.providers.base import DeliveryResult
from app.providers.email import EmailNotificationProvider, EmailProviderConfig
from app.security.core import keyed_hash
from app.smtp_config import save_email_config, save_ndr_config
from app.time import utc_now


TOKEN = "abcdefghijklmnopqrstuvwxyzABCDEFGH1234567890_-"


class RecordingProvider:
    channel = "email"

    def __init__(self):
        self.envelope_token = None

    def send(self, recipient, subject, body, *, envelope_token=None):
        self.envelope_token = envelope_token
        return DeliveryResult(True, message_id="provider-id")


class PermanentlyRejectingProvider:
    channel = "email"

    def send(self, recipient, subject, body, *, envelope_token=None):
        return DeliveryResult(
            False,
            permanent_failure=True,
            error_class="recipient_rejected",
        )


class FakeImap:
    messages = {}
    deleted = []
    expunged = False
    selected_mailbox = None

    def __init__(self, host, port, timeout):
        assert (host, port, timeout) == ("imap.example.org", 993, 15)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def login(self, username, password):
        assert (username, password) == ("notifications@example.org", "imap-secret")

    def select(self, mailbox):
        type(self).selected_mailbox = mailbox
        return "OK", [str(len(self.messages)).encode()]

    def search(self, *_):
        return "OK", [b" ".join(self.messages)]

    def fetch(self, message_id, _):
        return "OK", [(b"RFC822", self.messages[message_id])]

    def store(self, message_id, *_):
        type(self).deleted.append(message_id)
        return "OK", []

    def expunge(self):
        type(self).expunged = True
        return "OK", []


def _configure_ndr(db, settings, cipher):
    save_email_config(
        db,
        cipher,
        host="smtp.example.org",
        port=587,
        username="notifications@example.org",
        password="smtp-secret",
        from_address="notifications@example.org",
        from_name="SilentRelay",
    )
    save_ndr_config(
        db,
        settings,
        cipher,
        host="imap.example.org",
        port=993,
        username="notifications@example.org",
        password="imap-secret",
        acknowledged_address="notifications@example.org",
    )


def _dsn(action: str, status: str, token: str = TOKEN) -> bytes:
    return f"""From: MAILER-DAEMON@example.net
To: notifications+{token}@example.org
MIME-Version: 1.0
Content-Type: multipart/report; report-type=delivery-status; boundary=dsn

--dsn
Content-Type: text/plain

Delivery report
--dsn
Content-Type: message/delivery-status

Reporting-MTA: dns; mx.example.net

Final-Recipient: rfc822; hidden@example.net
Action: {action}
Status: {status}
--dsn--
""".replace("\n", "\r\n").encode()


def _ordinary_mail() -> bytes:
    return b"From: person@example.net\r\nTo: notifications@example.org\r\n\r\nHello"


def _tracking(db, settings, *, contact_id=None, delivery_id=None):
    tracking = EmailDeliveryTracking(
        token_hash=keyed_hash(TOKEN, settings.token_hmac_key),
        contact_method_id=contact_id,
        delivery_id=delivery_id,
        expires_at=utc_now() + timedelta(days=1),
    )
    db.add(tracking)
    db.commit()
    return tracking


def _run(db, settings, cipher, messages):
    FakeImap.messages = messages
    FakeImap.deleted = []
    FakeImap.expunged = False
    FakeImap.selected_mailbox = None
    return NdrMailboxProcessor(
        settings, cipher, imap_factory=FakeImap
    ).process(db)


def test_outgoing_mail_uses_opaque_envelope_token_and_stores_only_hash(
    db, settings, cipher
):
    _configure_ndr(db, settings, cipher)
    provider = RecordingProvider()

    result = send_tracked_email(
        db, settings, cipher, provider, "recipient@example.net", "Subject", "Body"
    )
    db.commit()

    assert result.successful
    assert provider.envelope_token
    tracking = db.scalar(select(EmailDeliveryTracking))
    assert tracking.token_hash == keyed_hash(
        provider.envelope_token, settings.token_hmac_key
    )
    assert provider.envelope_token not in tracking.token_hash


def test_immediate_permanent_rejection_marks_contact_and_delivery_failed(
    db, settings, cipher
):
    account = Account(status="active")
    db.add(account)
    db.flush()
    contact = ContactMethod(
        account_id=account.id,
        owner_type="account",
        owner_id=account.id,
        encrypted_value=cipher.encrypt("missing@example.org"),
        value_fingerprint="fingerprint",
        is_verified=True,
        verified_at=utc_now(),
    )
    notification = Notification(
        account_id=account.id,
        status=NotificationStatus.queued,
        encrypted_message_payload=cipher.encrypt("message"),
        deduplication_key="immediate-rejection",
    )
    db.add_all([contact, notification])
    db.flush()
    delivery = Delivery(
        notification_id=notification.id,
        contact_method_id=contact.id,
        provider="email",
        status=DeliveryStatus.processing,
    )
    db.add(delivery)
    db.flush()

    result = send_tracked_email(
        db,
        settings,
        cipher,
        PermanentlyRejectingProvider(),
        "missing@example.org",
        "Subject",
        "Body",
        contact_method_id=contact.id,
        delivery_id=delivery.id,
    )
    db.commit()

    assert not result.successful
    assert result.permanent_failure
    assert not contact.is_verified
    assert contact.verified_at is None
    assert contact.permanent_failure_count == 1
    assert contact.last_permanent_failure_at is not None
    assert delivery.status == DeliveryStatus.permanent_failure
    assert cipher.decrypt(delivery.encrypted_error_detail) == "recipient_rejected"
    assert notification.status == NotificationStatus.failed
    assert db.scalar(select(EmailDeliveryTracking)) is None


def test_permanent_dsn_marks_contact_and_delivery_failed(db, settings, cipher):
    _configure_ndr(db, settings, cipher)
    account = Account(status="active")
    db.add(account)
    db.flush()
    contact = ContactMethod(
        account_id=account.id,
        owner_type="account",
        owner_id=account.id,
        encrypted_value=cipher.encrypt("recipient@example.net"),
        value_fingerprint="fingerprint",
        is_verified=True,
        verified_at=utc_now(),
    )
    notification = Notification(
        account_id=account.id,
        status=NotificationStatus.delivered,
        deduplication_key="dedupe",
    )
    db.add_all([contact, notification])
    db.flush()
    delivery = Delivery(
        notification_id=notification.id,
        contact_method_id=contact.id,
        provider="email",
        status=DeliveryStatus.delivered,
    )
    db.add(delivery)
    db.flush()
    tracking = _tracking(
        db, settings, contact_id=contact.id, delivery_id=delivery.id
    )

    assert _run(db, settings, cipher, {b"1": _dsn("failed", "5.1.1")}) == 1

    db.refresh(contact)
    db.refresh(delivery)
    db.refresh(notification)
    db.refresh(tracking)
    assert not contact.is_verified
    assert contact.permanent_failure_count == 1
    assert contact.last_permanent_failure_at is not None
    assert delivery.status == DeliveryStatus.permanent_failure
    assert cipher.decrypt(delivery.encrypted_error_detail) == "dsn_5.1.1"
    assert notification.status == NotificationStatus.failed
    assert tracking.result == "failed"
    assert tracking.completed_at is not None
    assert FakeImap.deleted == [b"1"]
    assert FakeImap.expunged
    assert FakeImap.selected_mailbox == "INBOX"


def test_delayed_then_failed_and_duplicate_reports_are_idempotent(
    db, settings, cipher
):
    _configure_ndr(db, settings, cipher)
    tracking = _tracking(db, settings)

    assert _run(db, settings, cipher, {b"1": _dsn("delayed", "4.2.0")}) == 1
    db.refresh(tracking)
    assert tracking.result == "delayed"
    assert tracking.completed_at is None

    assert _run(db, settings, cipher, {b"2": _dsn("failed", "5.1.1")}) == 1
    db.refresh(tracking)
    assert tracking.result == "failed"
    assert tracking.completed_at is not None

    assert _run(db, settings, cipher, {b"3": _dsn("failed", "5.1.1")}) == 0
    assert FakeImap.deleted == [b"3"]


def test_untrusted_and_ordinary_messages_are_deleted_without_tracking(
    db, settings, cipher
):
    _configure_ndr(db, settings, cipher)
    _tracking(db, settings)
    malformed = b"To: notifications+" + TOKEN.encode() + b"@example.org\r\n\r\nNot a DSN"

    assert _run(
        db,
        settings,
        cipher,
        {
            b"1": _dsn("failed", "5.1.1", token="Z" * 44),
            b"2": malformed,
            b"3": _ordinary_mail(),
        },
    ) == 0
    assert FakeImap.deleted == [b"1", b"2", b"3"]
    tracking = db.scalar(select(EmailDeliveryTracking))
    assert tracking.result == "pending"
    assert tracking.last_reported_at is None


def test_unexpected_message_processing_failure_is_logged_safely_and_retried(
    db, settings, cipher, monkeypatch, caplog
):
    _configure_ndr(db, settings, cipher)
    tracking = _tracking(db, settings)
    FakeImap.messages = {
        b"1": _dsn("failed", "5.1.1"),
        b"2": _dsn("failed", "5.1.1"),
    }
    FakeImap.deleted = []
    FakeImap.expunged = False
    processor = NdrMailboxProcessor(settings, cipher, imap_factory=FakeImap)
    apply_reports = processor._apply_reports
    secret = "owner@example.org tracking-token imap-password"
    attempts = 0

    def fail_once(db, token, reports, now):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise IntegrityError(secret, {}, RuntimeError(secret))
        return apply_reports(db, token, reports, now)

    monkeypatch.setattr(processor, "_apply_reports", fail_once)

    with caplog.at_level(logging.ERROR, logger="silent_relay"):
        processed = processor.process(db)

    records = [
        record for record in caplog.records
        if record.getMessage() == "ndr_message_processing_failed"
    ]
    assert len(records) == 1
    payload = json.loads(JsonFormatter().format(records[0]))
    assert payload["event"] == "ndr_message_processing_failed"
    assert payload["error_class"] == "IntegrityError"
    assert secret not in json.dumps(payload)
    assert records[0].exc_info is None
    assert processed == 1
    assert FakeImap.deleted == [b"2"]
    assert FakeImap.expunged
    db.refresh(tracking)
    assert tracking.result == "failed"


def test_smtp_visible_from_stays_stable_while_envelope_sender_is_correlated(
    settings, monkeypatch
):
    captured = {}

    class FakeSmtp:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def starttls(self, *, context):
            captured["starttls"] = context.check_hostname

        def send_message(self, message, *, from_addr, to_addrs):
            captured.update(
                visible_from=message["From"],
                from_addr=from_addr,
                to_addrs=to_addrs,
            )

    monkeypatch.setattr("app.providers.email.smtplib.SMTP", FakeSmtp)
    provider = EmailNotificationProvider(
        EmailProviderConfig(
            host="smtp.example.org",
            port=25,
            username="",
            password="",
            from_address="notifications@example.org",
            from_name="SilentRelay",
        ),
    )

    assert provider.send(
        "recipient@example.net", "Subject", "Body", envelope_token=TOKEN
    ).successful
    assert captured["starttls"] is True
    assert captured["visible_from"] == "SilentRelay <notifications@example.org>"
    assert captured["from_addr"] == f"notifications+{TOKEN}@example.org"
    assert captured["to_addrs"] == ["recipient@example.net"]
