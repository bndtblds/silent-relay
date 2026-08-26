from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from alembic import command
from alembic.config import Config

from app.config import get_settings
from app.time import utc_now


def _upgrade(database_path, revision: str, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), revision)
    finally:
        get_settings.cache_clear()


def test_fresh_database_upgrades_to_current_schema(tmp_path, monkeypatch):
    database_path = tmp_path / "fresh.db"
    _upgrade(database_path, "head", monkeypatch)

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        account_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(accounts)")
        }
        contact_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(contact_methods)")
        }
        delivery_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(deliveries)")
            }
        trusted_token_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(trusted_person_tokens)"
            )
        }
        session_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(server_sessions)")
        }
        partner_credential_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(partner_credentials)")
        }
        notification_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(notifications)")
        }
        system_configuration_columns = {
            row[1]: row[4]
            for row in connection.execute("PRAGMA table_info(system_configurations)")
        }
        reminder_delivery_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(review_reminder_deliveries)"
            )
        }
        reminder_delivery_indexes = list(connection.execute(
            "PRAGMA index_list(review_reminder_deliveries)"
        ))
    assert "public_site_contents" in tables
    assert "email_delivery_tracking" in tables
    assert "language_code" in account_columns
    assert "encrypted_owner_name" in account_columns
    assert "permanent_failure_count" in contact_columns
    assert "contact_reviews" in tables
    assert "contact_review_tokens" in tables
    assert "rate_limit_buckets" in tables
    assert "last_contact_problem_reminder_at" in account_columns
    assert "last_review_expired_at" in contact_columns
    assert "processing_started_at" in delivery_columns
    assert "processing_until" in delivery_columns
    assert "pin_hash" in trusted_token_columns
    assert "enrollment_expires_at" in trusted_token_columns
    assert "trusted_person_id" in session_columns
    assert "partner_id" in session_columns
    assert "partner_credentials" in tables
    assert "notification_recipients" in tables
    assert "review_reminder_deliveries" in tables
    assert {
        "review_reminder_id", "contact_method_id", "status", "last_attempt_at"
    } <= reminder_delivery_columns
    assert any(index[2] == 1 for index in reminder_delivery_indexes)
    assert {"token_hash", "password_hash", "enrollment_expires_at"} <= partner_credential_columns
    assert "system_configurations" in tables
    assert "release_at" in notification_columns
    assert "cancelled_at" in notification_columns
    assert "recipients_frozen_at" in notification_columns
    assert system_configuration_columns["notification_delay_minutes"] == "'10'"
    assert system_configuration_columns["account_creation_enabled"] == "1"
    assert system_configuration_columns["account_review_interval_days"] == "'180'"
    assert "message_retention_hours" not in system_configuration_columns
    assert system_configuration_columns["message_retention_days"] == "'30'"
    assert revision == "0012"


def test_published_0009_database_receives_operational_configuration(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "published-0009.db"
    _upgrade(database_path, "0009", monkeypatch)
    operational_columns = (
        "account_creation_enabled",
        "account_pending_retention_days",
        "account_review_interval_days",
        "account_review_reminder_days",
        "account_review_grace_days",
        "contact_problem_reminder_days",
        "account_retention_after_disable_days",
        "message_retention_hours",
        "audit_retention_days",
    )
    with sqlite3.connect(database_path) as connection:
        published_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(system_configurations)"
            )
        }
        assert not set(operational_columns) & published_columns
        connection.execute(
            "INSERT INTO system_configurations "
            "(id, notification_delay_minutes, updated_at) "
            "VALUES ('default', 75, '2026-08-06 12:00:00')"
        )
        connection.commit()

    _upgrade(database_path, "head", monkeypatch)

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]: row[4]
            for row in connection.execute(
                "PRAGMA table_info(system_configurations)"
            )
        }
        stored = connection.execute(
            "SELECT notification_delay_minutes, account_creation_enabled, "
            "account_review_interval_days, message_retention_days "
            "FROM system_configurations WHERE id = 'default'"
        ).fetchone()
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
    assert set(operational_columns) - {"message_retention_hours"} <= columns.keys()
    assert "message_retention_hours" not in columns
    assert "message_retention_days" in columns
    assert stored == (75, 1, 180, 30)
    assert revision == "0012"


