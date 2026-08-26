from __future__ import annotations

import enum
from datetime import datetime, timedelta

from sqlalchemy import Boolean, Enum, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid6 import uuid7

from app.model_base import Base
from app.time import UTCDateTime, utc_now


def uuid7_str() -> str:
    return str(uuid7())


class AccountStatus(str, enum.Enum):
    pending_verification = "pending_verification"
    active = "active"
    overdue = "overdue"
    disabled = "disabled"
    scheduled_for_deletion = "scheduled_for_deletion"
    deleted = "deleted"


class NotificationStatus(str, enum.Enum):
    created = "created"
    queued = "queued"
    partially_delivered = "partially_delivered"
    delivered = "delivered"
    failed = "failed"
    discarded = "discarded"


class DeliveryStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    delivered = "delivered"
    retry_scheduled = "retry_scheduled"
    permanent_failure = "permanent_failure"
    cancelled = "cancelled"


class ReviewReminderDeliveryStatus(str, enum.Enum):
    pending = "pending"
    successful = "successful"
    permanent_failure = "permanent_failure"
    cancelled = "cancelled"


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7_str)
    encrypted_owner_name: Mapped[bytes | None] = mapped_column(LargeBinary)
    status: Mapped[AccountStatus] = mapped_column(Enum(AccountStatus), default=AccountStatus.pending_verification)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    deletion_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    next_review_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    review_grace_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_contact_problem_reminder_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    is_admin_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    language_code: Mapped[str] = mapped_column(String(16), default="de")
    credential: Mapped["AccountOwnerCredential"] = relationship(back_populates="account", cascade="all, delete-orphan")


class AccountOwnerCredential(Base):
    __tablename__ = "account_owner_credentials"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    account_owner_token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    setup_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    setup_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    password_hash: Mapped[str | None] = mapped_column(Text)
    password_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    account: Mapped[Account] = relationship(back_populates="credential")


class Partner(Base):
    __tablename__ = "partners"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7_str)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    encrypted_name: Mapped[bytes] = mapped_column(LargeBinary)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class PartnerCredential(Base):
    __tablename__ = "partner_credentials"
    partner_id: Mapped[str] = mapped_column(ForeignKey("partners.id", ondelete="CASCADE"), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(Text)
    enrollment_expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    enrolled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    password_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    rotated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    setup_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expiry_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class ContactMethod(Base):
    __tablename__ = "contact_methods"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7_str)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    owner_type: Mapped[str] = mapped_column(String(16))
    owner_id: Mapped[str] = mapped_column(String(36), index=True)
    channel: Mapped[str] = mapped_column(String(24), default="email")
    encrypted_value: Mapped[bytes] = mapped_column(LargeBinary)
    value_fingerprint: Mapped[str] = mapped_column(String(64))
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    verification_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    verification_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    permanent_failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_permanent_failure_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_review_expired_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)
    __table_args__ = (
        UniqueConstraint("account_id", "owner_type", "owner_id", "channel", "value_fingerprint"),
        Index("ix_contact_recipient", "account_id", "owner_type", "owner_id", "is_verified", "is_active"),
    )


class TrustedPerson(Base):
    __tablename__ = "trusted_persons"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7_str)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    owner_type: Mapped[str] = mapped_column(String(16))
    owner_id: Mapped[str] = mapped_column(String(36), index=True)
    encrypted_display_name: Mapped[bytes | None] = mapped_column(LargeBinary)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)
    __table_args__ = (
        Index("ix_trusted_person_owner", "account_id", "owner_type", "owner_id", "is_active"),
    )


class TrustedPersonToken(Base):
    __tablename__ = "trusted_person_tokens"
    trusted_person_id: Mapped[str] = mapped_column(ForeignKey("trusted_persons.id", ondelete="CASCADE"), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    rotated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    pin_hash: Mapped[str | None] = mapped_column(Text)
    enrollment_expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=lambda: utc_now() + timedelta(days=14), index=True
    )
    enrolled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    failed_pin_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    setup_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expiry_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7_str)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    trusted_person_id: Mapped[str | None] = mapped_column(ForeignKey("trusted_persons.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    status: Mapped[NotificationStatus] = mapped_column(Enum(NotificationStatus), default=NotificationStatus.created)
    message_digest: Mapped[str] = mapped_column(String(64))
    encrypted_message_payload: Mapped[bytes | None] = mapped_column(LargeBinary)
    release_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, index=True)
    recipients_frozen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    deduplication_key: Mapped[str] = mapped_column(String(64), unique=True)


class NotificationRecipient(Base):
    __tablename__ = "notification_recipients"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7_str)
    notification_id: Mapped[str] = mapped_column(
        ForeignKey("notifications.id", ondelete="CASCADE"), index=True
    )
    owner_type: Mapped[str] = mapped_column(String(16))
    owner_id: Mapped[str] = mapped_column(String(36), index=True)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    __table_args__ = (
        UniqueConstraint("notification_id", "owner_type", "owner_id"),
        Index("ix_notification_recipient_lookup", "owner_type", "owner_id", "notification_id"),
    )


class Delivery(Base):
    __tablename__ = "deliveries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7_str)
    notification_id: Mapped[str] = mapped_column(ForeignKey("notifications.id", ondelete="CASCADE"), index=True)
    contact_method_id: Mapped[str | None] = mapped_column(ForeignKey("contact_methods.id", ondelete="SET NULL"))
    provider: Mapped[str] = mapped_column(String(24))
    status: Mapped[DeliveryStatus] = mapped_column(Enum(DeliveryStatus), default=DeliveryStatus.pending)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    next_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    processing_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    processing_until: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    encrypted_error_detail: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    __table_args__ = (UniqueConstraint("notification_id", "contact_method_id"),)


