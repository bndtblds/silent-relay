"""Add a processing lease to review reminders.

Revision ID: 0013
Revises: 0012
"""

from alembic import op
import sqlalchemy as sa


revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {
        column["name"] for column in inspector.get_columns("review_reminders")
    }
    with op.batch_alter_table("review_reminders") as batch_op:
        if "processing_started_at" not in columns:
            batch_op.add_column(
                sa.Column("processing_started_at", sa.DateTime(), nullable=True)
            )
        if "processing_until" not in columns:
            batch_op.add_column(
                sa.Column("processing_until", sa.DateTime(), nullable=True)
            )
    inspector = sa.inspect(op.get_bind())
    indexes = {
        index["name"] for index in inspector.get_indexes("review_reminders")
    }
    if "ix_review_reminders_processing_until" not in indexes:
        op.create_index(
            "ix_review_reminders_processing_until",
            "review_reminders",
            ["processing_until"],
        )


def downgrade() -> None:
    with op.batch_alter_table("review_reminders") as batch_op:
        batch_op.drop_index("ix_review_reminders_processing_until")
        batch_op.drop_column("processing_until")
        batch_op.drop_column("processing_started_at")
