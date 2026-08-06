from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None,
    reason="backup transfer script requires a POSIX host",
)


def _prepare(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    backups = tmp_path / "backups"
    project.mkdir()
    backups.mkdir()
    shutil.copy(Path(__file__).parents[1] / "backup-transfer.sh", project)
    (project / ".backup.conf").write_text(
        f"BACKUP_DIRECTORY={backups}\n"
        "KEEP_BACKUPS=7\n"
        "AGE_RECIPIENT=age1test\n"
        "INSTALLATION_ID=test-installation\n",
        encoding="utf-8",
    )
    archive = backups / "silentrelay-test-installation-20260806T120000Z.tar.gz.age"
    archive.write_bytes(b"encrypted-backup")
    return project, archive


def _run(
    project: Path, *arguments: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "backup-transfer.sh", *arguments],
        cwd=project,
        env={**os.environ, **(env or {})},
        text=True,
        capture_output=True,
        check=False,
    )


def test_rejects_webdav_without_https(tmp_path: Path) -> None:
    project, archive = _prepare(tmp_path)
    credentials = project / "credentials.netrc"
    credentials.write_text(
        "machine example.test login backup password secret\n", encoding="utf-8"
    )
    credentials.chmod(0o600)
    (project / ".backup-transfer.conf").write_text(
        "TRANSFER_TARGET=webdav\n"
        "WEBDAV_BASE_URL=http://example.test/backups\n"
        f"WEBDAV_CREDENTIAL_FILE={credentials}\n",
        encoding="utf-8",
    )

    result = _run(project, str(archive))

    assert result.returncode != 0
    assert "must use HTTPS" in result.stderr
    assert "secret" not in result.stdout + result.stderr


def test_rejects_non_private_webdav_credentials(tmp_path: Path) -> None:
    project, archive = _prepare(tmp_path)
    credentials = project / "credentials.netrc"
    credentials.write_text(
        "machine example.test login backup password secret\n", encoding="utf-8"
    )
    credentials.chmod(0o644)
    (project / ".backup-transfer.conf").write_text(
        "TRANSFER_TARGET=webdav\n"
        "WEBDAV_BASE_URL=https://example.test/backups\n"
        f"WEBDAV_CREDENTIAL_FILE={credentials}\n",
        encoding="utf-8",
    )

    result = _run(project, str(archive))

    assert result.returncode != 0
    assert "mode 0600" in result.stderr
    assert "secret" not in result.stdout + result.stderr


def test_rejects_archive_from_another_installation(tmp_path: Path) -> None:
    project, _ = _prepare(tmp_path)
    archive = project / "silentrelay-other-20260806T120000Z.tar.gz.age"
    archive.write_bytes(b"encrypted-backup")
    (project / ".backup-transfer.conf").write_text(
        "TRANSFER_TARGET=sftp\n",
        encoding="utf-8",
    )

    result = _run(project, str(archive))

    assert result.returncode != 0
    assert "not a completed backup for this installation" in result.stderr
