from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_, select

from app.config import Settings
from app.database import SessionLocal
from app.email_tracking import NdrMailboxProcessor, send_tracked_email
from app.i18n import email_body, format_date, translate
from app.models import (
    Account, AccountReview, ContactMethod, ContactReview, ContactReviewToken,
    Partner, ReviewReminder, TrustedPerson, TrustedPersonToken,
)
from app.rate_limit import purge_expired_rate_limits
from app.security.core import FieldCipher, SessionManager, generate_token, keyed_hash
from app.services import DeliveryService, LifecycleService
from app.smtp_config import load_email_provider
from app.system_config import system_configuration


def run_jobs(settings: Settings) -> dict[str, int]:
    cipher = FieldCipher(settings.field_encryption_key)
    with SessionLocal() as db:
        try:
            ndr_reports = NdrMailboxProcessor(settings, cipher).process(db)
        except Exception:
            db.rollback()
            ndr_reports = 0
        provider = load_email_provider(db, settings, cipher)
        system_config = system_configuration(db)
        deliveries = DeliveryService(
            settings, cipher, {"email": provider}
        ).process_due(db)
        now = datetime.utcnow()
        reminders = 0
        due = list(db.scalars(select(ReviewReminder).where(
            ReviewReminder.sent_at.is_(None),
            ReviewReminder.scheduled_at <= now,
        )))
        for reminder in due:
            review = db.get(AccountReview, reminder.account_review_id)
            if not review or review.confirmed_at:
                reminder.sent_at = now
                db.commit()
                continue
            account = db.get(Account, review.account_id)
            if not account:
                reminder.sent_at = now
                db.commit()
                continue
            language = account.language_code

            confirmations_sent = True
            if reminder.relative_day >= 0:
                for contact_review, contact in _ensure_contact_reviews(
                    db, review, account
                ):
                    if (
                        contact_review.confirmed_at
                        or contact_review.last_reminder_day == reminder.relative_day
                    ):
                        continue
                    token = generate_token()
                    db.add(ContactReviewToken(
                        token_hash=keyed_hash(token, settings.token_hmac_key),
                        contact_review_id=contact_review.id,
                        expires_at=contact_review.confirmation_due_at,
                    ))
                    result = send_tracked_email(
                        db,
                        settings,
                        cipher,
                        provider,
                        cipher.decrypt(contact.encrypted_value),
                        translate(language, "email.contact_review_subject"),
                        email_body(
                            language,
                            "email.contact_review_body",
                            url=(
                                f"{settings.app_base_url}/verify-contact/{token}"
                            ),
                            due=format_date(
                                contact_review.confirmation_due_at, language
                            ),
                        ),
                        contact_method_id=contact.id,
                    )
                    if result.successful or result.permanent_failure:
                        contact_review.last_sent_at = now
                        contact_review.last_reminder_day = reminder.relative_day
                    else:
                        confirmations_sent = False

            owner_contacts = list(db.scalars(select(ContactMethod).where(
                ContactMethod.account_id == review.account_id,
                ContactMethod.owner_type == "account",
                ContactMethod.is_verified.is_(True),
                ContactMethod.is_active.is_(True),
            )))
            owner_reminders_sent = bool(owner_contacts)
            for contact in owner_contacts:
                result = send_tracked_email(
                    db,
                    settings,
                    cipher,
                    provider,
                    cipher.decrypt(contact.encrypted_value),
                    translate(language, "email.review_subject"),
                    email_body(
                        language,
                        "email.review_body",
                        due=format_date(review.review_due_at, language),
                        url=settings.app_base_url,
                    ),
                    contact_method_id=contact.id,
                )
                owner_reminders_sent = owner_reminders_sent and (
                    result.successful or result.permanent_failure
                )
            if owner_reminders_sent and confirmations_sent:
                reminder.sent_at = now
                reminders += 1
            db.commit()

        contact_problem_reminders = _send_contact_problem_reminders(
            db, settings, cipher, provider, now,
            system_config.contact_problem_reminder_days,
        )
        trusted_access_notices = _send_trusted_access_notices(
            db, settings, cipher, provider, now
        )
        LifecycleService(settings).run(db, now)
        sessions = SessionManager(settings).purge_expired(db)
        rate_limit_buckets = purge_expired_rate_limits(db, now)
        db.commit()
    return {
        "ndr_reports": ndr_reports,
        "deliveries": deliveries,
        "reminders": reminders,
        "contact_problem_reminders": contact_problem_reminders,
        "trusted_access_notices": trusted_access_notices,
        "sessions": sessions,
        "rate_limit_buckets": rate_limit_buckets,
    }


