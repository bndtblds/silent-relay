"""Bind partner sessions to partners with cascading deletion.

Revision ID: 0014
Revises: 0013
"""

from alembic import op
import sqlalchemy as sa


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM server_sessions
        WHERE partner_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM partners WHERE partners.id = server_sessions.partner_id
          )
        """
    )
    partner_foreign_keys = [
        foreign_key
        for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys(
            "server_sessions"
        )
        if foreign_key["constrained_columns"] == ["partner_id"]
    ]
    if not partner_foreign_keys:
        with op.batch_alter_table("server_sessions") as batch_op:
            batch_op.create_foreign_key(
                "fk_server_sessions_partner_id",
                "partners",
                ["partner_id"],
                ["id"],
                ondelete="CASCADE",
            )


def downgrade() -> None:
    foreign_key_names = {
        foreign_key["name"]
        for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys(
            "server_sessions"
        )
    }
    if "fk_server_sessions_partner_id" in foreign_key_names:
        with op.batch_alter_table("server_sessions") as batch_op:
            batch_op.drop_constraint(
                "fk_server_sessions_partner_id", type_="foreignkey"
            )
