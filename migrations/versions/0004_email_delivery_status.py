"""Add email delivery-status processing.

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    contact_columns = {
        column["name"] for column in inspector.get_columns("contact_methods")
    }
    if "permanent_failure_count" not in contact_columns:
        with op.batch_alter_table("contact_methods") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "permanent_failure_count",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                )
            )
            batch_op.add_column(
                sa.Column("last_permanent_failure_at", sa.DateTime(), nullable=True)
            )

    smtp_columns = {
        column["name"] for column in inspector.get_columns("smtp_configurations")
    }
    if "ndr_enabled" not in smtp_columns:
        with op.batch_alter_table("smtp_configurations") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "ndr_enabled",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )
            batch_op.add_column(
                sa.Column("encrypted_imap_host", sa.LargeBinary(), nullable=True)
            )
            batch_op.add_column(sa.Column("imap_port", sa.Integer(), nullable=True))
            batch_op.add_column(
                sa.Column("encrypted_imap_username", sa.LargeBinary(), nullable=True)
            )
            batch_op.add_column(
                sa.Column("encrypted_imap_password", sa.LargeBinary(), nullable=True)
            )
            batch_op.add_column(
                sa.Column(
                    "ndr_acknowledged_address_fingerprint",
                    sa.String(length=64),
                    nullable=True,
                )
            )

    if "email_delivery_tracking" not in inspector.get_table_names():
        op.create_table(
            "email_delivery_tracking",
            sa.Column("token_hash", sa.String(length=64), primary_key=True),
            sa.Column(
                "delivery_id",
                sa.String(length=36),
                sa.ForeignKey("deliveries.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "contact_method_id",
                sa.String(length=36),
                sa.ForeignKey("contact_methods.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("last_reported_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column(
                "result", sa.String(length=16), nullable=False, server_default="pending"
            ),
            sa.Column("status_code", sa.String(length=32), nullable=True),
        )
        op.create_index(
            "ix_email_delivery_tracking_delivery_id",
            "email_delivery_tracking",
            ["delivery_id"],
        )
        op.create_index(
            "ix_email_delivery_tracking_contact_method_id",
            "email_delivery_tracking",
            ["contact_method_id"],
        )
        op.create_index(
            "ix_email_delivery_tracking_expires_at",
            "email_delivery_tracking",
            ["expires_at"],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_email_delivery_tracking_expires_at", table_name="email_delivery_tracking"
    )
    op.drop_index(
        "ix_email_delivery_tracking_contact_method_id",
        table_name="email_delivery_tracking",
    )
    op.drop_index(
        "ix_email_delivery_tracking_delivery_id", table_name="email_delivery_tracking"
    )
    op.drop_table("email_delivery_tracking")

    with op.batch_alter_table("smtp_configurations") as batch_op:
        batch_op.drop_column("ndr_acknowledged_address_fingerprint")
        batch_op.drop_column("encrypted_imap_password")
        batch_op.drop_column("encrypted_imap_username")
        batch_op.drop_column("imap_port")
        batch_op.drop_column("encrypted_imap_host")
        batch_op.drop_column("ndr_enabled")

    with op.batch_alter_table("contact_methods") as batch_op:
        batch_op.drop_column("last_permanent_failure_at")
        batch_op.drop_column("permanent_failure_count")
