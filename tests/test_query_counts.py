from datetime import timedelta

from sqlalchemy import event, select
from starlette.requests import Request

from app.main import app
from app.models import (
    Account,
    AccountStatus,
    ContactMethod,
    Notification,
    NotificationRecipient,
    NotificationStatus,
    Partner,
    PartnerCredential,
    TrustedPerson,
)
from app.routers.web import dashboard, inbox_rows
from app.time import utc_now


def count_queries(db, operation) -> int:
    count = 0

    def before_cursor_execute(*_args):
        nonlocal count
        count += 1

    event.listen(db.bind, "before_cursor_execute", before_cursor_execute)
    try:
        operation()
    finally:
        event.remove(db.bind, "before_cursor_execute", before_cursor_execute)
    return count


def create_account(db, cipher) -> Account:
    account = Account(
        encrypted_owner_name=cipher.encrypt("Account owner"),
        status=AccountStatus.active,
        activated_at=utc_now(),
    )
    db.add(account)
    db.commit()
    return account


def add_partner(db, cipher, account: Account, number: int) -> Partner:
    now = utc_now()
    partner = Partner(
        account_id=account.id,
        encrypted_name=cipher.encrypt(f"Partner {number}"),
    )
    db.add(partner)
    db.flush()
    db.add(PartnerCredential(
        partner_id=partner.id,
        token_hash=f"partner-token-{number}",
        password_hash="enrolled-password-hash",
        enrollment_expires_at=now + timedelta(days=14),
        enrolled_at=now,
    ))
    db.add(ContactMethod(
        account_id=account.id,
        owner_type="partner",
        owner_id=partner.id,
        encrypted_value=cipher.encrypt(f"partner-{number}@example.org"),
        value_fingerprint=f"partner-contact-{number}",
        is_verified=True,
        verified_at=now,
    ))
    db.add(TrustedPerson(
        account_id=account.id,
        owner_type="partner",
        owner_id=partner.id,
        encrypted_display_name=cipher.encrypt(f"Trusted person {number}"),
    ))
    db.commit()
    return partner


def dashboard_request() -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/account/dashboard",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "app": app,
        "router": app.router,
    })


def test_dashboard_query_count_does_not_grow_with_partners(db, settings, cipher):
    account = create_account(db, cipher)
    add_partner(db, cipher, account, 1)

    def measure() -> int:
        db.expire_all()
        measured_account = db.get(Account, account.id)
        return count_queries(db, lambda: dashboard(
            dashboard_request(), measured_account, None, db, settings
        ))

    one_partner = measure()
    for number in range(2, 6):
        add_partner(db, cipher, account, number)
    five_partners = measure()

    assert abs(five_partners - one_partner) <= 1, (
        f"dashboard queries grew from {one_partner} to {five_partners}"
    )


def add_inbox_message(db, cipher, account: Account, partner: Partner, number: int) -> None:
    now = utc_now()
    trusted_person = db.scalar(select(TrustedPerson).where(
        TrustedPerson.owner_type == "partner",
        TrustedPerson.owner_id == partner.id,
    ))
    assert trusted_person is not None
    notification = Notification(
        account_id=account.id,
        trusted_person_id=trusted_person.id,
        status=NotificationStatus.delivered,
        encrypted_message_payload=cipher.encrypt(f"Message {number}"),
        release_at=now,
        recipients_frozen_at=now,
        expires_at=now + timedelta(days=30),
        deduplication_key=f"deduplication-key-{number}",
    )
    db.add(notification)
    db.flush()
    db.add(NotificationRecipient(
        notification_id=notification.id,
        owner_type="account",
        owner_id=account.id,
    ))
    db.commit()


def test_inbox_query_count_does_not_grow_with_messages(db, cipher):
    account = create_account(db, cipher)
    partners = [add_partner(db, cipher, account, number) for number in range(1, 11)]
    add_inbox_message(db, cipher, account, partners[0], 1)

    def measure() -> int:
        db.expire_all()
        db.get(Account, account.id)
        return count_queries(
            db, lambda: inbox_rows(db, cipher, "de", "account", account.id)
        )

    one_message = measure()
    for number, partner in enumerate(partners[1:], start=2):
        add_inbox_message(db, cipher, account, partner, number)
    ten_messages = measure()

    assert abs(ten_messages - one_message) <= 1, (
        f"inbox queries grew from {one_message} to {ten_messages}"
    )
