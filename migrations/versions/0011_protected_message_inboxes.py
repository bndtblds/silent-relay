"""Add protected message inboxes and personal partner access.

Revision ID: 0011
Revises: 0010
"""

from alembic import op
import sqlalchemy as sa


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    account_columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("accounts")
    }
    if "encrypted_owner_name" not in account_columns:
        op.add_column("accounts", sa.Column("encrypted_owner_name", sa.LargeBinary(), nullable=True))
    with op.batch_alter_table("system_configurations") as batch:
        batch.alter_column(
            "message_retention_hours",
            new_column_name="message_retention_days",
            existing_type=sa.Integer(),
            server_default="30",
        )
    op.execute(sa.text(
        "UPDATE system_configurations SET message_retention_days = 30"
    ))
    notification_columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("notifications")
    }
    if "recipients_frozen_at" not in notification_columns:
        op.add_column("notifications", sa.Column("recipients_frozen_at", sa.DateTime(), nullable=True))
    op.create_table(
        "partner_credentials",
        sa.Column("partner_id", sa.String(36), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("enrollment_expires_at", sa.DateTime(), nullable=False),
        sa.Column("enrolled_at", sa.DateTime(), nullable=True),
        sa.Column("password_changed_at", sa.DateTime(), nullable=True),
        sa.Column("rotated_at", sa.DateTime(), nullable=True),
        sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(), nullable=True),
        sa.Column("setup_notified_at", sa.DateTime(), nullable=True),
        sa.Column("expiry_notified_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["partner_id"], ["partners.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("partner_id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_partner_credentials_token_hash", "partner_credentials", ["token_hash"], unique=True)
    op.create_index("ix_partner_credentials_enrollment_expires_at", "partner_credentials", ["enrollment_expires_at"])
    inspector = sa.inspect(op.get_bind())
    session_columns = {column["name"] for column in inspector.get_columns("server_sessions")}
    if "partner_id" not in session_columns:
        op.add_column("server_sessions", sa.Column("partner_id", sa.String(36), nullable=True))
    session_indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("server_sessions")}
    if "ix_server_sessions_partner_id" not in session_indexes:
        op.create_index("ix_server_sessions_partner_id", "server_sessions", ["partner_id"])
    op.create_table(
        "notification_recipients",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("notification_id", sa.String(36), nullable=False),
        sa.Column("owner_type", sa.String(16), nullable=False),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["notification_id"], ["notifications.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("notification_id", "owner_type", "owner_id"),
    )
    op.create_index("ix_notification_recipients_notification_id", "notification_recipients", ["notification_id"])
    op.create_index("ix_notification_recipients_owner_id", "notification_recipients", ["owner_id"])
    op.create_index("ix_notification_recipient_lookup", "notification_recipients", ["owner_type", "owner_id", "notification_id"])
    _backfill_recipients()
    op.execute(sa.text("""
        UPDATE notifications
        SET recipients_frozen_at = release_at
        WHERE encrypted_message_payload IS NOT NULL
          AND cancelled_at IS NULL
          AND release_at <= CURRENT_TIMESTAMP
    """))


def _backfill_recipients() -> None:
    connection = op.get_bind()
    rows = connection.execute(sa.text("""
        SELECT DISTINCT d.notification_id, c.owner_type, c.owner_id, n.created_at
        FROM deliveries d
        JOIN notifications n ON n.id = d.notification_id
        JOIN contact_methods c ON c.id = d.contact_method_id
        WHERE n.encrypted_message_payload IS NOT NULL
          AND n.cancelled_at IS NULL
          AND n.release_at <= CURRENT_TIMESTAMP
          AND (n.expires_at IS NULL OR n.expires_at > CURRENT_TIMESTAMP)
          AND n.status NOT IN ('discarded', 'failed')
    """)).mappings()
    for row in rows:
        connection.execute(sa.text("""
            INSERT OR IGNORE INTO notification_recipients
                (id, notification_id, owner_type, owner_id, read_at, created_at)
            VALUES (lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-7' ||
                    substr(lower(hex(randomblob(2))), 2) || '-a' || substr(lower(hex(randomblob(2))), 2) || '-' ||
                    lower(hex(randomblob(6))), :notification_id, :owner_type, :owner_id, NULL, :created_at)
        """), row)


def downgrade() -> None:
    op.drop_index("ix_notification_recipient_lookup", table_name="notification_recipients")
    op.drop_index("ix_notification_recipients_owner_id", table_name="notification_recipients")
    op.drop_index("ix_notification_recipients_notification_id", table_name="notification_recipients")
    op.drop_table("notification_recipients")
    op.drop_index("ix_server_sessions_partner_id", table_name="server_sessions")
    with op.batch_alter_table("server_sessions") as batch:
        batch.drop_column("partner_id")
    op.drop_index("ix_partner_credentials_enrollment_expires_at", table_name="partner_credentials")
    op.drop_index("ix_partner_credentials_token_hash", table_name="partner_credentials")
    op.drop_table("partner_credentials")
    with op.batch_alter_table("notifications") as batch:
        batch.drop_column("recipients_frozen_at")
    with op.batch_alter_table("accounts") as batch:
        batch.drop_column("encrypted_owner_name")
    with op.batch_alter_table("system_configurations") as batch:
        batch.alter_column(
            "message_retention_days",
            new_column_name="message_retention_hours",
            existing_type=sa.Integer(),
            server_default="48",
        )
    op.execute(sa.text(
        "UPDATE system_configurations SET message_retention_hours = 48"
    ))
