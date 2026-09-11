import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite:///./data/test.db",
        "sqlite:///:memory:",
        "sqlite+pysqlite:///./data/test.db",
    ],
)
def test_settings_accept_supported_sqlite_urls(database_url):
    settings = Settings(database_url=database_url)

    assert settings.database_url == database_url


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://user:password@database/silentrelay",
        "mysql://user:password@database/silentrelay",
        "sqlite://server/share/app.db",
    ],
)
def test_settings_reject_unsupported_database_urls(database_url):
    with pytest.raises(ValidationError, match="DATABASE_URL must use SQLite"):
        Settings(database_url=database_url)


def test_pysqlite_database_directory_is_created(tmp_path):
    database_path = tmp_path / "nested" / "app.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path.as_posix()}")

    settings.ensure_database_directory()

    assert database_path.parent.is_dir()
