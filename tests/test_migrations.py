from __future__ import annotations

import sqlite3

from alembic import command
from alembic.config import Config

from app.config import get_settings


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
    assert "public_site_contents" in tables
    assert "email_delivery_tracking" in tables
    assert "language_code" in account_columns
    assert "permanent_failure_count" in contact_columns
    assert "contact_reviews" in tables
    assert "contact_review_tokens" in tables
    assert "last_contact_problem_reminder_at" in account_columns
    assert "last_review_expired_at" in contact_columns
    assert revision == "0005"


def test_existing_database_upgrades_without_losing_data(tmp_path, monkeypatch):
    database_path = tmp_path / "existing.db"
    _upgrade(database_path, "0001", monkeypatch)
    with sqlite3.connect(database_path) as connection:
        # Revision 0001 is metadata-driven; remove the newly modeled column to
        # reproduce a database created by the already published 0001 migration.
        connection.execute("ALTER TABLE accounts DROP COLUMN language_code")
        connection.execute(
            """
            INSERT INTO audit_logs
                (id, account_id, event_type, technical_metadata, created_at, request_id)
            VALUES
                ('existing-event', NULL, 'existing', '{}', '2026-07-26 12:00:00', NULL)
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
    assert event == ("existing",)
    assert public_table == ("public_site_contents",)
    assert "language_code" in account_columns
    assert "permanent_failure_count" in contact_columns
    assert tracking_table == ("email_delivery_tracking",)
