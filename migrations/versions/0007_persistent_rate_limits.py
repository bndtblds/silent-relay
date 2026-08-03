"""Add privacy-preserving persistent rate-limit buckets.

Revision ID: 0007
Revises: 0006
"""

from alembic import op
import sqlalchemy as sa


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_buckets",
        sa.Column("id_hash", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id_hash"),
    )
    op.create_index(
        "ix_rate_limit_buckets_action", "rate_limit_buckets", ["action"]
    )
    op.create_index(
        "ix_rate_limit_buckets_expires_at", "rate_limit_buckets", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_rate_limit_buckets_expires_at", table_name="rate_limit_buckets")
    op.drop_index("ix_rate_limit_buckets_action", table_name="rate_limit_buckets")
    op.drop_table("rate_limit_buckets")
