"""Remove the persisted message digest.

Revision ID: 0015
Revises: 0014
"""

from alembic import op
import sqlalchemy as sa


revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    notification_columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("notifications")
    }
    if "message_digest" in notification_columns:
        with op.batch_alter_table("notifications") as batch_op:
            batch_op.drop_column("message_digest")


def downgrade() -> None:
    notification_columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("notifications")
    }
    if "message_digest" not in notification_columns:
        with op.batch_alter_table("notifications") as batch_op:
            batch_op.add_column(
                sa.Column("message_digest", sa.String(64), nullable=True)
            )