class EmailDeliveryTracking(Base):
    __tablename__ = "email_delivery_tracking"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    delivery_id: Mapped[str | None] = mapped_column(
        ForeignKey("deliveries.id", ondelete="SET NULL"), index=True
    )
    contact_method_id: Mapped[str | None] = mapped_column(
        ForeignKey("contact_methods.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    last_reported_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    result: Mapped[str] = mapped_column(String(16), default="pending")
    status_code: Mapped[str | None] = mapped_column(String(32))


class AccountReview(Base):
    __tablename__ = "account_reviews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7_str)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    review_due_at: Mapped[datetime] = mapped_column(UTCDateTime)
    details_confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class ReviewReminder(Base):
    __tablename__ = "review_reminders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7_str)
    account_review_id: Mapped[str] = mapped_column(ForeignKey("account_reviews.id", ondelete="CASCADE"), index=True)
    relative_day: Mapped[int] = mapped_column(Integer)
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    processing_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    processing_until: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    __table_args__ = (UniqueConstraint("account_review_id", "relative_day"),)


class ReviewReminderDelivery(Base):
    __tablename__ = "review_reminder_deliveries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7_str)
    review_reminder_id: Mapped[str] = mapped_column(
        ForeignKey("review_reminders.id", ondelete="CASCADE"), index=True
    )
    contact_method_id: Mapped[str] = mapped_column(
        ForeignKey("contact_methods.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[ReviewReminderDeliveryStatus] = mapped_column(
        Enum(ReviewReminderDeliveryStatus),
        default=ReviewReminderDeliveryStatus.pending,
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    __table_args__ = (
        UniqueConstraint("review_reminder_id", "contact_method_id"),
    )


class ContactReview(Base):
    __tablename__ = "contact_reviews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7_str)
    account_review_id: Mapped[str] = mapped_column(
        ForeignKey("account_reviews.id", ondelete="CASCADE"), index=True
    )
    contact_method_id: Mapped[str] = mapped_column(
        ForeignKey("contact_methods.id", ondelete="CASCADE"), index=True
    )
    confirmation_due_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_reminder_day: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    __table_args__ = (
        UniqueConstraint("account_review_id", "contact_method_id"),
    )


class ContactReviewToken(Base):
    __tablename__ = "contact_review_tokens"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    contact_review_id: Mapped[str] = mapped_column(
        ForeignKey("contact_reviews.id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class ServerSession(Base):
    __tablename__ = "server_sessions"
    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    trusted_person_id: Mapped[str | None] = mapped_column(
        ForeignKey("trusted_persons.id", ondelete="CASCADE"), index=True
    )
    partner_id: Mapped[str | None] = mapped_column(
        String(36), index=True
    )
    csrf_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class Submission(Base):
    __tablename__ = "submissions"
    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    trusted_person_id: Mapped[str] = mapped_column(ForeignKey("trusted_persons.id", ondelete="CASCADE"))
    encrypted_message: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7_str)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    technical_metadata: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, index=True)
    request_id: Mapped[str | None] = mapped_column(String(36))


class SmtpConfiguration(Base):
    __tablename__ = "smtp_configurations"
    id: Mapped[str] = mapped_column(String(16), primary_key=True, default="default")
    encrypted_host: Mapped[bytes] = mapped_column(LargeBinary)
    port: Mapped[int] = mapped_column(Integer)
    encrypted_username: Mapped[bytes | None] = mapped_column(LargeBinary)
    encrypted_password: Mapped[bytes | None] = mapped_column(LargeBinary)
    starttls: Mapped[bool] = mapped_column(Boolean, default=True)
    encrypted_from_address: Mapped[bytes] = mapped_column(LargeBinary)
    encrypted_from_name: Mapped[bytes] = mapped_column(LargeBinary)
    ndr_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    encrypted_imap_host: Mapped[bytes | None] = mapped_column(LargeBinary)
    imap_port: Mapped[int | None] = mapped_column(Integer)
    encrypted_imap_username: Mapped[bytes | None] = mapped_column(LargeBinary)
    encrypted_imap_password: Mapped[bytes | None] = mapped_column(LargeBinary)
    ndr_acknowledged_address_fingerprint: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class SystemConfiguration(Base):
    __tablename__ = "system_configurations"
    id: Mapped[str] = mapped_column(String(16), primary_key=True, default="default")
    notification_delay_minutes: Mapped[int] = mapped_column(Integer, default=10)
    account_creation_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    account_pending_retention_days: Mapped[int] = mapped_column(Integer, default=7)
    account_review_interval_days: Mapped[int] = mapped_column(Integer, default=180)
    account_review_reminder_days: Mapped[str] = mapped_column(String(128), default="-30,-15,-3,0,30")
    account_review_grace_days: Mapped[int] = mapped_column(Integer, default=60)
    contact_problem_reminder_days: Mapped[int] = mapped_column(Integer, default=7)
    account_retention_after_disable_days: Mapped[int] = mapped_column(Integer, default=365)
    message_retention_days: Mapped[int] = mapped_column(Integer, default=30)
    audit_retention_days: Mapped[int] = mapped_column(Integer, default=90)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class PublicSiteContent(Base):
    __tablename__ = "public_site_contents"
    language_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    imprint_text: Mapped[str] = mapped_column(Text)
    privacy_text: Mapped[str] = mapped_column(Text)
    contact_email: Mapped[str] = mapped_column(String(320))
    contact_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class RateLimitBucket(Base):
    __tablename__ = "rate_limit_buckets"
    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    action: Mapped[str] = mapped_column(String(48), index=True)
    request_count: Mapped[int] = mapped_column(Integer)
    window_started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
