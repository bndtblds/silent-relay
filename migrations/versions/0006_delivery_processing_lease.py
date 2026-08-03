"""Add a time-bounded delivery processing lease.

Revision ID: 0006
Revises: 0005
"""

from alembic import op
import sqlalchemy as sa


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("deliveries")}
    with op.batch_alter_table("deliveries") as batch_op:
        if "processing_started_at" not in columns:
            batch_op.add_column(sa.Column("processing_started_at", sa.DateTime(), nullable=True))
        if "processing_until" not in columns:
            batch_op.add_column(sa.Column("processing_until", sa.DateTime(), nullable=True))
    inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("deliveries")}
    if "ix_deliveries_processing_until" not in indexes:
        op.create_index("ix_deliveries_processing_until", "deliveries", ["processing_until"])

    # A processing row from an older process has no live owner after the
    # migration and is therefore immediately eligible for a controlled claim.
    op.execute(
        sa.text(
            "UPDATE deliveries SET processing_started_at = NULL, processing_until = NULL "
            "WHERE status = 'processing'"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_deliveries_processing_until", table_name="deliveries")
    with op.batch_alter_table("deliveries") as batch_op:
        batch_op.drop_column("processing_until")
        batch_op.drop_column("processing_started_at")
