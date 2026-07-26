"""Initial schema.

Revision ID: 0001
"""
from alembic import op

from app.model_base import Base
from app import models  # noqa: F401

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

INITIAL_TABLE_NAMES = (
    "accounts",
    "account_owner_credentials",
    "partners",
    "contact_methods",
    "trusted_persons",
    "trusted_person_tokens",
    "notifications",
    "deliveries",
    "account_reviews",
    "review_reminders",
    "server_sessions",
    "submissions",
    "audit_logs",
    "smtp_configurations",
)


def upgrade() -> None:
    tables = [Base.metadata.tables[name] for name in INITIAL_TABLE_NAMES]
    Base.metadata.create_all(bind=op.get_bind(), tables=tables)


def downgrade() -> None:
    tables = [Base.metadata.tables[name] for name in INITIAL_TABLE_NAMES]
    Base.metadata.drop_all(bind=op.get_bind(), tables=tables)
