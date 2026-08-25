from datetime import timedelta
import threading

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.model_base import Base
from app.models import (
    ContactMethod, Delivery, Notification, NotificationRecipient, Partner, PartnerCredential,
    ServerSession, SystemConfiguration,
)
from app.security.core import SessionManager, hash_password, keyed_hash, verify_password
from app.routers.web import inbox_rows
from app.services import (
    AccountService, DeliveryService, InboxService, LifecycleService,
    ManagementService, NotificationService, PartnerAuthenticationService,
)
from app.time import utc_now


def active_account(db, settings, cipher):
    service = AccountService(settings, cipher)
    account, _, setup = service.create(db)
    _, verification = service.setup(
        db, setup, "correct horse battery staple", "owner@example.org"
    )
    service.verify_contact(db, verification)
    return account


def activate_partner(db, settings, credential, password="partner secure password"):
    PartnerAuthenticationService(settings).enroll(db, credential, password)


def release_message(db, settings, cipher, account, trusted_owner_type="account", trusted_owner_id=None):
    management = ManagementService(settings, cipher)
    _, token = management.add_trusted_person(
        db, account.id, trusted_owner_type, trusted_owner_id or account.id, "Trusted"
    )
    service = NotificationService(settings, cipher)
    return service.accept(db, service.stage(
        db, service.resolve_person(db, token), "A private message only the inbox may show."
    ))


def test_partner_access_is_one_time_hashed_and_expires(db, settings, cipher):
    account = active_account(db, settings, cipher)
    partner, token = ManagementService(settings, cipher).add_partner_with_access(
        db, account.id, "Partner"
    )
    credential = db.get(PartnerCredential, partner.id)
    assert token not in credential.token_hash
    assert credential.password_hash is None
    assert credential.enrollment_expires_at <= credential.created_at + timedelta(days=14, seconds=1)

    activate_partner(db, settings, credential)
    assert credential.password_hash != "partner secure password"
    assert verify_password(credential.password_hash, "partner secure password")
    try:
        PartnerAuthenticationService(settings).enroll(db, credential, "another secure password")
    except LookupError:
        pass
    else:
        raise AssertionError("partner access was enrolled twice")


def test_migrated_partner_without_credentials_can_receive_first_access(
    db, settings, cipher
):
    account = active_account(db, settings, cipher)
    partner = Partner(
        account_id=account.id,
        encrypted_name=cipher.encrypt("Migrated partner"),
    )
    db.add(partner)
    db.commit()
    assert db.get(PartnerCredential, partner.id) is None

    token = ManagementService(settings, cipher).rotate_partner_access(
        db, account.id, partner.id
    )
    credential = db.get(PartnerCredential, partner.id)

    assert credential is not None
    assert credential.token_hash == keyed_hash(token, settings.token_hmac_key)
    assert credential.password_hash is None
    assert credential.enrollment_expires_at <= utc_now() + timedelta(days=14, seconds=1)
    activate_partner(db, settings, credential, "migrated partner password")
    resolved = PartnerAuthenticationService(settings).resolve_access(db, token)
    assert resolved is not None
    assert resolved[0].id == partner.id


def test_fixed_recipient_is_one_person_with_multiple_deliveries(db, settings, cipher):
    account = active_account(db, settings, cipher)
    management = ManagementService(settings, cipher)
    partner, _ = management.add_partner_with_access(db, account.id, "Partner")
    activate_partner(db, settings, db.get(PartnerCredential, partner.id))
    for address in ("one@example.org", "two@example.org"):
        token = management.add_contact(db, account.id, "partner", partner.id, address)
        AccountService(settings, cipher).verify_contact(db, token)

    notification = release_message(db, settings, cipher, account)
    recipients = list(db.scalars(select(NotificationRecipient).where(
        NotificationRecipient.notification_id == notification.id
    )))
    assert {(item.owner_type, item.owner_id) for item in recipients} == {
        ("account", account.id), ("partner", partner.id)
    }
    assert db.scalar(select(func.count()).select_from(Delivery).where(
        Delivery.notification_id == notification.id
    )) == 3


