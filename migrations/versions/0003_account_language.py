"""Add the account language.

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("accounts")}
    if "language_code" in columns:
        return
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.add_column(
            sa.Column("language_code", sa.String(length=16), nullable=False, server_default="de")
        )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("accounts")}
    if "language_code" not in columns:
        return
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_column("language_code")
