"""Persist review-reminder delivery state per recipient.

Revision ID: 0012
Revises: 0011
"""

from alembic import op
import sqlalchemy as sa


revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_reminder_deliveries",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("review_reminder_id", sa.String(36), nullable=False),
        sa.Column("contact_method_id", sa.String(36), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "successful",
                "permanent_failure",
                "cancelled",
                name="reviewreminderdeliverystatus",
            ),
            nullable=False,
        ),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["review_reminder_id"], ["review_reminders.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["contact_method_id"], ["contact_methods.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("review_reminder_id", "contact_method_id"),
    )
    op.create_index(
        "ix_review_reminder_deliveries_review_reminder_id",
        "review_reminder_deliveries",
        ["review_reminder_id"],
    )
    op.create_index(
        "ix_review_reminder_deliveries_contact_method_id",
        "review_reminder_deliveries",
        ["contact_method_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_review_reminder_deliveries_contact_method_id",
        table_name="review_reminder_deliveries",
    )
    op.drop_index(
        "ix_review_reminder_deliveries_review_reminder_id",
        table_name="review_reminder_deliveries",
    )
    op.drop_table("review_reminder_deliveries")