def test_activated_partner_without_verified_contact_receives_inbox_message(
    db, settings, cipher
):
    account = active_account(db, settings, cipher)
    management = ManagementService(settings, cipher)
    partner, _ = management.add_partner_with_access(db, account.id, "Partner")
    activate_partner(db, settings, db.get(PartnerCredential, partner.id))
    contact_token = management.add_contact(
        db, account.id, "partner", partner.id, "pending@example.org"
    )

    notification = release_message(db, settings, cipher, account)

    recipients = list(db.scalars(select(NotificationRecipient).where(
        NotificationRecipient.notification_id == notification.id
    )))
    assert {(item.owner_type, item.owner_id) for item in recipients} == {
        ("account", account.id), ("partner", partner.id)
    }
    assert db.scalar(select(func.count()).select_from(Delivery).where(
        Delivery.notification_id == notification.id
    )) == 1
    assert InboxService(cipher).messages(db, "partner", partner.id)
    AccountService(settings, cipher).verify_contact(db, contact_token)
    DeliveryService._freeze_recipients(db, notification.release_at + timedelta(minutes=1))
    assert db.scalar(select(func.count()).select_from(Delivery).where(
        Delivery.notification_id == notification.id
    )) == 1


def test_inbox_rows_include_iso_utc_for_local_browser_display(db, settings, cipher):
    account = active_account(db, settings, cipher)
    notification = release_message(db, settings, cipher, account)

    rows = inbox_rows(db, cipher, "de", "account", account.id)

    assert len(rows) == 1
    assert rows[0]["released_iso"] == notification.release_at.isoformat().replace(
        "+00:00", "Z"
    )
    assert rows[0]["released_iso"].endswith("Z")


def test_trusted_person_can_submit_when_no_verified_email_contact_remains(
    db, settings, cipher
):
    account = active_account(db, settings, cipher)
    owner_contact = db.scalar(select(ContactMethod).where(
        ContactMethod.account_id == account.id,
        ContactMethod.owner_type == "account",
    ))
    owner_contact.is_active = False
    owner_contact.is_verified = False
    db.commit()

    notification = release_message(db, settings, cipher, account)

    assert InboxService(cipher).messages(db, "account", account.id)
    assert db.scalar(select(func.count()).select_from(Delivery).where(
        Delivery.notification_id == notification.id
    )) == 0


def test_later_partner_cannot_read_earlier_message(db, settings, cipher):
    account = active_account(db, settings, cipher)
    notification = release_message(db, settings, cipher, account)
    partner, _ = ManagementService(settings, cipher).add_partner_with_access(
        db, account.id, "Later partner"
    )
    activate_partner(db, settings, db.get(PartnerCredential, partner.id))
    assert InboxService(cipher).recipient(db, notification.id, "partner", partner.id) is None


def test_account_inbox_recipient_does_not_require_an_active_contact(db, settings, cipher):
    config = db.get(SystemConfiguration, "default")
    config.notification_delay_minutes = 60
    account = active_account(db, settings, cipher)
    notification = release_message(db, settings, cipher, account)
    owner_contact = db.scalar(select(ContactMethod).where(
        ContactMethod.account_id == account.id,
        ContactMethod.owner_type == "account",
    ))
    owner_contact.is_active = False
    db.commit()
    DeliveryService._freeze_recipients(db, notification.release_at)
    owner_contact.is_active = True
    db.commit()
    DeliveryService._freeze_recipients(db, notification.release_at + timedelta(minutes=1))
    db.refresh(notification)
    assert notification.recipients_frozen_at is not None
    recipients = list(db.scalars(select(NotificationRecipient).where(
        NotificationRecipient.notification_id == notification.id
    )))
    assert {(item.owner_type, item.owner_id) for item in recipients} == {
        ("account", account.id)
    }
    assert db.scalar(select(func.count()).select_from(Delivery).where(
        Delivery.notification_id == notification.id
    )) == 0


def test_explicit_read_confirmation_erases_after_all_current_recipients(db, settings, cipher):
    account = active_account(db, settings, cipher)
    management = ManagementService(settings, cipher)
    partner, _ = management.add_partner_with_access(db, account.id, "Partner")
    activate_partner(db, settings, db.get(PartnerCredential, partner.id))
    contact_token = management.add_contact(db, account.id, "partner", partner.id, "partner@example.org")
    AccountService(settings, cipher).verify_contact(db, contact_token)
    notification = release_message(db, settings, cipher, account)
    inbox = InboxService(cipher)

    assert inbox.messages(db, "account", account.id)
    assert inbox.confirm_read(db, notification.id, "account", account.id)
    db.refresh(notification)
    assert notification.encrypted_message_payload is not None
    assert inbox.confirm_read(db, notification.id, "partner", partner.id)
    db.refresh(notification)
    assert notification.encrypted_message_payload is None


