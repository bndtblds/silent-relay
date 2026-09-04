"""Constrain polymorphic owner relationships.

Revision ID: 0016
Revises: 0015
"""

from alembic import op
import sqlalchemy as sa


revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


OWNER_CHECKS = {
    "contact_methods": "ck_contact_methods_owner_type",
    "trusted_persons": "ck_trusted_persons_owner_type",
    "notification_recipients": "ck_notification_recipients_owner_type",
}

TRIGGERS = {
    "trg_contact_methods_owner_insert": """
        CREATE TRIGGER trg_contact_methods_owner_insert
        BEFORE INSERT ON contact_methods
        FOR EACH ROW WHEN NOT (
            (NEW.owner_type = 'account' AND NEW.owner_id = NEW.account_id)
            OR (NEW.owner_type = 'partner' AND EXISTS (
                SELECT 1 FROM partners
                WHERE id = NEW.owner_id AND account_id = NEW.account_id
            ))
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid contact method owner');
        END
    """,
    "trg_contact_methods_owner_update": """
        CREATE TRIGGER trg_contact_methods_owner_update
        BEFORE UPDATE OF account_id, owner_type, owner_id ON contact_methods
        FOR EACH ROW WHEN NOT (
            (NEW.owner_type = 'account' AND NEW.owner_id = NEW.account_id)
            OR (NEW.owner_type = 'partner' AND EXISTS (
                SELECT 1 FROM partners
                WHERE id = NEW.owner_id AND account_id = NEW.account_id
            ))
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid contact method owner');
        END
    """,
    "trg_trusted_persons_owner_insert": """
        CREATE TRIGGER trg_trusted_persons_owner_insert
        BEFORE INSERT ON trusted_persons
        FOR EACH ROW WHEN NOT (
            (NEW.owner_type = 'account' AND NEW.owner_id = NEW.account_id)
            OR (NEW.owner_type = 'partner' AND EXISTS (
                SELECT 1 FROM partners
                WHERE id = NEW.owner_id AND account_id = NEW.account_id
            ))
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid trusted person owner');
        END
    """,
    "trg_trusted_persons_owner_update": """
        CREATE TRIGGER trg_trusted_persons_owner_update
        BEFORE UPDATE OF account_id, owner_type, owner_id ON trusted_persons
        FOR EACH ROW WHEN NOT (
            (NEW.owner_type = 'account' AND NEW.owner_id = NEW.account_id)
            OR (NEW.owner_type = 'partner' AND EXISTS (
                SELECT 1 FROM partners
                WHERE id = NEW.owner_id AND account_id = NEW.account_id
            ))
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid trusted person owner');
        END
    """,
    "trg_notification_recipients_owner_insert": """
        CREATE TRIGGER trg_notification_recipients_owner_insert
        BEFORE INSERT ON notification_recipients
        FOR EACH ROW WHEN NOT EXISTS (
            SELECT 1 FROM notifications AS notification
            WHERE notification.id = NEW.notification_id
              AND (
                (NEW.owner_type = 'account'
                 AND NEW.owner_id = notification.account_id)
                OR (NEW.owner_type = 'partner' AND EXISTS (
                    SELECT 1 FROM partners
                    WHERE id = NEW.owner_id
                      AND account_id = notification.account_id
                ))
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid notification recipient owner');
        END
    """,
    "trg_notification_recipients_owner_update": """
        CREATE TRIGGER trg_notification_recipients_owner_update
        BEFORE UPDATE OF notification_id, owner_type, owner_id
        ON notification_recipients
        FOR EACH ROW WHEN NOT EXISTS (
            SELECT 1 FROM notifications AS notification
            WHERE notification.id = NEW.notification_id
              AND (
                (NEW.owner_type = 'account'
                 AND NEW.owner_id = notification.account_id)
                OR (NEW.owner_type = 'partner' AND EXISTS (
                    SELECT 1 FROM partners
                    WHERE id = NEW.owner_id
                      AND account_id = notification.account_id
                ))
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid notification recipient owner');
        END
    """,
}


def _invalid_owner_counts(connection) -> dict[str, int]:
    queries = {
        "contact_methods": """
            SELECT COUNT(*) FROM contact_methods AS child
            WHERE child.owner_type NOT IN ('account', 'partner')
               OR (child.owner_type = 'account'
                   AND child.owner_id != child.account_id)
               OR (child.owner_type = 'partner' AND NOT EXISTS (
                   SELECT 1 FROM partners AS partner
                   WHERE partner.id = child.owner_id
                     AND partner.account_id = child.account_id
               ))
        """,
        "trusted_persons": """
            SELECT COUNT(*) FROM trusted_persons AS child
            WHERE child.owner_type NOT IN ('account', 'partner')
               OR (child.owner_type = 'account'
                   AND child.owner_id != child.account_id)
               OR (child.owner_type = 'partner' AND NOT EXISTS (
                   SELECT 1 FROM partners AS partner
                   WHERE partner.id = child.owner_id
                     AND partner.account_id = child.account_id
               ))
        """,
        # A missing partner is valid historical state after partner deletion.
        "notification_recipients": """
            SELECT COUNT(*) FROM notification_recipients AS recipient
            WHERE recipient.owner_type NOT IN ('account', 'partner')
               OR NOT EXISTS (
                   SELECT 1 FROM notifications AS notification
                   WHERE notification.id = recipient.notification_id
                     AND (
                       (recipient.owner_type = 'account'
                        AND recipient.owner_id = notification.account_id)
                       OR (recipient.owner_type = 'partner' AND NOT EXISTS (
                           SELECT 1 FROM partners
                           WHERE id = recipient.owner_id
                             AND account_id != notification.account_id
                       ))
                     )
               )
        """,
    }
    return {
        table: connection.execute(sa.text(query)).scalar_one()
        for table, query in queries.items()
    }


def upgrade() -> None:
    connection = op.get_bind()
    invalid = {
        table: count
        for table, count in _invalid_owner_counts(connection).items()
        if count
    }
    if invalid:
        details = ", ".join(
            f"{table}={count}" for table, count in sorted(invalid.items())
        )
        raise RuntimeError(f"Invalid owner relationships prevent migration: {details}")

    inspector = sa.inspect(connection)
    for table, constraint_name in OWNER_CHECKS.items():
        existing = {
            constraint["name"]
            for constraint in inspector.get_check_constraints(table)
        }
        if constraint_name not in existing:
            with op.batch_alter_table(table) as batch_op:
                batch_op.create_check_constraint(
                    constraint_name, "owner_type IN ('account', 'partner')"
                )

    for sql in TRIGGERS.values():
        op.execute(sql)


def downgrade() -> None:
    for trigger_name in reversed(TRIGGERS):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")

    for table, constraint_name in reversed(OWNER_CHECKS.items()):
        existing = {
            constraint["name"]
            for constraint in sa.inspect(op.get_bind()).get_check_constraints(table)
        }
        if constraint_name in existing:
            with op.batch_alter_table(table) as batch_op:
                batch_op.drop_constraint(constraint_name, type_="check")
