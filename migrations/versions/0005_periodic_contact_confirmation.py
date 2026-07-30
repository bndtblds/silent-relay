"""Add periodic confirmation for every contact method.

Revision ID: 0005
Revises: 0004
"""

from alembic import op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    account_columns = {
        column["name"] for column in inspector.get_columns("accounts")
    }
    if "last_contact_problem_reminder_at" not in account_columns:
        with op.batch_alter_table("accounts") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "last_contact_problem_reminder_at",
                    sa.DateTime(),
                    nullable=True,
                )
            )
    contact_columns = {
        column["name"] for column in inspector.get_columns("contact_methods")
    }
    if "last_review_expired_at" not in contact_columns:
        with op.batch_alter_table("contact_methods") as batch_op:
            batch_op.add_column(
                sa.Column("last_review_expired_at", sa.DateTime(), nullable=True)
            )
    review_columns = {
        column["name"] for column in inspector.get_columns("account_reviews")
    }
    if "details_confirmed_at" not in review_columns:
        with op.batch_alter_table("account_reviews") as batch_op:
            batch_op.add_column(
                sa.Column("details_confirmed_at", sa.DateTime(), nullable=True)
            )

    if "contact_reviews" not in inspector.get_table_names():
        op.create_table(
            "contact_reviews",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("account_review_id", sa.String(length=36), nullable=False),
            sa.Column("contact_method_id", sa.String(length=36), nullable=False),
            sa.Column("confirmation_due_at", sa.DateTime(), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(), nullable=True),
            sa.Column("last_sent_at", sa.DateTime(), nullable=True),
            sa.Column("last_reminder_day", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["account_review_id"], ["account_reviews.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["contact_method_id"], ["contact_methods.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("account_review_id", "contact_method_id"),
        )
        op.create_index(
            "ix_contact_reviews_account_review_id",
            "contact_reviews",
            ["account_review_id"],
        )
        op.create_index(
            "ix_contact_reviews_contact_method_id",
            "contact_reviews",
            ["contact_method_id"],
        )
        op.create_index(
            "ix_contact_reviews_confirmation_due_at",
            "contact_reviews",
            ["confirmation_due_at"],
        )
    inspector = sa.inspect(op.get_bind())
    if "contact_review_tokens" not in inspector.get_table_names():
        op.create_table(
            "contact_review_tokens",
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("contact_review_id", sa.String(length=36), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["contact_review_id"], ["contact_reviews.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("token_hash"),
        )
        op.create_index(
            "ix_contact_review_tokens_contact_review_id",
            "contact_review_tokens",
            ["contact_review_id"],
        )
        op.create_index(
            "ix_contact_review_tokens_expires_at",
            "contact_review_tokens",
            ["expires_at"],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_contact_review_tokens_expires_at",
        table_name="contact_review_tokens",
    )
    op.drop_index(
        "ix_contact_review_tokens_contact_review_id",
        table_name="contact_review_tokens",
    )
    op.drop_table("contact_review_tokens")
    op.drop_index(
        "ix_contact_reviews_confirmation_due_at", table_name="contact_reviews"
    )
    op.drop_index("ix_contact_reviews_contact_method_id", table_name="contact_reviews")
    op.drop_index("ix_contact_reviews_account_review_id", table_name="contact_reviews")
    op.drop_table("contact_reviews")
    with op.batch_alter_table("account_reviews") as batch_op:
        batch_op.drop_column("details_confirmed_at")
    with op.batch_alter_table("contact_methods") as batch_op:
        batch_op.drop_column("last_review_expired_at")
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_column("last_contact_problem_reminder_at")
