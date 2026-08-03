"""Require a PIN for trusted-person access.

Revision ID: 0008
Revises: 0007
"""

from alembic import op
import sqlalchemy as sa


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    token_columns = {
        column["name"]
        for column in inspector.get_columns("trusted_person_tokens")
    }
    with op.batch_alter_table("trusted_person_tokens") as batch:
        if "pin_hash" not in token_columns:
            batch.add_column(sa.Column("pin_hash", sa.Text(), nullable=True))
        if "enrollment_expires_at" not in token_columns:
            batch.add_column(sa.Column("enrollment_expires_at", sa.DateTime(), nullable=True))
        if "enrolled_at" not in token_columns:
            batch.add_column(sa.Column("enrolled_at", sa.DateTime(), nullable=True))
        if "failed_pin_attempts" not in token_columns:
            batch.add_column(sa.Column("failed_pin_attempts", sa.Integer(), nullable=False, server_default="0"))
        if "locked_until" not in token_columns:
            batch.add_column(sa.Column("locked_until", sa.DateTime(), nullable=True))
        if "setup_notified_at" not in token_columns:
            batch.add_column(sa.Column("setup_notified_at", sa.DateTime(), nullable=True))
        if "expiry_notified_at" not in token_columns:
            batch.add_column(sa.Column("expiry_notified_at", sa.DateTime(), nullable=True))
    if "enrollment_expires_at" not in token_columns:
        op.execute(
            "UPDATE trusted_person_tokens "
            "SET enrollment_expires_at = datetime('now', '+14 days')"
        )
        with op.batch_alter_table("trusted_person_tokens") as batch:
            batch.alter_column("enrollment_expires_at", nullable=False)
    inspector = sa.inspect(op.get_bind())
    token_indexes = {
        index["name"] for index in inspector.get_indexes("trusted_person_tokens")
    }
    if "ix_trusted_person_tokens_enrollment_expires_at" not in token_indexes:
        op.create_index(
            "ix_trusted_person_tokens_enrollment_expires_at",
            "trusted_person_tokens", ["enrollment_expires_at"],
        )

    session_columns = {
        column["name"] for column in inspector.get_columns("server_sessions")
    }
    with op.batch_alter_table("server_sessions") as batch:
        if "trusted_person_id" not in session_columns:
            batch.add_column(sa.Column(
                "trusted_person_id", sa.String(length=36), nullable=True
            ))
            batch.create_foreign_key(
                "fk_server_sessions_trusted_person_id",
                "trusted_persons",
                ["trusted_person_id"],
                ["id"],
                ondelete="CASCADE",
            )
    inspector = sa.inspect(op.get_bind())
    session_indexes = {
        index["name"] for index in inspector.get_indexes("server_sessions")
    }
    if "ix_server_sessions_trusted_person_id" not in session_indexes:
        op.create_index(
            "ix_server_sessions_trusted_person_id", "server_sessions",
            ["trusted_person_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("server_sessions") as batch:
        batch.drop_index("ix_server_sessions_trusted_person_id")
        batch.drop_constraint(
            "fk_server_sessions_trusted_person_id", type_="foreignkey"
        )
        batch.drop_column("trusted_person_id")
    with op.batch_alter_table("trusted_person_tokens") as batch:
        batch.drop_index("ix_trusted_person_tokens_enrollment_expires_at")
        batch.drop_column("expiry_notified_at")
        batch.drop_column("setup_notified_at")
        batch.drop_column("locked_until")
        batch.drop_column("failed_pin_attempts")
        batch.drop_column("enrolled_at")
        batch.drop_column("enrollment_expires_at")
        batch.drop_column("pin_hash")
