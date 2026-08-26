from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None,
    reason="update script requires a POSIX shell",
)


def _prepare(
    tmp_path: Path,
    *,
    dirty: bool = False,
    fail_transfer: bool = False,
    update_available: bool = True,
    fast_forward: bool = True,
) -> tuple[Path, Path]:
    project = tmp_path / "project"
    bin_dir = project / "test-bin"
    project.mkdir()
    bin_dir.mkdir()
    shutil.copy(Path(__file__).parents[1] / "update.sh", project)
    for name in (".env", ".backup.conf", ".backup-transfer.conf"):
        (project / name).touch()
    backup = tmp_path / "silentrelay-test-20260807T120000Z.tar.gz.age"
    backup.write_bytes(b"encrypted")
    (project / "backup.sh").write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' backup >> commands\n"
        f"printf '%s\\n' 'Backup created: {backup}'\n",
        encoding="utf-8",
    )
    transfer_exit = 1 if fail_transfer else 0
    (project / "backup-transfer.sh").write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"transfer:$1\" >> commands\nexit {transfer_exit}\n",
        encoding="utf-8",
    )
    status = " M tracked" if dirty else ""
    available_commit = "def456" if update_available else "abc123"
    merge_base_exit = 0 if fast_forward else 1
    (bin_dir / "git").write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"git:$*\" >> commands\n"
        f"[ \"$1 $2\" = 'status --porcelain' ] && {{ printf '%s' '{status}'; exit 0; }}\n"
        "[ \"$1 $2\" = 'rev-parse HEAD' ] && {\n"
        "    [ -f .updated ] && printf '%s\\n' def456 || printf '%s\\n' abc123\n"
        "    exit 0\n"
        "}\n"
        "[ \"$1 $2 $3\" = 'rev-parse --abbrev-ref --symbolic-full-name' ] && "
        "{ printf '%s\\n' origin/main; exit 0; }\n"
        f"[ \"$1 $2\" = 'rev-parse origin/main' ] && {{ printf '%s\\n' {available_commit}; exit 0; }}\n"
        f"[ \"$1\" = 'merge-base' ] && exit {merge_base_exit}\n"
        "[ \"$1 $2\" = 'merge --ff-only' ] && { touch .updated; exit 0; }\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "docker").write_text(
        "#!/bin/sh\nprintf '%s\\n' \"docker:$*\" >> commands\nexit 0\n", encoding="utf-8"
    )
    for path in (bin_dir / "git", bin_dir / "docker"):
        path.chmod(0o755)
    return project, bin_dir


def _run(project: Path, bin_dir: Path) -> subprocess.CompletedProcess[str]:
    assert bin_dir == project / "test-bin"
    return subprocess.run(
        ["sh", "-c", 'PATH="$PWD/test-bin:$PATH" sh update.sh'],
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )


def test_successful_update_runs_safety_steps_in_order(tmp_path: Path) -> None:
    project, bin_dir = _prepare(tmp_path)

    result = _run(project, bin_dir)

    assert result.returncode == 0
    commands = (project / "commands").read_text(encoding="utf-8")
    assert commands.index("git:fetch --quiet") < commands.index("backup")
    assert commands.index("transfer:") < commands.index("git:merge --ff-only def456")
    assert commands.index("git:merge --ff-only def456") < commands.index(
        "docker:compose up -d --build --wait --wait-timeout 120"
    )
    assert "docker:compose exec -T web python -c" in commands
    assert "[1/6] Checking prerequisites" in result.stdout
    assert "[2/6] Checking for updates" in result.stdout
    assert "[3/6] Creating encrypted backup" in result.stdout
    assert "[4/6] Transferring backup off-site" in result.stdout
    assert "[5/6] Installing update and starting services" in result.stdout
    assert "[6/6] Verifying readiness" in result.stdout
    assert "Update completed: abc123 -> def456" in result.stdout


def test_current_installation_stops_before_backup_and_build(tmp_path: Path) -> None:
    project, bin_dir = _prepare(tmp_path, update_available=False)

    result = _run(project, bin_dir)

    assert result.returncode == 0
    commands = (project / "commands").read_text(encoding="utf-8")
    assert "git:fetch --quiet" in commands
    assert "backup" not in commands
    assert "transfer:" not in commands
    assert "git:merge --ff-only" not in commands
    assert "docker:" not in commands
    assert "No update available. Installed commit: abc123" in result.stdout


def test_dirty_worktree_stops_before_backup(tmp_path: Path) -> None:
    project, bin_dir = _prepare(tmp_path, dirty=True)

    result = _run(project, bin_dir)

    assert result.returncode != 0
    assert "worktree is not clean" in result.stderr
    assert "transfer:" not in (project / "commands").read_text(encoding="utf-8")


def test_failed_transfer_stops_before_git_merge(tmp_path: Path) -> None:
    project, bin_dir = _prepare(tmp_path, fail_transfer=True)

    result = _run(project, bin_dir)

    assert result.returncode != 0
    commands = (project / "commands").read_text(encoding="utf-8")
    assert "transfer:" in commands
    assert "git:merge --ff-only" not in commands


def test_non_fast_forward_stops_before_backup(tmp_path: Path) -> None:
    project, bin_dir = _prepare(tmp_path, fast_forward=False)

    result = _run(project, bin_dir)

    assert result.returncode != 0
    commands = (project / "commands").read_text(encoding="utf-8")
    assert "cannot be fast-forwarded" in result.stderr
    assert "backup" not in commands
    assert "transfer:" not in commands
