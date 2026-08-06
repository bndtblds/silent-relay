"""Add the cancellable notification queue.

Revision ID: 0009
Revises: 0008
"""

from alembic import op
import sqlalchemy as sa


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "system_configurations" not in inspector.get_table_names():
        op.create_table(
            "system_configurations",
            sa.Column("id", sa.String(length=16), nullable=False),
            sa.Column(
                "notification_delay_minutes", sa.Integer(),
                nullable=False, server_default="10",
            ),
            sa.Column("account_creation_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("account_pending_retention_days", sa.Integer(), nullable=False, server_default="7"),
            sa.Column("account_review_interval_days", sa.Integer(), nullable=False, server_default="180"),
            sa.Column("account_review_reminder_days", sa.String(length=128), nullable=False, server_default="-30,-15,-3,0,30"),
            sa.Column("account_review_grace_days", sa.Integer(), nullable=False, server_default="60"),
            sa.Column("contact_problem_reminder_days", sa.Integer(), nullable=False, server_default="7"),
            sa.Column("account_retention_after_disable_days", sa.Integer(), nullable=False, server_default="365"),
            sa.Column("message_retention_hours", sa.Integer(), nullable=False, server_default="48"),
            sa.Column("audit_retention_days", sa.Integer(), nullable=False, server_default="90"),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    notification_columns = {
        column["name"] for column in inspector.get_columns("notifications")
    }
    with op.batch_alter_table("notifications") as batch:
        if "release_at" not in notification_columns:
            batch.add_column(sa.Column("release_at", sa.DateTime(), nullable=True))
        if "cancelled_at" not in notification_columns:
            batch.add_column(sa.Column("cancelled_at", sa.DateTime(), nullable=True))
    if "release_at" not in notification_columns:
        op.execute("UPDATE notifications SET release_at = created_at WHERE release_at IS NULL")
        with op.batch_alter_table("notifications") as batch:
            batch.alter_column("release_at", nullable=False)
    inspector = sa.inspect(op.get_bind())
    notification_indexes = {
        index["name"] for index in inspector.get_indexes("notifications")
    }
    if "ix_notifications_release_at" not in notification_indexes:
        op.create_index("ix_notifications_release_at", "notifications", ["release_at"])


def downgrade() -> None:
    with op.batch_alter_table("notifications") as batch:
        batch.drop_index("ix_notifications_release_at")
        batch.drop_column("cancelled_at")
        batch.drop_column("release_at")
    op.drop_table("system_configurations")
