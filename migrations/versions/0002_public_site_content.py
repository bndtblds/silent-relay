"""Add configurable public site content.

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "public_site_contents",
        sa.Column("language_code", sa.String(length=16), nullable=False),
        sa.Column("imprint_text", sa.Text(), nullable=False),
        sa.Column("privacy_text", sa.Text(), nullable=False),
        sa.Column("contact_email", sa.String(length=320), nullable=False),
        sa.Column("contact_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("language_code"),
    )


def downgrade() -> None:
    op.drop_table("public_site_contents")