def test_previous_alembic_head_upgrades_to_protected_inbox_schema(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "previous-head.db"
    _upgrade(database_path, "0010", monkeypatch)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO accounts
                (id, status, created_at, updated_at, is_admin_locked, version,
                 last_contact_problem_reminder_at, language_code)
            VALUES
                ('migrated-account', 'active', '2026-08-01 12:00:00',
                 '2026-08-01 12:00:00', 0, 1, NULL, 'de')
            """
        )
        connection.execute(
            """
            INSERT INTO partners
                (id, account_id, encrypted_name, is_active, created_at, updated_at)
            VALUES
                ('migrated-partner', 'migrated-account', X'00', 1,
                 '2026-08-01 12:00:00', '2026-08-01 12:00:00')
            """
        )
        connection.commit()
    _upgrade(database_path, "head", monkeypatch)
    with sqlite3.connect(database_path) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        migrated_partner = connection.execute(
            "SELECT account_id, is_active FROM partners WHERE id = 'migrated-partner'"
        ).fetchone()
        migrated_credential = connection.execute(
            "SELECT partner_id FROM partner_credentials WHERE partner_id = 'migrated-partner'"
        ).fetchone()
    assert revision == "0012"
    assert {"partner_credentials", "notification_recipients"} <= tables
    assert migrated_partner == ("migrated-account", 1)
    assert migrated_credential is None


def test_existing_database_upgrades_without_losing_data(tmp_path, monkeypatch):
    database_path = tmp_path / "existing.db"
    _upgrade(database_path, "0001", monkeypatch)
    with sqlite3.connect(database_path) as connection:
        # Revision 0001 is metadata-driven; remove the newly modeled column to
        # reproduce a database created by the already published 0001 migration.
        connection.execute("ALTER TABLE accounts DROP COLUMN language_code")
        connection.execute(
            "DROP INDEX ix_trusted_person_tokens_enrollment_expires_at"
        )
        connection.executescript(
            """
            CREATE TABLE trusted_person_tokens_old (
                trusted_person_id VARCHAR(36) PRIMARY KEY,
                token_hash VARCHAR(64) NOT NULL UNIQUE,
                created_at DATETIME NOT NULL,
                rotated_at DATETIME,
                revoked_at DATETIME,
                last_used_at DATETIME
            );
            INSERT INTO trusted_person_tokens_old
                VALUES ('existing-person', 'existing-token-hash',
                        '2026-07-26 12:00:00', NULL, NULL, NULL);
            DROP TABLE trusted_person_tokens;
            ALTER TABLE trusted_person_tokens_old RENAME TO trusted_person_tokens;
            CREATE UNIQUE INDEX ix_trusted_person_tokens_token_hash
                ON trusted_person_tokens (token_hash);

            DROP INDEX ix_server_sessions_trusted_person_id;
            CREATE TABLE server_sessions_old (
                id_hash VARCHAR(64) PRIMARY KEY,
                kind VARCHAR(16) NOT NULL,
                account_id VARCHAR(36),
                csrf_hash VARCHAR(64) NOT NULL,
                created_at DATETIME NOT NULL,
                expires_at DATETIME NOT NULL
            );
            INSERT INTO server_sessions_old
                SELECT id_hash, kind, account_id, csrf_hash, created_at, expires_at
                FROM server_sessions;
            DROP TABLE server_sessions;
            ALTER TABLE server_sessions_old RENAME TO server_sessions;
            CREATE INDEX ix_server_sessions_account_id
                ON server_sessions (account_id);
            CREATE INDEX ix_server_sessions_expires_at
                ON server_sessions (expires_at);
            """
        )
        connection.execute(
            """
            INSERT INTO audit_logs
                (id, account_id, event_type, technical_metadata, created_at, request_id)
            VALUES
                ('existing-event', NULL, 'existing', '{}', '2026-07-26 12:00:00', NULL)
            """
        )
        connection.execute(
            """
            INSERT INTO deliveries
                (id, notification_id, contact_method_id, provider, status,
                 attempt_count, last_attempt_at, next_retry_at,
                 provider_message_id, encrypted_error_detail, created_at, delivered_at)
            VALUES
                ('stuck-delivery', 'missing-notification', NULL, 'email',
                 'processing', 0, NULL, NULL, NULL, NULL,
                 '2026-07-26 12:00:00', NULL)
            """
        )
        connection.commit()

    _upgrade(database_path, "head", monkeypatch)

    with sqlite3.connect(database_path) as connection:
        event = connection.execute(
            "SELECT event_type FROM audit_logs WHERE id = 'existing-event'"
        ).fetchone()
        public_table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'public_site_contents'
            """
        ).fetchone()
        account_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(accounts)")
        }
        contact_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(contact_methods)")
        }
        tracking_table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'email_delivery_tracking'
            """
        ).fetchone()
        delivery_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(deliveries)")
        }
        recovered_delivery = connection.execute(
            """
            SELECT status, processing_started_at, processing_until
            FROM deliveries WHERE id = 'stuck-delivery'
            """
        ).fetchone()
        trusted_token = connection.execute(
            """
            SELECT pin_hash, enrollment_expires_at, enrolled_at
            FROM trusted_person_tokens
            WHERE trusted_person_id = 'existing-person'
            """
        ).fetchone()
    assert event == ("existing",)
    assert public_table == ("public_site_contents",)
    assert "language_code" in account_columns
    assert "permanent_failure_count" in contact_columns
    assert tracking_table == ("email_delivery_tracking",)
    assert "processing_started_at" in delivery_columns
    assert "processing_until" in delivery_columns
    assert recovered_delivery == ("processing", None, None)
    assert trusted_token[0] is None
    assert trusted_token[2] is None
    enrollment_deadline = datetime.fromisoformat(trusted_token[1])
    assert enrollment_deadline.replace(tzinfo=UTC) > utc_now() + timedelta(days=13)
