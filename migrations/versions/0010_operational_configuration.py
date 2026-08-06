"""Store operational configuration in the database.

Revision ID: 0010
Revises: 0009
"""

from alembic import op
import sqlalchemy as sa


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


COLUMNS = (
    sa.Column(
        "account_creation_enabled",
        sa.Boolean(),
        nullable=False,
        server_default=sa.true(),
    ),
    sa.Column(
        "account_pending_retention_days",
        sa.Integer(),
        nullable=False,
        server_default="7",
    ),
    sa.Column(
        "account_review_interval_days",
        sa.Integer(),
        nullable=False,
        server_default="180",
    ),
    sa.Column(
        "account_review_reminder_days",
        sa.String(length=128),
        nullable=False,
        server_default="-30,-15,-3,0,30",
    ),
    sa.Column(
        "account_review_grace_days",
        sa.Integer(),
        nullable=False,
        server_default="60",
    ),
    sa.Column(
        "contact_problem_reminder_days",
        sa.Integer(),
        nullable=False,
        server_default="7",
    ),
    sa.Column(
        "account_retention_after_disable_days",
        sa.Integer(),
        nullable=False,
        server_default="365",
    ),
    sa.Column(
        "message_retention_hours",
        sa.Integer(),
        nullable=False,
        server_default="48",
    ),
    sa.Column(
        "audit_retention_days",
        sa.Integer(),
        nullable=False,
        server_default="90",
    ),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {
        column["name"]
        for column in inspector.get_columns("system_configurations")
    }
    with op.batch_alter_table("system_configurations") as batch:
        for column in COLUMNS:
            if column.name not in existing:
                batch.add_column(column)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {
        column["name"]
        for column in inspector.get_columns("system_configurations")
    }
    with op.batch_alter_table("system_configurations") as batch:
        for column in reversed(COLUMNS):
            if column.name in existing:
                batch.drop_column(column.name)