def test_inactive_partner_does_not_block_erasure(db, settings, cipher):
    account = active_account(db, settings, cipher)
    management = ManagementService(settings, cipher)
    partner, _ = management.add_partner_with_access(db, account.id, "Partner")
    activate_partner(db, settings, db.get(PartnerCredential, partner.id))
    contact_token = management.add_contact(db, account.id, "partner", partner.id, "partner@example.org")
    AccountService(settings, cipher).verify_contact(db, contact_token)
    notification = release_message(db, settings, cipher, account)
    partner.is_active = False
    db.commit()
    assert InboxService(cipher).confirm_read(db, notification.id, "account", account.id)
    db.refresh(notification)
    assert notification.encrypted_message_payload is None


def test_content_is_erased_thirty_days_after_release(db, settings, cipher):
    db.get(SystemConfiguration, "default").message_retention_days = 30
    db.commit()
    account = active_account(db, settings, cipher)
    notification = release_message(db, settings, cipher, account)
    assert notification.expires_at == notification.release_at + timedelta(days=30)
    LifecycleService(settings).run(db, notification.expires_at)
    db.refresh(notification)
    assert notification.encrypted_message_payload is None


def test_partner_rotation_and_password_change_revoke_sessions_but_keep_recipient(db, settings, cipher):
    account = active_account(db, settings, cipher)
    management = ManagementService(settings, cipher)
    partner, _ = management.add_partner_with_access(db, account.id, "Partner")
    credential = db.get(PartnerCredential, partner.id)
    activate_partner(db, settings, credential)
    contact_token = management.add_contact(db, account.id, "partner", partner.id, "partner@example.org")
    AccountService(settings, cipher).verify_contact(db, contact_token)
    notification = release_message(db, settings, cipher, account)
    manager = SessionManager(settings)
    first, _ = manager.create(db, "partner", account.id, partner_id=partner.id)
    db.commit()
    assert PartnerAuthenticationService(settings).change_password(
        db, partner.id, "partner secure password", "partner changed password"
    )
    assert manager.resolve(db, first, "partner") is None
    second, _ = manager.create(db, "partner", account.id, partner_id=partner.id)
    db.commit()
    management.rotate_partner_access(db, account.id, partner.id)
    assert manager.resolve(db, second, "partner") is None
    assert db.scalar(select(NotificationRecipient.id).where(
        NotificationRecipient.notification_id == notification.id,
        NotificationRecipient.owner_id == partner.id,
    )) is not None
    activate_partner(db, settings, db.get(PartnerCredential, partner.id), "replacement partner password")
    assert InboxService(cipher).recipient(
        db, notification.id, "partner", partner.id
    ) is not None


def test_parallel_read_confirmations_are_idempotent_on_sqlite(tmp_path, settings, cipher):
    engine = create_engine(f"sqlite:///{(tmp_path / 'read-confirm.db').as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as setup_db:
        setup_db.add(SystemConfiguration(id="default", notification_delay_minutes=0))
        setup_db.commit()
        account = active_account(setup_db, settings, cipher)
        notification = release_message(setup_db, settings, cipher, account)
        account_id = account.id
        notification_id = notification.id

    barrier = threading.Barrier(2)
    results = []

    def confirm():
        with Session(engine, expire_on_commit=False) as worker:
            barrier.wait(timeout=5)
            results.append(InboxService(cipher).confirm_read(
                worker, notification_id, "account", account_id
            ))

    threads = [threading.Thread(target=confirm) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert results == [True, True]
    with Session(engine) as check_db:
        notification = check_db.get(Notification, notification_id)
        recipient = check_db.scalar(select(NotificationRecipient).where(
            NotificationRecipient.notification_id == notification_id
        ))
        assert recipient.read_at is not None
        assert notification.encrypted_message_payload is None
    engine.dispose()