def _ensure_contact_reviews(
    db, review: AccountReview, account: Account
) -> list[tuple[ContactReview, ContactMethod]]:
    contacts = list(db.scalars(select(ContactMethod).where(
        ContactMethod.account_id == account.id,
        ContactMethod.is_active.is_(True),
        ContactMethod.is_verified.is_(True),
    )))
    existing = {
        item.contact_method_id: item
        for item in db.scalars(select(ContactReview).where(
            ContactReview.account_review_id == review.id
        ))
    }
    due = account.review_grace_due_at or review.review_due_at
    for contact in contacts:
        if contact.id not in existing:
            item = ContactReview(
                account_review_id=review.id,
                contact_method_id=contact.id,
                confirmation_due_at=due,
            )
            db.add(item)
            existing[contact.id] = item
    db.flush()
    return [(existing[contact.id], contact) for contact in contacts]


def _send_contact_problem_reminders(
    db, settings: Settings, cipher: FieldCipher, provider, now: datetime,
    reminder_interval_days: int,
) -> int:
    threshold = now - timedelta(days=reminder_interval_days)
    accounts = list(db.scalars(select(Account).where(or_(
        Account.last_contact_problem_reminder_at.is_(None),
        Account.last_contact_problem_reminder_at <= threshold,
    ))))
    sent = 0
    for account in accounts:
        problems = list(db.scalars(select(ContactMethod).where(
            ContactMethod.account_id == account.id,
            ContactMethod.is_active.is_(True),
            ContactMethod.is_verified.is_(False),
            or_(
                ContactMethod.last_permanent_failure_at.is_not(None),
                ContactMethod.last_review_expired_at.is_not(None),
            ),
        )))
        if not problems:
            account.last_contact_problem_reminder_at = None
            continue
        owner_contacts = list(db.scalars(select(ContactMethod).where(
            ContactMethod.account_id == account.id,
            ContactMethod.owner_type == "account",
            ContactMethod.is_active.is_(True),
            ContactMethod.is_verified.is_(True),
        )))
        if not owner_contacts:
            continue
        problem_lines = []
        for contact in problems:
            if contact.owner_type == "account":
                owner = translate(
                    account.language_code,
                    "email.contact_problem_account_owner",
                )
            else:
                partner = db.get(Partner, contact.owner_id)
                owner = (
                    cipher.decrypt(partner.encrypted_name)
                    if partner
                    else translate(
                        account.language_code,
                        "email.contact_problem_partner",
                    )
                )
            problem_lines.append(
                f"- {owner}: {cipher.decrypt(contact.encrypted_value)}"
            )
        any_success = False
        for contact in owner_contacts:
            result = send_tracked_email(
                db,
                settings,
                cipher,
                provider,
                cipher.decrypt(contact.encrypted_value),
                translate(
                    account.language_code, "email.contact_problem_subject"
                ),
                email_body(
                    account.language_code,
                    "email.contact_problem_body",
                    problems="\n".join(problem_lines),
                    url=settings.app_base_url,
                ),
                contact_method_id=contact.id,
            )
            any_success = any_success or result.successful
        if any_success:
            account.last_contact_problem_reminder_at = now
            sent += 1
        db.commit()
    return sent


def _send_trusted_access_notices(
    db, settings: Settings, cipher: FieldCipher, provider, now: datetime
) -> int:
    records = list(db.scalars(select(TrustedPersonToken).where(or_(
        (
            TrustedPersonToken.enrolled_at.is_not(None)
            & TrustedPersonToken.setup_notified_at.is_(None)
        ),
        (
            TrustedPersonToken.enrolled_at.is_(None)
            & TrustedPersonToken.expiry_notified_at.is_(None)
            & (TrustedPersonToken.enrollment_expires_at <= now)
        ),
    ))))
    sent = 0
    for record in records:
        person = db.get(TrustedPerson, record.trusted_person_id)
        account = db.get(Account, person.account_id) if person else None
        if not person or not account:
            continue
        owner_contacts = list(db.scalars(select(ContactMethod).where(
            ContactMethod.account_id == account.id,
            ContactMethod.owner_type == "account",
            ContactMethod.is_active.is_(True),
            ContactMethod.is_verified.is_(True),
        )))
        if not owner_contacts:
            continue
        setup_complete = record.enrolled_at is not None
        subject_key = (
            "email.trusted_setup_subject" if setup_complete
            else "email.trusted_expired_subject"
        )
        body_key = (
            "email.trusted_setup_body" if setup_complete
            else "email.trusted_expired_body"
        )
        handled = False
        for contact in owner_contacts:
            result = send_tracked_email(
                db, settings, cipher, provider,
                cipher.decrypt(contact.encrypted_value),
                translate(account.language_code, subject_key),
                email_body(
                    account.language_code, body_key,
                    url=settings.app_base_url,
                ),
                contact_method_id=contact.id,
            )
            handled = handled or result.successful or result.permanent_failure
        if handled:
            if setup_complete:
                record.setup_notified_at = now
            else:
                record.expiry_notified_at = now
            sent += 1
        db.commit()
    return sent
